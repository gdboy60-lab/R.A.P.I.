"""시세 공급 (명세서 9-20, NFR-07).

체결 엔진과 규칙 엔진은 PriceFeed.quote() 만 부른다. 시세가 어디서 오는지는 모른다.

ReplayFeed: 그날 실제 일봉(시가·고가·저가·종가·거래량)에 맞춰 장중 흐름을 만든다.
- 시가에서 시작해 고가·저가를 거쳐 종가로 끝난다.
  (양봉: 시가 → 저가 → 고가 → 종가, 음봉: 시가 → 고가 → 저가 → 종가)
- 같은 종목·같은 날이면 항상 같은 흐름이 나온다 (판정 일관성, NFR-01).
- 가격은 호가 단위로 맞추고, 고가·저가를 넘지 않는다.
- 누적 거래량은 장 초반에 몰리도록 경과 비율의 0.7 제곱에 비례해서 그날 거래량까지 늘어난다.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from typing import Optional

# 양봉이면 저가를 30% 지점, 고가를 75% 지점에서 찍는다 (음봉은 반대)
WAYPOINT_AT = (0.0, 0.30, 0.75, 1.0)
NOISE_SHARE = 0.06          # 흔들림 폭 = (고가 - 저가) × 6%
KNOTS_PER_DAY = 78          # 5분마다 흔들림 기준점 (6.5시간 × 12)


def tick_size(price: int) -> int:
    """한국거래소 주식 호가 단위 (2023년 개편 기준)."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_tick(price: float) -> int:
    t = tick_size(int(price))
    return int(round(price / t) * t)


def _noise(key: str) -> float:
    """문자열로 정해지는 -1 ~ 1 사이 값 (난수처럼 보이지만 항상 같음)."""
    h = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF * 2 - 1


@dataclass(frozen=True)
class DayBar:
    stock_code: str
    date: date
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass(frozen=True)
class Quote:
    stock_code: str
    price: int              # 현재가
    prev_close: Optional[int]
    cum_volume: int         # 당일 누적 거래량
    elapsed_ratio: float    # 장 경과 비율 (MC-02)


class IntradayPath:
    """하루치 장중 가격 흐름. 장 경과 비율(0~1)을 넣으면 가격이 나온다."""

    def __init__(self, bar: DayBar):
        self.bar = bar
        if bar.close >= bar.open:
            values = (bar.open, bar.low, bar.high, bar.close)
        else:
            values = (bar.open, bar.high, bar.low, bar.close)
        self.points = list(zip(WAYPOINT_AT, values))
        self.amp = (bar.high - bar.low) * NOISE_SHARE

    def price(self, r: float) -> int:
        r = min(max(r, 0.0), 1.0)
        for (r0, v0), (r1, v1) in zip(self.points, self.points[1:]):
            if r <= r1:
                break
        local = 0.0 if r1 == r0 else (r - r0) / (r1 - r0)
        base = v0 + (v1 - v0) * local
        # 기준점 사이를 잇는 흔들림. 고가·저가 지점에서는 0이 되도록 sin 모양으로 줄인다.
        k = r * KNOTS_PER_DAY
        k0 = math.floor(k)
        tag = f"{self.bar.stock_code}|{self.bar.date}"
        n = _noise(f"{tag}|{k0}") * (1 - (k - k0)) + _noise(f"{tag}|{k0 + 1}") * (k - k0)
        p = base + n * self.amp * math.sin(math.pi * local)
        return min(max(round_tick(p), self.bar.low), self.bar.high)

    def cum_volume(self, r: float) -> int:
        r = min(max(r, 0.0), 1.0)
        return int(self.bar.volume * r ** 0.7)


class ReplayFeed:
    """daily_prices 를 읽어 장중 시세를 만드는 공급원."""

    def __init__(self, conn):
        self.conn = conn
        self._bar = lru_cache(maxsize=512)(self._load_bar)
        self._prev = lru_cache(maxsize=512)(self._load_prev_close)
        self._session = lru_cache(maxsize=64)(self._load_session)
        self._path = lru_cache(maxsize=512)(lambda bar: IntradayPath(bar))

    # ---- 읽기
    def _load_bar(self, code: str, d: date) -> Optional[DayBar]:
        r = self.conn.execute(
            "SELECT open, high, low, close, volume FROM daily_prices WHERE stock_code = ? AND date = ?",
            (code, d.isoformat()),
        ).fetchone()
        return DayBar(code, d, *r) if r else None

    def _load_prev_close(self, code: str, d: date) -> Optional[int]:
        r = self.conn.execute(
            "SELECT close FROM daily_prices WHERE stock_code = ? AND date < ? ORDER BY date DESC LIMIT 1",
            (code, d.isoformat()),
        ).fetchone()
        return r[0] if r else None

    def _load_session(self, d: date) -> tuple[datetime, datetime]:
        r = self.conn.execute(
            "SELECT open_time, close_time FROM market_calendar WHERE date = ? AND is_open = 1",
            (d.isoformat(),),
        ).fetchone()
        o, c = (time.fromisoformat(r[0]), time.fromisoformat(r[1])) if r else (time(9), time(15, 30))
        return datetime.combine(d, o), datetime.combine(d, c)

    # ---- 공개
    def elapsed_ratio(self, t: datetime) -> float:
        open_at, close_at = self._session(t.date())
        return min(max((t - open_at).total_seconds() / (close_at - open_at).total_seconds(), 0.0), 1.0)

    def quote(self, code: str, t: datetime) -> Optional[Quote]:
        """그 시각의 시세. 그날 일봉이 없으면 None (거래정지 등, 판정은 보류)."""
        bar = self._bar(code, t.date())
        if bar is None:
            return None
        path = self._path(bar)
        r = self.elapsed_ratio(t)
        return Quote(code, path.price(r), self._prev(code, t.date()), path.cum_volume(r), r)
