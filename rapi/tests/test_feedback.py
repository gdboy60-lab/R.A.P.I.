"""AI 일일 피드백 (명세서 11장). Claude 호출은 가짜 함수로 대신한다 (네트워크·비용 없음)."""
import json
from datetime import datetime, timedelta

import pytest

from app.ai import feedback as fb
from app.engine import indicators
from app.engine.trading import place_order
from app.projects import create_guest, create_project
from app.rules import engine as rules
from tests.test_rules import FakeFeed

D1 = datetime(2024, 3, 4, 10, 0)
D2 = datetime(2024, 3, 5, 10, 0)


@pytest.fixture
def P(conn, period_id, monkeypatch):
    monkeypatch.setattr(indicators, "rsi_with_price", lambda *a: 50.0)
    monkeypatch.setattr(indicators, "ma_and_avg_volume", lambda *a: (1e12, 1.0))
    pid = create_project(conn, create_guest(conn), period_id)
    feed = FakeFeed()

    class E:
        pass
    e = E()
    e.conn, e.pid, e.feed = conn, pid, feed

    def buy(code, t, plan):
        feed.price[code] = 10_000
        place_order(conn, feed, pid, t, "running", code, "buy", "market", 10, plan=plan)

    def close(n, t):
        rules.on_market_close(conn, pid, n, t.replace(hour=15, minute=30))
        rules.publish(conn, pid)

    e.buy, e.close = buy, close
    return e


FULL = {"reason": "이유", "target_price": 20_000, "stop_price": 1, "target_qty": 99}


def day1_without_plan(P):
    P.buy("DEV001", D1, {})                     # 계획 누락 → PA-01
    P.close(1, D1)
    return [r["eval_id"] for r in P.conn.execute(
        "SELECT eval_id FROM rule_evaluations WHERE result='violation'")]


def good_json(ids, action_rule="PA-01", **kw):
    out = {"summary": "계획 없이 산 거래가 있었어요", "causes": [{"text": "매수 사유가 비어 있었어요", "eval_ids": ids[:1]}],
           "pattern": None, "yesterday_check": None, "tomorrow_action": "매수 전에 계획 세 칸을 먼저 채워 보세요",
           "action_rule": action_rule}
    out.update(kw)
    return "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"


def test_input_has_today_history_scores_and_names(P):
    ids = day1_without_plan(P)
    inp = fb.build_input(P.conn, P.pid, 1, "2024-03-04")
    assert [t["eval_id"] for t in inp["today"] if t["result"] == "violation"] == ids
    assert inp["scores"]["before"]["PA"] == 100 and inp["scores"]["after"]["PA"] == 70
    assert inp["history"][0]["rule_id"] == "PA-01" and inp["history"][0]["consecutive_days"] == 1
    assert inp["yesterday"] is None and "한빛전자" in inp["stock_names"]


def test_no_key_uses_rule_based_and_it_is_valid(P):
    day1_without_plan(P)
    out = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", api_key="")
    assert out["model"] == "rule-based" and out["status"] == "ok" and not out["is_ai"]
    assert fb.validate({k: out[k] for k in ("summary", "causes", "pattern", "yesterday_check",
                                            "tomorrow_action", "action_rule")},
                       fb.build_input(P.conn, P.pid, 1, "2024-03-04")) == []
    assert out["action_rule"] == "PA-01"


def test_ai_success_is_stored_and_reused(P):
    ids = day1_without_plan(P)
    calls = []
    def caller(system, user):
        calls.append(user)
        return good_json(ids)
    out = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", caller=caller)
    assert out["is_ai"] and out["status"] == "ok" and len(calls) == 1
    again = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", caller=caller)
    assert again["summary"] == out["summary"] and len(calls) == 1          # 다시 열어도 같은 내용, 재호출 없음
    stored = json.loads(P.conn.execute("SELECT input_data FROM ai_feedbacks").fetchone()[0])
    assert stored["input"]["today"]                                        # 입력도 함께 저장


def test_retries_when_eval_id_is_made_up(P):
    ids = day1_without_plan(P)
    answers = iter([good_json([9999]), good_json(ids)])                     # 처음엔 없는 번호
    out = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", caller=lambda s, u: next(answers))
    row = P.conn.execute("SELECT attempts, status FROM ai_feedbacks").fetchone()
    assert out["is_ai"] and tuple(row) == (2, "ok")


def test_fails_over_to_rule_based_after_three_bad_answers(P):
    day1_without_plan(P)
    out = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", caller=lambda s, u: "죄송해요")
    row = P.conn.execute("SELECT attempts, status, model FROM ai_feedbacks").fetchone()
    assert tuple(row) == (3, "failed", "rule-based") and out["summary"]


def test_network_error_also_falls_back(P):
    day1_without_plan(P)
    def boom(s, u):
        raise TimeoutError("timeout")
    out = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", caller=boom)
    assert out["status"] == "failed" and out["model"] == "rule-based"


@pytest.mark.parametrize("change, msg", [
    ({"tomorrow_action": "한빛전자를 내일 사 보세요"}, "종목 이름"),
    ({"causes": []}, "원인이 없음"),
    ({"yesterday_check": "어제 잘했어요"}, "판단할 수 없는 날"),
    ({"action_rule": "XX-99"}, "없는 규칙"),
])
def test_validate_rejects(P, change, msg):
    ids = day1_without_plan(P)
    inp = fb.build_input(P.conn, P.pid, 1, "2024-03-04")
    out = json.loads(good_json(ids).strip("`json\n"))
    out.update(change)
    assert any(msg in e for e in fb.validate(out, inp))


@pytest.mark.parametrize("day2, expected", [("full_plan", "실행함"), ("no_plan", "실행하지 않음"), ("no_trade", None)])
def test_yesterday_check_by_backend(P, day2, expected):
    day1_without_plan(P)
    fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", api_key="")          # 1일차 제안: PA-01 관련 행동
    if day2 == "full_plan":
        P.buy("DEV003", D2, FULL)
    elif day2 == "no_plan":
        P.buy("DEV003", D2, {})
    P.close(2, D2)
    y = fb.build_input(P.conn, P.pid, 2, "2024-03-05")["yesterday"]
    assert (y["result"] if y else None) == expected


def test_no_penalty_day_praises_from_trade_summary(P):
    P.buy("DEV001", D1, FULL)
    P.close(1, D1)
    out = fb.generate_daily(P.conn, P.pid, 1, "2024-03-04", api_key="")
    assert out["causes"] == [] and "계획" in out["summary"]
