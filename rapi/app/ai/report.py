"""최종 리포트 (명세서 11-7).

| 순서 | 항목                          | 작성        |
| 1    | 최종 점수                      | 백엔드      |
| 2    | 일차별 감점 (막대)             | 백엔드      |
| 3    | 반복된 위반 상위 3개와 대표 사례 | 백엔드 + AI 설명 (근거 번호 필수) |
| 4    | 처음 5일과 이후 비교            | 백엔드 + AI 설명 (숫자 그대로) |
| 5    | 제안 행동 실행률               | 백엔드      |
| 6    | 다음에 집중할 습관 하나         | AI          |
| 7    | 해결되지 않은 보류             | 백엔드      |

판정이 '일어난 날'은 하루 단위 규칙이면 대상 날짜, 거래 단위 규칙이면 처음 판정한 날로 본다.
(BC-01처럼 5일차에 다시 판정된 기록도 원래 날짜에 들어간다.)
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Optional

from .. import settings
from ..rules.catalog import RULES
from ..rules.describe import describe
from .feedback import ACTION_FOR_RULE, DEFAULT_ACTION, Caller, _names, call_claude, parse_json

BASELINE_DAYS = 5
FINAL_DAY = 0          # ai_feedbacks.trading_day 값: 최종 리포트

SYSTEM_PROMPT = """너는 초보 투자자의 가상 매매 습관 리포트를 마무리하는 코치야.
백엔드가 계산한 JSON 데이터만 보고, 정해진 JSON 형식으로 써.

반드시 지킬 것
- 데이터에 없는 사실, 숫자, 점수를 만들지 마. 숫자는 데이터에 있는 값을 그대로 써.
- repeated 는 data.top_rules 의 규칙마다 하나씩, 그 규칙의 eval_ids 중에서만 번호를 골라 적어.
- change 는 data.change 의 숫자를 그대로 써서 설명해. "개선됐다", "나빠졌다"처럼 단정하지 말고
  "하루 평균 위반이 0.8건에서 0.2건으로 줄었어요"처럼 숫자로 말해. data.change 가 null 이면 change 도 null.
- focus 는 다음 프로젝트에서 집중할 습관 하나. 특정 종목을 사거나 팔라고 권하지 말고, 종목 이름을 쓰지 마.
- 사용자의 감정이나 성격을 추측하지 마. 말투는 친근한 존댓말(~해요). 각 문장은 100자 이내.

출력은 아래 JSON 하나만.
{"repeated": [{"rule_id": "RM-03", "text": "설명", "eval_ids": [번호]}],
 "change": "처음 5일과 이후 비교 설명" 또는 null,
 "focus": {"text": "집중할 습관 하나", "rule_id": "규칙 ID" 또는 null}}"""


# ------------------------------------------------------------------ 백엔드 계산
def _days(conn, project_id: int) -> list[str]:
    """프로젝트에서 실제로 지나간 거래일 (조기 종료면 거기까지)."""
    p = conn.execute("SELECT start_date, end_date, current_day FROM projects WHERE project_id = ?",
                     (project_id,)).fetchone()
    days = [r[0] for r in conn.execute(
        "SELECT date FROM market_calendar WHERE is_open = 1 AND date BETWEEN ? AND ? ORDER BY date",
        (p["start_date"], p["end_date"]))]
    return days[:p["current_day"]]


def _occurred(row) -> str:
    return row["target_date"] or row["evaluated_at"][:10]


def build_report_data(conn, project_id: int) -> dict:
    names = _names(conn, project_id)
    days = _days(conn, project_id)
    rows = conn.execute("SELECT * FROM rule_evaluations WHERE project_id = ? ORDER BY eval_id",
                        (project_id,)).fetchall()
    v = [r for r in rows if r["result"] == "violation"]

    # 1. 최종 점수 (기간 종료 때 확정한 값)
    fr = conn.execute("SELECT * FROM final_results WHERE project_id = ?", (project_id,)).fetchone()
    scores = None if fr is None else {"RM": fr["rm_score"], "PA": fr["pa_score"], "BC": fr["bc_score"],
                                      "MC": fr["mc_score"], "overall": fr["overall_score"]}

    # 2. 일차별 감점
    per_day = defaultdict(int)
    for r in v:
        per_day[_occurred(r)] += r["penalty"]
    daily = [{"day": i + 1, "date": d, "penalty": per_day.get(d, 0)} for i, d in enumerate(days)]

    # 3. 반복된 위반 상위 3개
    by_rule = defaultdict(list)
    for r in v:
        by_rule[r["rule_id"]].append(r)
    top = sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), -sum(x["penalty"] for x in kv[1]), kv[0]))[:3]
    top_rules = [{"rule_id": rid, "rule_name": RULES[rid].name, "count": len(rs),
                  "penalty": sum(x["penalty"] for x in rs), "eval_ids": [x["eval_id"] for x in rs],
                  "example": describe(rs[-1], names), "example_eval_id": rs[-1]["eval_id"]} for rid, rs in top]

    # 4. 처음 5일과 이후 비교 (하루 평균 위반 건수)
    first, rest = days[:BASELINE_DAYS], days[BASELINE_DAYS:]
    change = None
    if rest:
        def avg(rs, ds):
            return round(sum(1 for r in rs if _occurred(r) in ds) / len(ds), 2)
        fs, rs_ = set(first), set(rest)
        per_rule = [{"rule_id": rid, "rule_name": RULES[rid].name,
                     "first_avg": avg(rs, fs), "rest_avg": avg(rs, rs_)} for rid, rs in sorted(by_rule.items())]
        change = {"first_days": len(first), "rest_days": len(rest),
                  "first_avg": avg(v, fs), "rest_avg": avg(v, rs_), "per_rule": per_rule}

    # 5. 제안 행동 실행률 (일일 피드백의 '어제 제안 점검' 중 판단할 수 있었던 것)
    judged = done = 0
    for r in conn.execute("SELECT input_data FROM ai_feedbacks WHERE project_id = ? AND kind = 'daily'",
                          (project_id,)):
        y = json.loads(r[0]).get("input", {}).get("yesterday")
        if y:
            judged += 1
            done += y["result"] == "실행함"
    execution = {"judged": judged, "done": done, "rate": round(done / judged, 2) if judged else None}

    # 7. 해결되지 않은 보류
    holds = [{"rule_id": r["rule_id"], "rule_name": RULES[r["rule_id"]].name, "reason": r["hold_reason"],
              "eval_id": r["eval_id"]} for r in rows if r["result"] == "hold"]

    p = conn.execute("SELECT status, end_reason FROM projects WHERE project_id = ?", (project_id,)).fetchone()
    return {"status": p["status"], "end_reason": p["end_reason"], "total_days": len(days),
            "scores": scores, "daily": daily, "top_rules": top_rules, "change": change,
            "execution": execution, "holds": holds, "stock_names": sorted(set(names.values()))}


# ------------------------------------------------------------------ 검증
def validate(out: dict, data: dict) -> list[str]:
    errors = []
    tops = {t["rule_id"]: set(t["eval_ids"]) for t in data["top_rules"]}
    rep = out.get("repeated")
    if not isinstance(rep, list) or len(rep) != len(tops):
        errors.append(f"repeated: 상위 규칙 {len(tops)}개마다 하나씩 필요")
        rep = []
    for i, r in enumerate(rep):
        if not isinstance(r, dict) or r.get("rule_id") not in tops:
            errors.append(f"repeated[{i}]: 상위 규칙이 아님")
            continue
        ids = r.get("eval_ids")
        if not ids or not isinstance(ids, list) or not set(ids) <= tops[r["rule_id"]]:
            errors.append(f"repeated[{i}]: 그 규칙의 판정 번호가 아님 {ids}")
        if not isinstance(r.get("text"), str) or not r["text"].strip() or len(r["text"]) > 160:
            errors.append(f"repeated[{i}]: 설명 길이")
    ch = out.get("change")
    if data["change"] is None and ch:
        errors.append("change: 비교할 기간이 없는데 작성함")
    if data["change"] is not None and (not isinstance(ch, str) or not ch.strip()):
        errors.append("change: 비어 있음")
    focus = out.get("focus")
    if not isinstance(focus, dict) or not isinstance(focus.get("text"), str) or not focus["text"].strip():
        errors.append("focus: 비어 있음")
    else:
        if focus.get("rule_id") is not None and focus["rule_id"] not in RULES:
            errors.append("focus: 없는 규칙")
        for name in data["stock_names"]:
            if name and name in focus["text"]:
                errors.append(f"focus: 종목 이름({name})이 들어 있음")
    return errors


# ------------------------------------------------------------------ 규칙 기반 기본 문장
def fallback(data: dict) -> dict:
    repeated = [{"rule_id": t["rule_id"], "eval_ids": [t["example_eval_id"]],
                 "text": f"'{t['rule_name']}' 위반이 {t['count']}번, 감점 {t['penalty']}점이에요. 같은 상황이 오면 이 규칙부터 떠올려 보세요"}
                for t in data["top_rules"]]
    change = None
    c = data["change"]
    if c:
        change = (f"처음 {c['first_days']}일은 하루 평균 위반이 {c['first_avg']:.1f}건, "
                  f"이후 {c['rest_days']}일은 {c['rest_avg']:.1f}건이었어요")
    rule = data["top_rules"][0]["rule_id"] if data["top_rules"] else None
    return {"repeated": repeated, "change": change,
            "focus": {"text": ACTION_FOR_RULE.get(rule, DEFAULT_ACTION), "rule_id": rule}}


# ------------------------------------------------------------------ 생성과 저장
def generate_final(conn, project_id: int, caller: Optional[Caller] = None, api_key: Optional[str] = None) -> dict:
    """최종 리포트 문장. 한 번 만들면 저장된 것을 그대로 돌려준다 (기간 종료 후 바뀌지 않음)."""
    data = build_report_data(conn, project_id)
    row = conn.execute("SELECT * FROM ai_feedbacks WHERE project_id = ? AND kind = 'final'",
                       (project_id,)).fetchone()
    if row and row["status"] in ("ok", "failed"):
        return {**data, "ai": {**json.loads(row["output_text"]), "model": row["model"], "status": row["status"]}}

    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    user = "data = " + json.dumps({k: v for k, v in data.items() if k != "holds"}, ensure_ascii=False)
    out, model, status, attempts, errors = None, "rule-based", "ok", 0, []
    if key or caller:
        call = caller or (lambda s, u: call_claude(s, u, key))
        for attempts in range(1, settings.AI_MAX_ATTEMPTS + 1):
            try:
                cand = parse_json(call(SYSTEM_PROMPT, user))
                errs = validate(cand, data)
            except Exception as e:
                cand, errs = None, [f"{type(e).__name__}: {e}"[:200]]
            if not errs:
                out, model = cand, settings.AI_MODEL
                break
            errors.append({"attempt": attempts, "errors": errs})
        if out is None:
            status = "failed"
    if out is None:
        out = fallback(data)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_day ON ai_feedbacks(project_id, kind, trading_day)")
    conn.execute(
        "INSERT OR IGNORE INTO ai_feedbacks (project_id, kind, trading_day, input_data, output_text, model, status, "
        "attempts) VALUES (?, 'final', ?, ?, ?, ?, ?, ?)",
        (project_id, FINAL_DAY, json.dumps({"input": data, "validation_errors": errors}, ensure_ascii=False),
         json.dumps(out, ensure_ascii=False), model, status, attempts))
    return {**data, "ai": {**out, "model": model, "status": status}}
