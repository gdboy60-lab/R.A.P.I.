"""AI 일일 피드백 (명세서 11장).

흐름
1. build_input  : 백엔드가 그날 판정·누적 요약·어제 제안 점검·거래 요약을 계산한다 (AI는 이것만 본다)
2. call_claude  : Claude에게 정해진 5칸 JSON을 쓰게 한다
3. validate     : 적힌 판정 번호가 실제로 있는지, 종목 추천이 없는지 등을 백엔드가 검사한다
4. 실패하면 최대 2번 다시 만든다. 그래도 안 되거나 API 키가 없으면 fallback(규칙 기반 기본 피드백)으로 채운다
5. 입력과 출력을 ai_feedbacks 에 함께 저장한다 (NFR-06)

출력 5칸 (11-4)
  summary          한 줄 요약
  causes           핵심 원인 최대 2개 [{text, eval_ids}]   ← 근거 번호 필수
  pattern          반복 패턴 {text, eval_ids} 또는 null    ← 근거 번호 필수
  yesterday_check  어제 제안 점검 (백엔드가 판단할 수 있을 때만) 또는 null
  tomorrow_action  내일 해볼 행동 하나 (종목 추천 금지)
  action_rule      그 행동이 겨냥하는 규칙 ID (다음 날 실행 여부 판단에 씀) 또는 null
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import date
from typing import Callable, Optional

from .. import settings
from ..rules.catalog import RULES, area_scores, scores_for_project
from ..rules.describe import describe

API_URL = "https://api.anthropic.com/v1/messages"

# 규칙 기반 기본 피드백에서 쓰는 '내일 해볼 행동'
ACTION_FOR_RULE = {
    "RM-01": "주문 전에 금액이 총자산의 절반을 넘지 않는지 먼저 확인해 보세요",
    "RM-02": "한 번에 총자산 대부분을 쓰지 말고, 금액을 나눠서 들어가 보세요",
    "RM-03": "손절가에 닿으면 시계를 멈추고 바로 매도 주문을 넣어 보세요",
    "RM-04": "목표 수량을 두세 번에 나눠서 사 보세요",
    "PA-01": "매수 전에 사유·목표가·손절가 세 칸을 먼저 채워 보세요",
    "PA-02": "목표가에 닿기 전에는 작은 수익에 서둘러 팔지 말고 계획을 지켜보세요",
    "PA-03": "손절가에 닿은 종목은 더 사지 말고 계획대로 정리해 보세요",
    "PA-04": "장 시작 전에 살 종목을 관심 종목에 넣고, 그 안에서만 사 보세요",
    "BC-01": "하루 주문 횟수를 평소 수준으로 줄여 보세요",
    "BC-02": "손실이 이어지면 시계를 멈추고 10분 이상 쉬었다가 다시 보세요",
    "BC-03": "사고 3분 안에 파는 매매를 하루 한 번 이하로 줄여 보세요",
    "MC-01": "크게 오른 종목은 바로 사지 말고 하루 더 지켜본 뒤 정해 보세요",
    "MC-02": "거래량이 평소보다 아주 적을 때는 매수를 미뤄 보세요",
    "MC-03": "20일 평균가보다 크게 오른 가격에서는 따라 사지 말아 보세요",
}
DEFAULT_ACTION = "오늘처럼 매수 전에 사유·목표가·손절가를 먼저 채워 보세요"

SYSTEM_PROMPT = """너는 초보 투자자의 가상 매매 습관을 돌아보게 돕는 코치야.
백엔드가 계산한 JSON 데이터만 보고, 정해진 JSON 형식으로 오늘의 피드백을 써.

반드시 지킬 것
- 데이터에 없는 사실, 숫자, 점수를 만들지 마. 점수는 data.scores 값만 그대로 써.
- 감점 원인과 반복 패턴에는 근거가 된 판정 번호(eval_id)를 eval_ids 에 적어. 데이터에 있는 번호만 써.
- 특정 종목을 사거나 팔라고 권하지 마. tomorrow_action 에는 종목 이름을 쓰지 마.
- 사용자의 감정이나 성격을 추측하지 마. 기록된 행동만 말해.
- data.yesterday 가 null 이면 yesterday_check 는 null. 있으면 그 결과(result)를 바꾸지 말고 한 문장으로 써.
- 감점이 없는 날은 causes 를 빈 목록으로 두고, summary 에서 data.trade_summary 를 근거로 잘한 점을 구체적으로 짚어.
- 말투는 친근한 존댓말(~해요). 각 문장은 80자 이내.

출력은 아래 JSON 하나만. 다른 글자는 쓰지 마.
{"summary": "한 줄 요약",
 "causes": [{"text": "핵심 원인", "eval_ids": [번호]}],
 "pattern": {"text": "반복 패턴", "eval_ids": [번호]} 또는 null,
 "yesterday_check": "어제 제안 점검" 또는 null,
 "tomorrow_action": "구체적인 행동 하나",
 "action_rule": "그 행동이 줄이려는 규칙 ID (예: RM-03)" 또는 null}
causes 는 감점이 큰 순서로 최대 2개."""


# ------------------------------------------------------------------ 1. 입력
def _names(conn, project_id: int) -> dict:
    return dict(conn.execute(
        "SELECT s.stock_code, s.display_name FROM replay_stocks s JOIN projects p USING (period_id) "
        "WHERE p.project_id = ?", (project_id,)).fetchall())


def _eval_date(row) -> str:
    return (row["updated_at"] or row["evaluated_at"])[:10]


def build_input(conn, project_id: int, day_number: int, day: str) -> dict:
    names = _names(conn, project_id)
    all_rows = conn.execute(
        "SELECT * FROM rule_evaluations WHERE project_id = ? AND published = 1 ORDER BY eval_id", (project_id,)
    ).fetchall()
    today_rows = [r for r in all_rows if _eval_date(r) == day and r["result"] in ("violation", "hold")]
    today = [{"eval_id": r["eval_id"], "rule_id": r["rule_id"], "rule_name": RULES[r["rule_id"]].name,
              "result": r["result"], "penalty": r["penalty"], "detail": describe(r, names)}
             for r in sorted(today_rows, key=lambda r: (-r["penalty"], r["eval_id"]))]

    # 누적 요약: 규칙별 지금까지 위반 횟수, 오늘까지 며칠 연속 위반했는지
    days = [r[0] for r in conn.execute(
        "SELECT c.date FROM market_calendar c JOIN projects p ON c.date BETWEEN p.start_date AND ? "
        "WHERE p.project_id = ? AND c.is_open = 1 ORDER BY c.date", (day, project_id))]
    history = []
    for rid in RULES:
        v = [r for r in all_rows if r["rule_id"] == rid and r["result"] == "violation"]
        if not v:
            continue
        vdays = {_eval_date(r) for r in v}
        streak = 0
        for d in reversed(days):
            if d not in vdays:
                break
            streak += 1
        history.append({"rule_id": rid, "rule_name": RULES[rid].name, "total_violations": len(v),
                        "consecutive_days": streak, "eval_ids": [r["eval_id"] for r in v]})

    before = area_scores([(r["area"], r["penalty"]) for r in all_rows
                          if r["result"] == "violation" and _eval_date(r) < day])
    after = scores_for_project(conn, project_id, published_only=True)

    account_id = conn.execute("SELECT account_id FROM accounts WHERE project_id = ?", (project_id,)).fetchone()[0]
    trade_summary = _trade_summary(conn, account_id, day)
    return {
        "day_number": day_number,
        "total_days": conn.execute("SELECT total_trading_days FROM projects WHERE project_id = ?",
                                   (project_id,)).fetchone()[0],
        "today": today,
        "history": history,
        "scores": {"before": {k: round(v, 1) for k, v in before.items()},
                   "after": {k: round(v, 1) for k, v in after.items()}},
        "yesterday": _yesterday_check(conn, project_id, day_number, today_rows, trade_summary),
        "trade_summary": trade_summary,
        "stock_names": sorted(set(names.values())),
    }


def _trade_summary(conn, account_id: int, day: str) -> dict:
    buys = conn.execute(
        "SELECT COUNT(DISTINCT e.order_id) FROM executions e JOIN orders o USING (order_id) "
        "WHERE o.account_id = ? AND o.side = 'buy' AND substr(e.executed_at, 1, 10) = ?", (account_id, day)
    ).fetchone()[0]
    sells = conn.execute(
        "SELECT COUNT(DISTINCT e.order_id) FROM executions e JOIN orders o USING (order_id) "
        "WHERE o.account_id = ? AND o.side = 'sell' AND substr(e.executed_at, 1, 10) = ?", (account_id, day)
    ).fetchone()[0]
    new = conn.execute(
        "SELECT p.reason, p.target_price, p.stop_price FROM trades t LEFT JOIN plans p ON p.plan_id = t.plan_id "
        "WHERE t.account_id = ? AND substr(t.opened_at, 1, 10) = ?", (account_id, day)).fetchall()
    closed = conn.execute(
        "SELECT realized_pnl FROM trades WHERE account_id = ? AND status = 'closed' AND substr(closed_at, 1, 10) = ?",
        (account_id, day)).fetchall()
    stop_sold = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE account_id = ? AND status = 'closed' AND stop_reached_at IS NOT NULL "
        "AND substr(stop_reached_at, 1, 10) = ? AND substr(closed_at, 1, 10) = ?", (account_id, day, day)
    ).fetchone()[0]
    return {
        "buy_orders_filled": buys,
        "sell_orders_filled": sells,
        "new_trades": len(new),
        "new_trades_with_full_plan": sum(1 for r in new if r[0] and r[1] and r[2]),
        "trades_closed": len(closed),
        "realized_pnl_sum": sum(r[0] for r in closed),
        "stop_reached_and_sold_same_day": stop_sold,
    }


def _yesterday_check(conn, project_id: int, day_number: int, today_rows, ts: dict) -> Optional[dict]:
    """전날 제안한 행동을 오늘 지켰는지 백엔드가 판단한다. 판단할 수 없으면 None."""
    prev = conn.execute(
        "SELECT output_text FROM ai_feedbacks WHERE project_id = ? AND kind = 'daily' AND trading_day = ?",
        (project_id, day_number - 1)).fetchone()
    if not prev or not prev[0]:
        return None
    out = json.loads(prev[0])
    rule = out.get("action_rule")
    if not rule:
        return None
    violated = any(r["rule_id"] == rule and r["result"] == "violation" for r in today_rows)
    active = ts["buy_orders_filled"] + ts["sell_orders_filled"] > 0
    if violated:
        result = "실행하지 않음"
    elif active:
        result = "실행함"
    else:
        return None                     # 거래가 없어서 지켰는지 알 수 없음
    return {"action": out.get("tomorrow_action"), "rule_id": rule, "result": result}


# ------------------------------------------------------------------ 2. Claude 호출
def call_claude(system: str, user: str, api_key: str, model: str = settings.AI_MODEL) -> str:
    body = json.dumps({
        "model": model, "max_tokens": settings.AI_MAX_TOKENS, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=settings.AI_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON을 찾지 못함")
    return json.loads(m.group(0))


# ------------------------------------------------------------------ 3. 검증
def validate(out: dict, inp: dict) -> list[str]:
    errors = []
    today_ids = {t["eval_id"] for t in inp["today"]}
    violation_ids = {t["eval_id"] for t in inp["today"] if t["result"] == "violation"}
    all_ids = today_ids | {i for h in inp["history"] for i in h["eval_ids"]}

    def text_ok(v, field, required=True):
        if v is None and not required:
            return
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{field}: 비어 있음")
        elif len(v) > 150:
            errors.append(f"{field}: 너무 김")

    text_ok(out.get("summary"), "summary")
    text_ok(out.get("tomorrow_action"), "tomorrow_action")
    causes = out.get("causes")
    if not isinstance(causes, list) or len(causes) > 2:
        errors.append("causes: 목록이 아니거나 3개 이상")
        causes = []
    if violation_ids and not causes:
        errors.append("causes: 감점이 있는데 원인이 없음")
    for i, c in enumerate(causes):
        text_ok(c.get("text") if isinstance(c, dict) else None, f"causes[{i}]")
        ids = c.get("eval_ids") if isinstance(c, dict) else None
        if not ids or not isinstance(ids, list) or not set(ids) <= today_ids:
            errors.append(f"causes[{i}]: 판정 번호가 없거나 오늘 판정이 아님 {ids}")
    p = out.get("pattern")
    if p is not None:
        if not isinstance(p, dict):
            errors.append("pattern: 형식 오류")
        else:
            text_ok(p.get("text"), "pattern")
            ids = p.get("eval_ids")
            if not ids or not isinstance(ids, list) or not set(ids) <= all_ids:
                errors.append(f"pattern: 판정 번호가 없거나 없는 번호 {ids}")
    if inp["yesterday"] is None and out.get("yesterday_check"):
        errors.append("yesterday_check: 판단할 수 없는 날인데 작성함")
    text_ok(out.get("yesterday_check"), "yesterday_check", required=False)
    action = out.get("tomorrow_action") or ""
    for name in inp["stock_names"]:
        if name and name in action:
            errors.append(f"tomorrow_action: 종목 이름({name})이 들어 있음")
    rule = out.get("action_rule")
    if rule is not None and rule not in RULES:
        errors.append(f"action_rule: 없는 규칙 {rule}")
    return errors


# ------------------------------------------------------------------ 4. 규칙 기반 기본 피드백
def fallback(inp: dict) -> dict:
    v = [t for t in inp["today"] if t["result"] == "violation"]
    ts = inp["trade_summary"]
    if v:
        summary = f"오늘 감점 {len(v)}건, 가장 큰 원인은 '{v[0]['rule_name']}'이에요"
    elif ts["new_trades"] and ts["new_trades"] == ts["new_trades_with_full_plan"]:
        summary = f"오늘 새로 시작한 거래 {ts['new_trades']}건 모두 계획을 세우고 샀어요"
    elif ts["stop_reached_and_sold_same_day"]:
        summary = f"손절가에 닿은 거래 {ts['stop_reached_and_sold_same_day']}건을 그날 정리했어요"
    elif ts["buy_orders_filled"] + ts["sell_orders_filled"] == 0:
        summary = "오늘은 거래 없이 시장을 지켜봤어요"
    else:
        summary = "오늘은 감점 없이 하루를 마쳤어요"
    causes = [{"text": t["detail"], "eval_ids": [t["eval_id"]]} for t in v[:2]]
    pattern = None
    today_rules = {t["rule_id"] for t in v}
    for h in sorted(inp["history"], key=lambda h: -h["consecutive_days"]):
        if h["rule_id"] in today_rules and h["consecutive_days"] >= 2:
            pattern = {"text": f"'{h['rule_name']}'이(가) {h['consecutive_days']}일째 이어지고 있어요",
                       "eval_ids": h["eval_ids"][-h["consecutive_days"]:]}
            break
    y = inp["yesterday"]
    ycheck = None if y is None else (
        "어제 제안한 행동을 오늘 지켰어요" if y["result"] == "실행함" else "어제 제안한 행동은 오늘 지켜지지 않았어요")
    rule = v[0]["rule_id"] if v else None
    return {"summary": summary, "causes": causes, "pattern": pattern, "yesterday_check": ycheck,
            "tomorrow_action": ACTION_FOR_RULE.get(rule, DEFAULT_ACTION), "action_rule": rule}


# ------------------------------------------------------------------ 5. 생성과 저장
Caller = Callable[[str, str], str]


def generate_daily(conn, project_id: int, day_number: int, day: str,
                   caller: Optional[Caller] = None, api_key: Optional[str] = None) -> dict:
    """그날 피드백을 돌려준다. 이미 만들었으면 저장된 것을 그대로 돌려준다 (다시 열어도 같은 내용)."""
    row = conn.execute(
        "SELECT * FROM ai_feedbacks WHERE project_id = ? AND kind = 'daily' AND trading_day = ?",
        (project_id, day_number)).fetchone()
    if row and row["status"] in ("ok", "failed"):
        return _view(row)

    inp = build_input(conn, project_id, day_number, day)
    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    user = "data = " + json.dumps(inp, ensure_ascii=False)
    out, model, status, attempts, errors = None, "rule-based", "ok", 0, []

    if key or caller:
        call = caller or (lambda s, u: call_claude(s, u, key))
        for attempts in range(1, settings.AI_MAX_ATTEMPTS + 1):
            try:
                cand = parse_json(call(SYSTEM_PROMPT, user))
                errs = validate(cand, inp)
            except Exception as e:                      # 네트워크 오류, 형식 오류 등
                errs = [f"{type(e).__name__}: {e}"[:200]]
                cand = None
            if not errs:
                out, model = cand, settings.AI_MODEL
                break
            errors.append({"attempt": attempts, "errors": errs})
        if out is None:
            status = "failed"                           # AI 실패 → 기본 피드백으로 대신 (화면에 표시)
    if out is None:
        out = fallback(inp)

    stored_input = {"input": inp, "validation_errors": errors}
    # 같은 날 피드백은 하나만 (화면에서 두 번 요청해도 중복 저장되지 않게)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_day ON ai_feedbacks(project_id, kind, trading_day)")
    conn.execute(
        "INSERT OR IGNORE INTO ai_feedbacks (project_id, kind, trading_day, input_data, output_text, model, status, attempts) "
        "VALUES (?, 'daily', ?, ?, ?, ?, ?, ?)",
        (project_id, day_number, json.dumps(stored_input, ensure_ascii=False),
         json.dumps(out, ensure_ascii=False), model, status, attempts),
    )
    row = conn.execute(
        "SELECT * FROM ai_feedbacks WHERE project_id = ? AND kind = 'daily' AND trading_day = ?",
        (project_id, day_number)).fetchone()
    return _view(row)


def _view(row) -> dict:
    out = json.loads(row["output_text"])
    return {**out, "model": row["model"], "status": row["status"], "trading_day": row["trading_day"],
            "is_ai": row["model"] != "rule-based"}
