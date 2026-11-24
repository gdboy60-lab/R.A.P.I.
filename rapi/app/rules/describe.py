"""판정 기록을 사람이 읽는 문장으로 바꾼다 (장 마감 결과 화면, AI 피드백 입력에 사용).

종목명 뒤 조사(을/를, 이/가)가 받침에 따라 달라지므로 '종목명 · 내용' 형태로 쓴다.
"""
from __future__ import annotations

import json

from .catalog import RULES


def _hm(ts) -> str:
    return str(ts)[11:16] if ts else "?"


def describe(row, names: dict) -> str:
    rid, res = row["rule_id"], row["result"]
    ev = json.loads(row["evidence"] or "{}")
    name = names.get(ev.get("stock_code"), ev.get("stock_code", ""))
    rule = RULES[rid]
    if res == "hold":
        return f"{rule.name}: 판정 보류 ({row['hold_reason']})"
    if res == "void":
        return f"{rule.name}: 기준을 넘지 않아 감점 없음"
    if rid in ("RM-01", "RM-02"):
        limit = "50%" if rid == "RM-01" else "80%"
        return f"{name} · 총자산의 {ev['ratio']:.0%}를 한 번에 매수했어요 (기준 {limit} 초과)"
    if rid == "RM-03":
        return f"{name} · {_hm(ev['stop_reached_at'])}에 손절가 {ev['stop_price']:,}원에 닿은 뒤 장 마감까지 보유했어요"
    if rid == "RM-04":
        return f"{name} · 목표 수량 {ev['target_qty']}주를 주문 한 번({ev['qty']}주)에 모두 매수했어요"
    if rid == "PA-01":
        return f"{name} · 매수할 때 {', '.join(ev['missing'])}이(가) 비어 있었어요"
    if rid == "PA-02":
        return (f"{name} · 목표가 {ev['target_price']:,}원에 닿기 전에 "
                f"{ev['realized_return']:+.2%} 수익으로 팔았어요 (0% 초과 2% 이하)")
    if rid == "PA-03":
        return f"{name} · {_hm(ev['stop_reached_at'])}에 손절가에 닿은 뒤 추가로 매수했어요"
    if rid == "PA-04":
        return f"{name} · 장 시작 전 관심 종목이 아니었는데, 전일보다 {ev['rise']:+.1%} 오른 상태에서 매수했어요"
    if rid == "BC-01":
        return f"하루 체결 주문 {ev['orders']}건 · 평소(하루 {ev['baseline_daily_avg']:.1f}건)의 3배 이상이에요"
    if rid == "BC-02":
        return f"{name} · {ev['loss_streak']}번 연속 손실로 끝난 거래({_hm(ev['watch_started_at'])}) 뒤 10분 안에 다시 매수했어요"
    if rid == "BC-03":
        return f"3분 안에 끝난 거래가 하루에 {len(ev['short_trades'])}번 있었어요"
    if rid == "MC-01":
        return f"{name} · RSI {ev['rsi_14']}인 과열 구간에서 매수했어요 (기준 75 이상)"
    if rid == "MC-02":
        return f"{name} · 거래량이 이 시각 평소의 {ev['share']:.0%} 수준일 때 매수했어요 (기준 20% 미만)"
    if rid == "MC-03":
        return f"{name} · 20일 평균가보다 {ev['ratio'] - 1:.0%} 높은 가격에 매수했어요 (기준 20% 이상)"
    return rule.name
