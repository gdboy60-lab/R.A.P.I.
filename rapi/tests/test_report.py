"""최종 리포트 (명세서 11-7). Claude 호출은 가짜 함수로 대신한다."""
import json
from datetime import datetime

import pytest

from app.ai import feedback as fb
from app.ai import report as rp
from app.engine import indicators
from app.engine.trading import place_order
from app.projects import create_guest, create_project
from app.rules import engine as rules
from tests.test_rules import FakeFeed


@pytest.fixture
def run(conn, period_id, monkeypatch):
    """n일을 진행한다. 처음 5일은 매일 계획 없이 다른 종목을 1번씩 산다 (PA-01 5건)."""
    monkeypatch.setattr(indicators, "rsi_with_price", lambda *a: 50.0)
    monkeypatch.setattr(indicators, "ma_and_avg_volume", lambda *a: (1e12, 1.0))
    pid = create_project(conn, create_guest(conn), period_id)
    feed = FakeFeed()
    days = [r[0] for r in conn.execute(
        "SELECT c.date FROM market_calendar c JOIN projects p ON c.date BETWEEN p.start_date AND p.end_date "
        "WHERE p.project_id = ? AND c.is_open = 1 ORDER BY c.date", (pid,))]

    def go(n, daily_feedback=True):
        for i in range(n):
            t = datetime.fromisoformat(days[i] + "T10:00:00")
            if i < 5:
                code = f"DEV00{i + 1}"
                feed.price[code] = 10_000
                place_order(conn, feed, pid, t, "running", code, "buy", "market", 10, plan={})
            conn.execute("UPDATE projects SET current_day = ? WHERE project_id = ?", (i + 1, pid))
            res = rules.on_market_close(conn, pid, i + 1, t.replace(hour=15, minute=30))
            rules.publish(conn, pid)
            if daily_feedback:
                fb.generate_daily(conn, pid, i + 1, days[i], api_key="")
            if res.get("terminated"):
                break
        rules.finalize(conn, pid, datetime.fromisoformat(days[n - 1] + "T15:30:00"))
        return pid
    return conn, go, days


def ai_json(data, **kw):
    out = {"repeated": [{"rule_id": t["rule_id"], "text": f"{t['rule_name']}이 반복됐어요", "eval_ids": t["eval_ids"][:1]}
                        for t in data["top_rules"]],
           "change": "하루 평균 위반이 1.0건에서 0.0건으로 줄었어요" if data["change"] else None,
           "focus": {"text": "매수 전에 계획 세 칸을 먼저 채워 보세요", "rule_id": "PA-01"}}
    out.update(kw)
    return json.dumps(out, ensure_ascii=False)


def test_backend_numbers(run):
    conn, go, days = run
    pid = go(10)
    d = rp.build_report_data(conn, pid)
    assert d["status"] == "finished" and d["total_days"] == 10 and d["scores"]["overall"] < 100
    assert [x["penalty"] > 0 for x in d["daily"]][:5] == [True] * 5
    total = conn.execute("SELECT SUM(penalty) FROM rule_evaluations WHERE result = 'violation'").fetchone()[0]
    assert sum(x["penalty"] for x in d["daily"]) == total          # 감점이 하루도 빠지지 않고 들어감
    top = d["top_rules"][0]
    assert top["rule_id"] == "PA-01" and top["count"] == 5 and len(d["top_rules"]) <= 3
    assert d["change"]["first_days"] == 5 and d["change"]["rest_days"] == 5
    assert d["change"]["first_avg"] > d["change"]["rest_avg"]
    ex = d["execution"]                                     # 2~5일차: 전날 제안(계획 채우기) 안 지킴
    assert ex["judged"] >= 4 and ex["done"] == 0 and ex["rate"] == 0


def test_change_is_null_when_only_five_days(run):
    conn, go, days = run
    pid = go(5)
    d = rp.build_report_data(conn, pid)
    assert d["change"] is None
    out = rp.generate_final(conn, pid, api_key="")
    assert out["ai"]["change"] is None and rp.validate(out["ai"], d) == []


def test_fallback_is_valid(run):
    conn, go, days = run
    pid = go(10)
    out = rp.generate_final(conn, pid, api_key="")
    ai = {k: out["ai"][k] for k in ("repeated", "change", "focus")}
    assert out["ai"]["model"] == "rule-based" and rp.validate(ai, out) == []
    assert ai["focus"]["rule_id"] == "PA-01" and "1.0건" in ai["change"]


def test_ai_stored_and_reused(run):
    conn, go, days = run
    pid = go(10)
    data = rp.build_report_data(conn, pid)
    calls = []
    def caller(s, u):
        calls.append(u)
        return ai_json(data)
    out = rp.generate_final(conn, pid, caller=caller)
    assert out["ai"]["model"] != "rule-based" and len(calls) == 1
    again = rp.generate_final(conn, pid, caller=caller)
    assert again["ai"]["focus"] == out["ai"]["focus"] and len(calls) == 1


def test_made_up_eval_id_is_retried(run):
    conn, go, days = run
    pid = go(10)
    data = rp.build_report_data(conn, pid)
    bad = ai_json(data, repeated=[{"rule_id": "PA-01", "text": "반복", "eval_ids": [9999]}])
    answers = iter([bad, ai_json(data)])
    out = rp.generate_final(conn, pid, caller=lambda s, u: next(answers))
    row = conn.execute("SELECT attempts, status FROM ai_feedbacks WHERE kind = 'final'").fetchone()
    assert tuple(row) == (2, "ok") and out["ai"]["model"] != "rule-based"


@pytest.mark.parametrize("change, msg", [
    ({"focus": {"text": "한빛전자를 다시 사 보세요", "rule_id": None}}, "종목 이름"),
    ({"change": ""}, "change: 비어 있음"),
    ({"repeated": []}, "repeated"),
    ({"focus": {"text": "좋아요", "rule_id": "XX-99"}}, "없는 규칙"),
])
def test_validate_rejects(run, change, msg):
    conn, go, days = run
    pid = go(10)
    data = rp.build_report_data(conn, pid)
    assert any(msg in e for e in rp.validate(json.loads(ai_json(data, **change)), data))


def test_bad_answers_fall_back(run):
    conn, go, days = run
    pid = go(10)
    out = rp.generate_final(conn, pid, caller=lambda s, u: "죄송해요")
    assert out["ai"]["status"] == "failed" and out["ai"]["model"] == "rule-based" and out["ai"]["focus"]["text"]
