"""개발용 예시 데이터. 실제 리플레이 데이터가 들어오기 전에 화면·체결 엔진을 개발하기 위한 것.

- 리플레이 기간 1개: 평일 10일, 09:00~15:30
- 가상 종목 6개 (시안과 같은 가상 이름)
- 가상 일봉: 기간 앞 270개장일 + 기간 10일. 항상 같은 값이 나온다.
  · 푸른제약: 3일차에 크게 오름 (PA-04, MC 규칙 확인용)
  · 누리소프트: 기간 내내 내림 (손절 확인용)

실행:  python -m app.seed_dev   (서버를 처음 켤 때 rapi.db 가 없으면 자동 실행됨)
B가 공공데이터를 적재하면 이 데이터는 쓰지 않는다.
"""
from __future__ import annotations

from datetime import date, timedelta

from .db import DEFAULT_DB_PATH, connect, init_db
from .engine.prices import _noise, round_tick

DEV_STOCKS = [
    # 코드, 이름, 시작가, 기간 중 하루 평균 등락(%), 거래량
    ("DEV001", "한빛전자", 62_000, 0.3, 1_200_000),
    ("DEV002", "누리소프트", 19_500, -1.2, 800_000),
    ("DEV003", "바다물산", 9_500, 0.2, 2_000_000),
    ("DEV004", "새벽에너지", 31_000, 0.0, 600_000),
    ("DEV005", "푸른제약", 38_000, 0.4, 900_000),
    ("DEV006", "한길건설", 8_800, -0.1, 1_500_000),
]
LOOKBACK_DAYS = 270
SPIKE = ("DEV005", 3, 0.07)      # 푸른제약 3일차 +7%


def weekdays_before(end: date, n: int) -> list[date]:
    out, d = [], end - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def weekdays_from(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_bars(code: str, start_price: int, drift: float, volume: int,
              lookback: list[date], period: list[date]) -> list[tuple]:
    rows, close = [], float(start_price)
    for i, d in enumerate(lookback + period):
        in_period = i >= len(lookback)
        day_no = i - len(lookback) + 1
        move = _noise(f"{code}|move|{d}") * 0.018 + (drift / 100 if in_period else 0.0005)
        if (code, day_no) == SPIKE[:2]:
            move = SPIKE[2]
        o = close * (1 + _noise(f"{code}|gap|{d}") * 0.004 + (move * 0.5 if (code, day_no) == SPIKE[:2] else 0))
        c = close * (1 + move)
        spread = abs(_noise(f"{code}|range|{d}")) * 0.012 + 0.004
        h = max(o, c) * (1 + spread)
        l = min(o, c) * (1 - spread)
        o, h, l, c = (round_tick(x) for x in (o, h, l, c))
        h, l = max(h, o, c), min(l, o, c)
        v = int(volume * (1 + _noise(f"{code}|vol|{d}") * 0.4) * (2.5 if (code, day_no) == SPIKE[:2] else 1))
        rows.append((code, d.isoformat(), o, h, l, c, v))
        close = c
    return rows


def seed(conn, start: date = date(2024, 3, 4), n_days: int = 10) -> int:
    period = weekdays_from(start, n_days)
    lookback = weekdays_before(start, LOOKBACK_DAYS)
    conn.executemany(
        "INSERT OR IGNORE INTO market_calendar (date, is_open, open_time, close_time) "
        "VALUES (?, 1, '09:00:00', '15:30:00')",
        [(x.isoformat(),) for x in lookback + period],
    )
    for code, _, price, drift, vol in DEV_STOCKS:
        conn.executemany(
            "INSERT OR IGNORE INTO daily_prices (stock_code, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            make_bars(code, price, drift, vol, lookback, period),
        )
    cur = conn.execute(
        "INSERT INTO replay_periods (label, start_date, end_date, trading_days, description) "
        "VALUES ('개발용 예시 기간', ?, ?, ?, '가상 시세. 3일차 푸른제약 급등, 누리소프트 하락')",
        (period[0].isoformat(), period[-1].isoformat(), n_days),
    )
    period_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO replay_stocks (period_id, stock_code, display_name) VALUES (?, ?, ?)",
        [(period_id, code, name) for code, name, *_ in DEV_STOCKS],
    )
    conn.commit()
    return period_id


if __name__ == "__main__":
    fresh = not DEFAULT_DB_PATH.exists()
    c = connect()
    if fresh:
        init_db(c)
    pid = seed(c)
    print(f"개발용 리플레이 기간 {pid}번을 넣었어요: {DEFAULT_DB_PATH}")
