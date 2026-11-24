"""가상 체결 엔진 (명세서 9-8, FR-05~10).

- 시장가: 주문 시점 현재가로 즉시 전량 체결
- 지정가: 현재가가 지정가에 닿으면 지정가로 전량 체결. 주문할 때 이미 유리하면 현재가로 즉시 체결
- 미체결 지정가는 장 마감 때 자동 취소
- 주문 가능 금액 = 현금 - 미체결 매수 주문 금액, 매도 가능 수량 = 보유 - 미체결 매도
- 첫 매수 체결 ~ 보유 0주까지가 하나의 거래(trade)
- 매수 주문 때 매매 계획을 받는다. 판정은 주문 제출 시점의 최초 값으로 한다.
- 체결마다 수수료·세금과 그 순간의 시세·지표 스냅샷을 저장한다.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from .. import settings
from ..rules import engine as rules
from . import indicators
from .prices import ReplayFeed


class OrderError(Exception):
    """사용자에게 그대로 보여줄 수 있는 주문 거절 사유."""


def _account(conn, project_id: int):
    return conn.execute("SELECT account_id, cash FROM accounts WHERE project_id = ?", (project_id,)).fetchone()


def tradable_codes(conn, project_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT s.stock_code FROM replay_stocks s JOIN projects p USING (period_id) WHERE p.project_id = ?",
        (project_id,),
    ).fetchall()
    return {r[0] for r in rows}


def buy_cost(price: int, qty: int) -> int:
    amount = price * qty
    return amount + math.floor(amount * settings.BUY_FEE_RATE)


def available_cash(conn, account_id: int, cash: int) -> int:
    """현금 - 미체결 매수 주문에 묶인 금액 (지정가 × 수량 + 수수료)."""
    rows = conn.execute(
        "SELECT limit_price, quantity FROM orders WHERE account_id = ? AND side = 'buy' AND status = 'open'",
        (account_id,),
    ).fetchall()
    return cash - sum(buy_cost(r[0], r[1]) for r in rows)


def sellable_qty(conn, account_id: int, code: str) -> int:
    held = conn.execute(
        "SELECT quantity FROM holdings WHERE account_id = ? AND stock_code = ?", (account_id, code)
    ).fetchone()
    pending = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM orders "
        "WHERE account_id = ? AND stock_code = ? AND side = 'sell' AND status = 'open'",
        (account_id, code),
    ).fetchone()[0]
    return (held[0] if held else 0) - pending


# ------------------------------------------------------------------ 주문
def place_order(conn, feed: ReplayFeed, project_id: int, t: datetime, clock_status: str,
                code: str, side: str, order_type: str, qty: int,
                limit_price: Optional[int] = None, plan: Optional[dict] = None) -> dict:
    if clock_status not in ("running", "paused"):
        raise OrderError("장이 열려 있을 때만 주문할 수 있어요")
    if code not in tradable_codes(conn, project_id):
        raise OrderError("이 기간에 거래할 수 있는 종목이 아니에요")
    if side not in ("buy", "sell") or order_type not in ("market", "limit"):
        raise OrderError("주문 종류가 올바르지 않아요")
    if qty <= 0:
        raise OrderError("수량은 1주 이상이어야 해요")
    if order_type == "limit" and (not limit_price or limit_price <= 0):
        raise OrderError("지정가를 입력해 주세요")
    if order_type == "market":
        limit_price = None
    q = feed.quote(code, t)
    if q is None:
        raise OrderError("지금은 이 종목의 시세가 없어요 (거래정지 등)")

    acc = _account(conn, project_id)
    account_id, cash = acc[0], acc[1]
    if side == "buy":
        need = buy_cost(limit_price or q.price, qty)
        if need > available_cash(conn, account_id, cash):
            raise OrderError("주문 가능 금액이 부족해요")
    elif qty > sellable_qty(conn, account_id, code):
        raise OrderError("매도 가능 수량이 부족해요")

    order_id = conn.execute(
        "INSERT INTO orders (account_id, stock_code, side, order_type, limit_price, quantity, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, code, side, order_type, limit_price, qty, t.isoformat()),
    ).lastrowid

    if side == "buy":
        _attach_plan(conn, account_id, code, order_id, plan or {}, t)

    marketable = (
        order_type == "market"
        or (side == "buy" and limit_price >= q.price)
        or (side == "sell" and limit_price <= q.price)
    )
    if marketable:
        fill(conn, feed, order_id, q.price, t)
    return {"order_id": order_id, "filled": marketable}


def _attach_plan(conn, account_id: int, code: str, order_id: int, plan: dict, t: datetime) -> None:
    """진행 중인 거래나 체결 대기 중인 첫 매수가 이미 있으면 그 계획을 따른다. 없으면 새 계획."""
    has_trade = conn.execute(
        "SELECT 1 FROM trades WHERE account_id = ? AND stock_code = ? AND status = 'open'", (account_id, code)
    ).fetchone()
    pending = conn.execute(
        "SELECT 1 FROM plans p JOIN orders o ON o.order_id = p.first_order_id "
        "WHERE p.trade_id IS NULL AND o.account_id = ? AND o.stock_code = ? AND o.status = 'open' "
        "AND o.order_id <> ?",
        (account_id, code, order_id),
    ).fetchone()
    if has_trade or pending:
        return
    conn.execute(
        "INSERT INTO plans (first_order_id, reason, target_price, stop_price, target_qty, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, plan.get("reason") or None, plan.get("target_price"), plan.get("stop_price"),
         plan.get("target_qty"), t.isoformat()),
    )


def cancel_order(conn, project_id: int, order_id: int, t: datetime, reason: str = "user") -> None:
    account_id = _account(conn, project_id)[0]
    cur = conn.execute(
        "UPDATE orders SET status = 'cancelled', closed_at = ?, cancel_reason = ? "
        "WHERE order_id = ? AND account_id = ? AND status = 'open'",
        (t.isoformat(), reason, order_id, account_id),
    )
    if cur.rowcount == 0:
        raise OrderError("취소할 수 있는 주문이 아니에요")


# ------------------------------------------------------------------ 체결
def fill(conn, feed: ReplayFeed, order_id: int, price: int, t: datetime) -> int:
    o = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    account_id, code, qty = o["account_id"], o["stock_code"], o["quantity"]
    amount = price * qty
    if o["side"] == "buy":
        fee, tax = math.floor(amount * settings.BUY_FEE_RATE), 0
        pre_total = total_assets(conn, feed, account_id, t)          # RM-01·02 분모: 매수 직전 총자산
        conn.execute("UPDATE accounts SET cash = cash - ? WHERE account_id = ?", (amount + fee, account_id))
        trade_id = _open_or_get_trade(conn, account_id, code, order_id, t)
        conn.execute(
            "INSERT INTO holdings (account_id, stock_code, quantity, trade_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (account_id, stock_code) DO UPDATE SET quantity = quantity + excluded.quantity, "
            "trade_id = excluded.trade_id",
            (account_id, code, qty, trade_id),
        )
    else:
        fee = math.floor(amount * settings.SELL_FEE_RATE)
        tax = math.floor(amount * settings.SELL_TAX_RATE)
        conn.execute("UPDATE accounts SET cash = cash + ? WHERE account_id = ?", (amount - fee - tax, account_id))
        trade_id = conn.execute(
            "SELECT trade_id FROM holdings WHERE account_id = ? AND stock_code = ?", (account_id, code)
        ).fetchone()[0]
        conn.execute(
            "UPDATE holdings SET quantity = quantity - ? WHERE account_id = ? AND stock_code = ?",
            (qty, account_id, code),
        )
    conn.execute(
        "UPDATE orders SET status = 'filled', closed_at = ?, trade_id = ? WHERE order_id = ?",
        (t.isoformat(), trade_id, order_id),
    )
    exec_id = conn.execute(
        "INSERT INTO executions (order_id, quantity, price, fee, tax, executed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, qty, price, fee, tax, t.isoformat()),
    ).lastrowid
    _snapshot(conn, feed, exec_id, code, price, t)

    if o["side"] == "buy":
        rules.on_buy_fill(conn, account_id=account_id, order_id=order_id, exec_id=exec_id, trade_id=trade_id,
                          price=price, qty=qty, pre_total_assets=pre_total, t=t)
    else:
        left = conn.execute(
            "SELECT quantity FROM holdings WHERE account_id = ? AND stock_code = ?", (account_id, code)
        ).fetchone()[0]
        if left == 0:
            close_trade(conn, trade_id, t)
            conn.execute("DELETE FROM holdings WHERE account_id = ? AND stock_code = ?", (account_id, code))
            rules.on_trade_closed(conn, trade_id, t)
    return exec_id


def total_assets(conn, feed: ReplayFeed, account_id: int, t: datetime) -> int:
    """현금 + 보유 주식 평가액 (그 시각 현재가). 시세가 없는 종목은 0으로 친다."""
    cash = conn.execute("SELECT cash FROM accounts WHERE account_id = ?", (account_id,)).fetchone()[0]
    value = 0
    for h in conn.execute("SELECT stock_code, quantity FROM holdings WHERE account_id = ? AND quantity > 0",
                          (account_id,)):
        q = feed.quote(h[0], t)
        value += (q.price if q else 0) * h[1]
    return cash + value


def _open_or_get_trade(conn, account_id: int, code: str, order_id: int, t: datetime) -> int:
    row = conn.execute(
        "SELECT trade_id FROM trades WHERE account_id = ? AND stock_code = ? AND status = 'open'", (account_id, code)
    ).fetchone()
    if row:
        return row[0]
    trade_id = conn.execute(
        "INSERT INTO trades (account_id, stock_code, opened_at) VALUES (?, ?, ?)",
        (account_id, code, t.isoformat()),
    ).lastrowid
    plan = conn.execute(
        "SELECT p.plan_id FROM plans p JOIN orders o ON o.order_id = p.first_order_id "
        "WHERE p.trade_id IS NULL AND o.account_id = ? AND o.stock_code = ? "
        "ORDER BY (p.first_order_id = ?) DESC, p.plan_id DESC LIMIT 1",
        (account_id, code, order_id),
    ).fetchone()
    if plan:
        conn.execute("UPDATE plans SET trade_id = ? WHERE plan_id = ?", (trade_id, plan[0]))
        conn.execute("UPDATE trades SET plan_id = ? WHERE trade_id = ?", (plan[0], trade_id))
    return trade_id


def close_trade(conn, trade_id: int, t: datetime) -> None:
    """보유 0주가 된 순간 거래 결과를 확정한다 (비용 차감 후 손익, 명세서 9-7)."""
    r = conn.execute(
        "SELECT "
        " COALESCE(SUM(CASE WHEN o.side = 'buy'  THEN e.price * e.quantity + e.fee END), 0), "
        " COALESCE(SUM(CASE WHEN o.side = 'sell' THEN e.price * e.quantity - e.fee - e.tax END), 0) "
        "FROM executions e JOIN orders o USING (order_id) WHERE o.trade_id = ?",
        (trade_id,),
    ).fetchone()
    net_buy, net_sell = r[0], r[1]
    conn.execute(
        "UPDATE trades SET status = 'closed', closed_at = ?, net_buy_amount = ?, net_sell_amount = ?, "
        "realized_pnl = ?, realized_return = ? WHERE trade_id = ?",
        (t.isoformat(), net_buy, net_sell, net_sell - net_buy, net_sell / net_buy - 1 if net_buy else None, trade_id),
    )


def _snapshot(conn, feed: ReplayFeed, exec_id: int, code: str, price: int, t: datetime) -> None:
    q = feed.quote(code, t)
    ma, avg_vol = indicators.ma_and_avg_volume(conn, code, t.date())
    conn.execute(
        "INSERT INTO execution_snapshots (execution_id, current_price, prev_close, cum_volume, elapsed_ratio, "
        "avg_volume_20d, ma_20d, rsi_14) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (exec_id, q.price if q else price, q.prev_close if q else None, q.cum_volume if q else None,
         q.elapsed_ratio if q else None, avg_vol, ma, indicators.rsi_with_price(conn, code, t.date(), price)),
    )


# ------------------------------------------------------------------ 시계 1초마다
class TickWatcher:
    """시계가 시뮬레이션 1초마다 부른다. 지정가 체결과 손절·목표가 최초 도달을 확인한다.

    매초 DB를 읽지 않도록, 감시 대상(미체결 지정가, 진행 중 거래의 계획)을 기억해 두고
    무언가 체결되거나 기록됐을 때만 다시 읽는다.
    """

    def __init__(self, conn, feed: ReplayFeed, project_id: int):
        self.conn, self.feed, self.project_id = conn, feed, project_id
        self.account_id = _account(conn, project_id)[0]
        self.events: list[str] = []
        self._reload()

    def _reload(self) -> None:
        self.orders = self.conn.execute(
            "SELECT order_id, stock_code, side, limit_price FROM orders "
            "WHERE account_id = ? AND status = 'open' AND order_type = 'limit'",
            (self.account_id,),
        ).fetchall()
        self.trades = self.conn.execute(
            "SELECT t.trade_id, t.stock_code, p.stop_price, p.target_price, t.stop_reached_at, t.target_reached_at "
            "FROM trades t LEFT JOIN plans p ON p.plan_id = t.plan_id "
            "WHERE t.account_id = ? AND t.status = 'open'",
            (self.account_id,),
        ).fetchall()

    def __call__(self, t: datetime) -> None:
        changed = False
        for o in self.orders:
            q = self.feed.quote(o["stock_code"], t)
            if q is None:
                continue
            if (o["side"] == "buy" and q.price <= o["limit_price"]) or \
               (o["side"] == "sell" and q.price >= o["limit_price"]):
                fill(self.conn, self.feed, o["order_id"], o["limit_price"], t)
                self.events.append(f"order_filled:{o['order_id']}")
                changed = True
        for tr in self.trades:
            q = self.feed.quote(tr["stock_code"], t)
            if q is None:
                continue
            if tr["stop_price"] and not tr["stop_reached_at"] and q.price <= tr["stop_price"]:
                self._touch(tr["trade_id"], "stop", q.price, t)
                changed = True
            if tr["target_price"] and not tr["target_reached_at"] and q.price >= tr["target_price"]:
                self._touch(tr["trade_id"], "target", q.price, t)
                changed = True
        if changed:
            self._reload()

    def _touch(self, trade_id: int, kind: str, price: int, t: datetime) -> None:
        col = "stop_reached_at" if kind == "stop" else "target_reached_at"
        self.conn.execute(f"UPDATE trades SET {col} = ? WHERE trade_id = ?", (t.isoformat(), trade_id))
        self.conn.execute(
            "INSERT OR IGNORE INTO price_touches (trade_id, kind, touched_at, price) VALUES (?, ?, ?, ?)",
            (trade_id, kind, t.isoformat(), price),
        )
        self.events.append(f"{kind}_reached:{trade_id}")


# ------------------------------------------------------------------ 장 마감
def market_close(conn, project_id: int, t: datetime) -> int:
    """장 마감 처리 1단계: 미체결 주문 자동 취소. 규칙 판정(2~6단계)은 3주차에 이어 붙인다."""
    account_id = _account(conn, project_id)[0]
    cur = conn.execute(
        "UPDATE orders SET status = 'cancelled', closed_at = ?, cancel_reason = 'market_close' "
        "WHERE account_id = ? AND status = 'open'",
        (t.isoformat(), account_id),
    )
    return cur.rowcount
