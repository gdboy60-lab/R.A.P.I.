"""MC 규칙에 쓰는 지표 (명세서 6장).

- 기준선(20일 이동평균, 20일 평균 거래량): 오늘 제외, 직전 20개장일. 하루 동안 고정.
- RSI(14): 와일더 방식. 직전 250개장일 종가 + 오늘은 매수 체결가를 현재 값으로 넣는다.
- 데이터가 모자라면 None → 그 규칙은 평가 보류.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

RSI_PERIOD = 14
RSI_LOOKBACK = 250
BASELINE_DAYS = 20


def history(conn, code: str, before: date, n: int) -> list[tuple[int, int]]:
    """오늘(before) 이전 n개장일의 (종가, 거래량), 오래된 것부터."""
    rows = conn.execute(
        "SELECT close, volume FROM daily_prices WHERE stock_code = ? AND date < ? ORDER BY date DESC LIMIT ?",
        (code, before.isoformat(), n),
    ).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


def ma_and_avg_volume(conn, code: str, today: date) -> tuple[Optional[float], Optional[float]]:
    h = history(conn, code, today, BASELINE_DAYS)
    if len(h) < BASELINE_DAYS:
        return None, None
    return sum(c for c, _ in h) / BASELINE_DAYS, sum(v for _, v in h) / BASELINE_DAYS


def wilder_rsi(closes: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def rsi_with_price(conn, code: str, today: date, price: int) -> Optional[float]:
    closes = [c for c, _ in history(conn, code, today, RSI_LOOKBACK)]
    if len(closes) < BASELINE_DAYS:          # 상장 20개장일 미만은 보류
        return None
    return wilder_rsi(closes + [price])
