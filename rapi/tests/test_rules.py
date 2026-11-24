"""14개 규칙의 경계값 (명세서 3~6장).

시세는 원하는 값을 넣을 수 있는 가짜 공급원(FakeFeed)으로 정하고, 실제 주문·체결 흐름을 그대로 통과시킨다.
"""
from datetime import datetime, timedelta

import pytest

from app.engine import indicators
from app.engine.prices import Quote
from app.engine.trading import place_order
from app.projects import create_guest, create_project
from app.rules import engine as rules
from app.rules.catalog import scores_for_project

D1 = datetime(2024, 3, 4, 9, 0)        # 1일차 장 시작 (개발용 기간)


class FakeFeed:
    """종목별 현재가·전일 종가를 직접 정하는 시세 공급원."""

    def __init__(self):
        self.price, self.prev = {}, {}
        self.cum_volume = 10_000_000

    def quote(self, code, t):
        if code not in self.price:
            return None
        r = min(max((t - t.replace(hour=9, minute=0, second=0)).total_seconds() / 23_400, 0), 1)
        return Quote(code, self.price[code], self.prev.get(code, self.price[code]), self.cum_volume, r)


@pytest.fixture
def P(conn, period_id, monkeypatch):
    """프로젝트 하나와 조작 도구."""
    # MC 지표는 기본적으로 '문제없음' 값으로 고정 (각 테스트에서 바꿈)
    monkeypatch.setattr(indicators, "rsi_with_price", lambda *a: 50.0)
    monkeypatch.setattr(indicators, "ma_and_avg_volume", lambda *a: (1e12, 1.0))
    pid = create_project(conn, create_guest(conn), period_id)
    feed = FakeFeed()

    class Env:
        pass
    e = Env()
    e.conn, e.feed, e.pid = conn, feed, pid
    e.acc = conn.execute("SELECT account_id FROM accounts WHERE project_id=?", (pid,)).fetchone()[0]

    def buy(code, qty, price, t, plan=None, prev=None):
        feed.price[code] = price
        feed.prev[code] = prev or price
        full = {"reason": "이유", "target_price": price * 2, "stop_price": 1, "target_qty": 10 ** 6}
        return place_order(conn, feed, pid, t, "running", code, "buy", "market", qty,
                           plan=full if plan is None else plan)

    def sell(code, qty, price, t):
        feed.price[code] = price
        return place_order(conn, feed, pid, t, "running", code, "sell", "market", qty)

    def evals(rule_id=None):
        sql = "SELECT * FROM rule_evaluations WHERE project_id = ?" + (" AND rule_id = ?" if rule_id else "")
        return conn.execute(sql, (pid, rule_id) if rule_id else (pid,)).fetchall()

    def violated(rule_id):
        return [r for r in evals(rule_id) if r["result"] == "violation"]

    e.buy, e.sell, e.evals, e.violated = buy, sell, evals, violated
    return e


# ------------------------------------------------------------------ RM
@pytest.mark.parametrize("qty, rm01, rm02", [
    (100, False, False),     # 5,000,000 = 총자산의 50% → 초과 아님
    (101, True, False),      # 50.5%
    (160, True, False),      # 80% → RM-02 초과 아님
    (161, True, True),       # 80.5% → 둘 다 (-45)
])
def test_rm01_rm02_boundaries(P, qty, rm01, rm02):
    P.buy("DEV001", qty, 50_000, D1 + timedelta(minutes=10))
    assert bool(P.violated("RM-01")) == rm01
    assert bool(P.violated("RM-02")) == rm02


def test_rm01_counts_holdings_in_total_assets(P):
    P.buy("DEV001", 90, 50_000, D1 + timedelta(minutes=10))          # 45% → 감점 없음
    # 현금은 약 550만이지만 총자산은 주식 450만을 더한 약 1,000만 → 500만은 50% 초과
    P.buy("DEV003", 100, 50_000, D1 + timedelta(minutes=20))
    assert len(P.violated("RM-01")) == 1


def test_rm04_single_order_fills_target_qty(P):
    plan = {"reason": "a", "target_price": 99_999, "stop_price": 1, "target_qty": 10}
    P.buy("DEV001", 10, 10_000, D1 + timedelta(minutes=5), plan=plan)
    assert P.violated("RM-04")


def test_rm04_not_when_split(P):
    plan = {"reason": "a", "target_price": 99_999, "stop_price": 1, "target_qty": 10}
    P.buy("DEV001", 5, 10_000, D1 + timedelta(minutes=5), plan=plan)
    P.buy("DEV001", 5, 10_000, D1 + timedelta(minutes=6))
    assert not P.violated("RM-04")


def test_rm03_held_after_stop_until_close(P):
    P.buy("DEV001", 10, 10_000, D1 + timedelta(minutes=5))
    P.conn.execute("UPDATE trades SET stop_reached_at = ?", ((D1 + timedelta(hours=2)).isoformat(),))
    rules.on_market_close(P.conn, P.pid, 1, D1.replace(hour=15, minute=30))
    assert P.violated("RM-03")


def test_rm03_not_when_sold_before_close(P):
    P.buy("DEV001", 10, 10_000, D1 + timedelta(minutes=5))
    P.conn.execute("UPDATE trades SET stop_reached_at = ?", ((D1 + timedelta(hours=2)).isoformat(),))
    P.sell("DEV001", 10, 9_000, D1 + timedelta(hours=3))
    rules.on_market_close(P.conn, P.pid, 1, D1.replace(hour=15, minute=30))
    assert not P.violated("RM-03")


# ------------------------------------------------------------------ PA
def test_pa01_lists_missing_items_once(P):
    P.buy("DEV001", 1, 10_000, D1 + timedelta(minutes=5), plan={"reason": "이유", "target_price": 12_000})
    P.buy("DEV001", 1, 10_000, D1 + timedelta(minutes=6))
    v = P.violated("PA-01")
    assert len(v) == 1 and '"손절가"' in v[0]["evidence"] and "목표가" not in v[0]["evidence"]


def test_pa01_ok_when_all_filled(P):
    P.buy("DEV001", 1, 10_000, D1 + timedelta(minutes=5))
    assert not P.violated("PA-01")


def _closed_trade(P, ret, target_reached=False, minutes=60):
    """수익률을 원하는 값으로 정한 끝난 거래를 하나 만든다."""
    P.buy("DEV001", 1, 10_000, D1 + timedelta(minutes=5),
          plan={"reason": "a", "target_price": 20_000, "stop_price": 1, "target_qty": 99})
    closed = D1 + timedelta(minutes=5 + minutes)
    tid = P.conn.execute("SELECT trade_id FROM trades WHERE status='open'").fetchone()[0]
    P.conn.execute(
        "UPDATE trades SET status='closed', closed_at=?, realized_pnl=?, realized_return=?, target_reached_at=? "
        "WHERE trade_id=?",
        (closed.isoformat(), int(ret * 10_000), ret,
         (closed - timedelta(minutes=1)).isoformat() if target_reached else None, tid))
    P.conn.execute("DELETE FROM holdings")
    rules.on_trade_closed(P.conn, tid, closed)


@pytest.mark.parametrize("ret, expected", [(0.02, True), (0.0001, True), (0.0201, False), (0.0, False), (-0.01, False)])
def test_pa02_return_range(P, ret, expected):
    _closed_trade(P, ret)
    assert bool(P.violated("PA-02")) == expected


def test_pa02_not_after_target_reached(P):
    _closed_trade(P, 0.01, target_reached=True)
    assert not P.violated("PA-02")


def test_pa03_buy_after_stop_once(P):
    P.buy("DEV001", 1, 10_000, D1 + timedelta(minutes=5))
    P.conn.execute("UPDATE trades SET stop_reached_at = ?", ((D1 + timedelta(minutes=30)).isoformat(),))
    P.buy("DEV001", 1, 9_000, D1 + timedelta(minutes=40))
    P.buy("DEV001", 1, 8_500, D1 + timedelta(minutes=50))
    assert len(P.violated("PA-03")) == 1


@pytest.mark.parametrize("price, watched_at, expected", [
    (10_500, None, True),                       # +5.0% 정확히, 미등록 → 감점
    (10_490, None, False),                      # +4.9%
    (10_500, "08:59:59", False),                # 장 시작 전 등록 → 계획 종목
    (10_500, "09:10:00", True),                 # 장중 등록 → 계획 외
])
def test_pa04_rise_and_watchlist(P, price, watched_at, expected):
    if watched_at:
        P.conn.execute("INSERT INTO watchlist (project_id, stock_code, added_at) VALUES (?, 'DEV001', ?)",
                       (P.pid, f"2024-03-04T{watched_at}"))
    P.buy("DEV001", 1, price, D1 + timedelta(minutes=20), prev=10_000)
    assert bool(P.violated("PA-04")) == expected


# ------------------------------------------------------------------ BC
def _loss_trades(P, pnls, last_close):
    """끝난 거래들을 직접 넣는다. 마지막 거래가 last_close 에 끝난다."""
    for i, pnl in enumerate(pnls):
        closed = last_close - timedelta(minutes=(len(pnls) - 1 - i) * 5)
        P.conn.execute(
            "INSERT INTO trades (account_id, stock_code, status, opened_at, closed_at, realized_pnl, realized_return) "
            "VALUES (?, 'DEV006', 'closed', ?, ?, ?, ?)",
            (P.acc, (closed - timedelta(minutes=4)).isoformat(), closed.isoformat(), pnl, pnl / 1e6))


@pytest.mark.parametrize("after, expected", [(timedelta(minutes=10), True), (timedelta(minutes=10, seconds=1), False)])
def test_bc02_window_includes_10_minutes(P, after, expected):
    last = D1 + timedelta(hours=1)
    _loss_trades(P, [-1, -1, -1], last)
    P.buy("DEV001", 1, 10_000, last + after)
    assert bool(P.violated("BC-02")) == expected


def test_bc02_streak_broken_by_breakeven(P):
    last = D1 + timedelta(hours=1)
    _loss_trades(P, [-1, -1, 0, -1, -1], last)                    # 0원은 연속을 끊음 → 마지막 연속 2회
    P.buy("DEV001", 1, 10_000, last + timedelta(minutes=1))
    assert not P.violated("BC-02")


def test_bc02_once_per_window_new_window_after_next_loss(P):
    last = D1 + timedelta(hours=1)
    _loss_trades(P, [-1, -1, -1], last)
    P.buy("DEV001", 1, 10_000, last + timedelta(minutes=1))
    P.buy("DEV003", 1, 10_000, last + timedelta(minutes=2))       # 같은 감시 구간 → 한 번만
    assert len(P.violated("BC-02")) == 1
    _loss_trades(P, [-1], last + timedelta(minutes=30))           # 4회째 손실 → 새 감시 구간
    P.buy("DEV004", 1, 10_000, last + timedelta(minutes=35))
    assert len(P.violated("BC-02")) == 2


@pytest.mark.parametrize("minutes, expected", [(3, True), (3.02, False)])
def test_bc03_two_short_trades_same_day(P, minutes, expected):
    for i in range(2):
        opened = D1 + timedelta(hours=1 + i)
        closed = opened + timedelta(minutes=minutes)
        tid = P.conn.execute(
            "INSERT INTO trades (account_id, stock_code, status, opened_at, closed_at, realized_pnl, realized_return) "
            "VALUES (?, 'DEV001', 'closed', ?, ?, 0, 0)", (P.acc, opened.isoformat(), closed.isoformat())).lastrowid
        rules.on_trade_closed(P.conn, tid, closed)
    v = P.violated("BC-03")
    assert bool(v) == expected
    if expected:
        assert v[0]["target_date"] == "2024-03-04"


def _orders_on(P, day, n):
    for i in range(n):
        t = f"{day}T10:{i:02d}:00"
        oid = P.conn.execute(
            "INSERT INTO orders (account_id, stock_code, side, order_type, quantity, status, submitted_at) "
            "VALUES (?, 'DEV001', 'buy', 'market', 1, 'filled', ?)", (P.acc, t)).lastrowid
        P.conn.execute("INSERT INTO executions (order_id, quantity, price, executed_at) VALUES (?, 1, 100, ?)", (oid, t))


DAYS = ["2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07", "2024-03-08", "2024-03-11"]


def _close(P, n):
    return rules.on_market_close(P.conn, P.pid, n, datetime.fromisoformat(f"{DAYS[n - 1]}T15:30:00"))


def test_bc01_baseline_hold_then_reevaluate(P):
    for n, count in enumerate([1, 1, 1, 1, 6], start=1):           # 일평균 2건
        _orders_on(P, DAYS[n - 1], count)
        _close(P, n)
        if n < 5:
            assert {r["result"] for r in P.evals("BC-01")} == {"hold"}
    results = {r["target_date"]: r["result"] for r in P.evals("BC-01")}
    assert results["2024-03-08"] == "violation"                    # 6 ≥ 2 × 3
    assert [results[d] for d in DAYS[:4]] == ["void"] * 4
    assert P.conn.execute("SELECT baseline_daily_avg FROM projects WHERE project_id=?", (P.pid,)).fetchone()[0] == 2


@pytest.mark.parametrize("count, expected", [(6, True), (5, False)])
def test_bc01_after_baseline(P, count, expected):
    for n in range(1, 6):
        _orders_on(P, DAYS[n - 1], 2)                              # 일평균 2건
        _close(P, n)
    _orders_on(P, DAYS[5], count)
    _close(P, 6)
    assert bool([r for r in P.violated("BC-01") if r["target_date"] == DAYS[5]]) == expected


def test_bc01_zero_average_terminates_project(P):
    for n in range(1, 6):
        r = _close(P, n)
    assert r["terminated"]
    p = P.conn.execute("SELECT status, end_reason FROM projects WHERE project_id=?", (P.pid,)).fetchone()
    assert tuple(p) == ("terminated", "체결량 미달")


# ------------------------------------------------------------------ MC
@pytest.mark.parametrize("rsi, result", [(75.0, "violation"), (74.9, None), (None, "hold")])
def test_mc01_rsi(P, monkeypatch, rsi, result):
    monkeypatch.setattr(indicators, "rsi_with_price", lambda *a: rsi)
    P.buy("DEV001", 1, 10_000, D1 + timedelta(minutes=10))
    rows = P.evals("MC-01")
    assert (rows[0]["result"] if rows else None) == result


@pytest.mark.parametrize("price, expected", [(12_000, True), (11_990, False)])
def test_mc03_ma_120(P, monkeypatch, price, expected):
    monkeypatch.setattr(indicators, "ma_and_avg_volume", lambda *a: (10_000.0, 1.0))
    P.buy("DEV001", 1, price, D1 + timedelta(minutes=10))
    assert bool(P.violated("MC-03")) == expected


@pytest.mark.parametrize("cum, expected", [(99_999, True), (100_000, False)])
def test_mc02_volume_vs_elapsed(P, monkeypatch, cum, expected):
    # 20일 평균 100만 주, 장 경과 50% → 기대 50만 주, 기준 20% = 10만 주 미만
    monkeypatch.setattr(indicators, "ma_and_avg_volume", lambda *a: (1e12, 1_000_000.0))
    P.feed.cum_volume = cum
    P.buy("DEV001", 1, 10_000, D1 + timedelta(seconds=11_700))
    assert bool(P.violated("MC-02")) == expected


# ------------------------------------------------------------------ 공통
def test_hold_upgrades_to_violation_but_violation_stays(P):
    t = D1 + timedelta(minutes=1)
    assert rules.record(P.conn, P.pid, "MC-01", t, "hold", trade_id=None, date="x", hold_reason="r") == "new"
    assert rules.record(P.conn, P.pid, "MC-01", t, "violation", date="x") == "updated"
    assert rules.record(P.conn, P.pid, "MC-01", t, "void", date="x") == "kept"


def test_scores_only_published_until_close(P):
    P.buy("DEV001", 161, 50_000, D1 + timedelta(minutes=10))       # RM-01 + RM-02 = -45
    assert scores_for_project(P.conn, P.pid, published_only=True)["RM"] == 100
    rules.publish(P.conn, P.pid)
    assert scores_for_project(P.conn, P.pid, published_only=True)["RM"] == 55
