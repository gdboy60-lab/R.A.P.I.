"""장 마감 처리 순서 (명세서 10-2).

1. 미체결 지정가 자동 취소
2. RM-03 판정
3. BC-01 판정 (기준 기간 중이면 보류)
4. 5일차라면 BC-01 일평균 계산과 재평가, 0회면 조기 종료
5. (AI 일일 피드백 생성 - 3주차) 그날 판정 결과 공개
6. 마지막 거래일이면(또는 조기 종료면) 최종 점수 확정
"""
from __future__ import annotations

from .engine.clock import SimClock
from .engine.trading import market_close
from .rules import engine as rules


def close_day(conn, project_id: int, clock: SimClock) -> dict:
    t = clock.sim_now
    cancelled = market_close(conn, project_id, t)                               # 1
    result = rules.on_market_close(conn, project_id, clock.day_number, t)       # 2·3·4
    published = rules.publish(conn, project_id)                                 # 5
    final = None
    if clock.is_last_day or result["terminated"]:                               # 6
        final = rules.finalize(conn, project_id, t)
    return {"cancelled": cancelled, "terminated": result["terminated"],
            "published": len(published), "final": final}
