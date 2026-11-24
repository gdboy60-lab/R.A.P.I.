"""체결 엔진 (명세서 9-8)."""
import math
from datetime import datetime, timedelta
import pytest
from app import settings
from app.engine.prices import ReplayFeed, tick_size
from app.engine.trading import OrderError, TickWatcher, cancel_order, market_close, place_order
from app.projects import create_guest, create_project

T = datetime(2024, 3, 4, 9, 30)     # 1일차 09:30


@pytest.fixture
def env(conn, period_id):
    pid = create_project(conn, create_guest(conn), period_id)
    return conn, ReplayFeed(conn), pid


def cash(conn, pid):
    return conn.execute("SELECT cash FROM accounts WHERE project_id=?", (pid,)).fetchone()[0]


def test_replay_path_matches_daily_bar(conn, period_id):
    feed = ReplayFeed(conn)
    o, h, l, c = conn.execute("SELECT open, high, low, close FROM daily_prices "
                              "WHERE stock_code='DEV001' AND date='2024-03-04'").fetchone()
    prices = [feed.quote("DEV001", datetime(2024, 3, 4, 9) + timedelta(seconds=s)).price for s in range(0, 23401, 30)]
    assert prices[0] == o and prices[-1] == c and min(prices) == l and max(prices) == h
    assert all(p % tick_size(p) == 0 for p in prices)
    assert feed.quote("DEV001", T).price == ReplayFeed(conn).quote("DEV001", T).price   # 항상 같음


def test_market_buy_fills_now_with_fee_and_snapshot(env):
    conn, feed, pid = env
    price = feed.quote("DEV001", T).price
    r = place_order(conn, feed, pid, T, "running", "DEV001", "buy", "market", 10,
                    plan={"reason": "테스트", "target_price": 99999, "stop_price": 1, "target_qty": 20})
    assert r["filled"]
    fee = math.floor(price * 10 * settings.BUY_FEE_RATE)
    assert cash(conn, pid) == 10_000_000 - price * 10 - fee
    tr = conn.execute("SELECT * FROM trades").fetchone()
    assert tr["status"] == "open" and tr["opened_at"] == T.isoformat() and tr["plan_id"]
    snap = conn.execute("SELECT * FROM execution_snapshots").fetchone()
    assert snap["prev_close"] and snap["ma_20d"] and snap["rsi_14"] is not None


def test_rejects_when_not_enough_cash_or_shares_or_closed(env):
    conn, feed, pid = env
    with pytest.raises(OrderError, match="금액"):
        place_order(conn, feed, pid, T, "running", "DEV001", "buy", "market", 10_000)
    with pytest.raises(OrderError, match="수량"):
        place_order(conn, feed, pid, T, "running", "DEV001", "sell", "market", 1)
    with pytest.raises(OrderError, match="장이"):
        place_order(conn, feed, pid, T, "pre_open", "DEV001", "buy", "market", 1)


def test_open_limit_reserves_cash(env):
    conn, feed, pid = env
    place_order(conn, feed, pid, T, "running", "DEV001", "buy", "limit", 100, limit_price=50_000)   # 아래 가격, 대기
    with pytest.raises(OrderError, match="금액"):
        place_order(conn, feed, pid, T, "running", "DEV003", "buy", "limit", 1000, limit_price=6_000)


def _low_and_when(conn, feed, code):
    low = conn.execute("SELECT low FROM daily_prices WHERE stock_code=? AND date='2024-03-04'", (code,)).fetchone()[0]
    return low


def test_limit_buy_fills_at_limit_when_reached_during_skip(env):
    conn, feed, pid = env
    low = _low_and_when(conn, feed, "DEV001")
    place_order(conn, feed, pid, T.replace(hour=9, minute=0, second=1), "running", "DEV001", "buy", "limit",
                10, limit_price=low)
    watcher = TickWatcher(conn, feed, pid)
    t = datetime(2024, 3, 4, 9, 0, 2)
    while t <= datetime(2024, 3, 4, 15, 30):
        watcher(t)
        t += timedelta(seconds=1)
    e = conn.execute("SELECT price FROM executions").fetchone()
    assert e and e[0] == low                                       # 지정가로 체결


def test_sell_all_closes_trade_with_net_pnl(env):
    conn, feed, pid = env
    place_order(conn, feed, pid, T, "running", "DEV003", "buy", "market", 100)
    t2 = T + timedelta(hours=3)
    place_order(conn, feed, pid, t2, "running", "DEV003", "sell", "market", 100)
    tr = conn.execute("SELECT * FROM trades").fetchone()
    rows = conn.execute("SELECT o.side, e.price, e.quantity, e.fee, e.tax FROM executions e JOIN orders o USING(order_id)").fetchall()
    buy = sum(p * q + f for s, p, q, f, _ in rows if s == "buy")
    sell = sum(p * q - f - x for s, p, q, f, x in rows if s == "sell")
    assert tr["status"] == "closed" and tr["closed_at"] == t2.isoformat()
    assert tr["realized_pnl"] == sell - buy and abs(tr["realized_return"] - (sell / buy - 1)) < 1e-12
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    assert cash(conn, pid) == 10_000_000 + tr["realized_pnl"]


def test_second_buy_follows_first_plan(env):
    conn, feed, pid = env
    place_order(conn, feed, pid, T, "running", "DEV001", "buy", "market", 5, plan={"reason": "처음"})
    place_order(conn, feed, pid, T + timedelta(minutes=5), "running", "DEV001", "buy", "market", 5, plan={"reason": "다른"})
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(DISTINCT trade_id) FROM orders").fetchone()[0] == 1


def test_stop_touch_recorded_once(env):
    conn, feed, pid = env
    q = feed.quote("DEV002", T).price
    place_order(conn, feed, pid, T, "running", "DEV002", "buy", "market", 10, plan={"stop_price": q + 10_000})
    w = TickWatcher(conn, feed, pid)
    w(T + timedelta(seconds=1)); w(T + timedelta(seconds=2))
    assert conn.execute("SELECT COUNT(*) FROM price_touches WHERE kind='stop'").fetchone()[0] == 1
    assert conn.execute("SELECT stop_reached_at FROM trades").fetchone()[0] == (T + timedelta(seconds=1)).isoformat()


def test_market_close_cancels_open_orders(env):
    conn, feed, pid = env
    oid = place_order(conn, feed, pid, T, "running", "DEV001", "buy", "limit", 1, limit_price=1_000)["order_id"]
    assert market_close(conn, pid, datetime(2024, 3, 4, 15, 30)) == 1
    o = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert tuple(o) == ("cancelled", "market_close")
    with pytest.raises(OrderError):
        cancel_order(conn, pid, oid, T)
