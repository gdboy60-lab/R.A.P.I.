"""14개 규칙 판정 (명세서 3~6장, 10-2 판정 시점).

판정 시점
- 매수 체결 때  on_buy_fill   : RM-01, RM-02, RM-04, PA-01, PA-03, PA-04, BC-02, MC-01, MC-02, MC-03
- 거래 종료 때  on_trade_closed: PA-02, BC-03
- 장 마감 때    on_market_close: RM-03, BC-01 (5일차에 기준 기간 재평가, 일평균 0회면 조기 종료)

공통 원칙
- 판정 기록(rule_evaluations)이 점수의 유일한 원본이다.
- 같은 규칙 + 같은 대상(거래 또는 날짜)은 한 줄만. 보류였다가 위반·무효로 바뀌면 그 줄을 고친다.
- 데이터가 모자라면 감점 대신 보류(hold)하고 사유를 남긴다.
- 모든 시각은 시뮬레이션 시각이다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from .catalog import RULES

# 명세서의 기준값
RM01_RATIO, RM02_RATIO = 0.50, 0.80          # 초과
PA02_MAX_RETURN = 0.02                       # 0% 초과 ~ 2% 이하
PA04_RISE = 0.05                             # 이상
BC01_MULTIPLE = 3                            # 이상
BC02_STREAK, BC02_WINDOW = 3, timedelta(minutes=10)   # 3회 이상, 10분 0초 포함
BC03_SECONDS, BC03_COUNT = timedelta(minutes=3), 2    # 3분 0초 포함, 2회 이상
MC01_RSI, MC02_SHARE, MC03_RATIO = 75, 0.20, 1.20     # 이상, 미만, 이상
BASELINE_DAYS = 5


# ------------------------------------------------------------------ 기록
def record(conn, project_id: int, rule_id: str, t: datetime, result: str, *,
           trade_id: Optional[int] = None, date: Optional[str] = None,
           evidence: Optional[dict] = None, hold_reason: Optional[str] = None) -> str:
    """판정 한 건을 남긴다. 이미 있으면 위반이 아닌 것(보류·무효)만 새 결과로 바꾼다.

    돌려주는 값: 'new' | 'updated' | 'kept'
    """
    rule = RULES[rule_id]
    penalty = rule.penalty if result == "violation" else 0
    ev = json.dumps(evidence or {}, ensure_ascii=False, default=str)
    row = conn.execute(
        "SELECT eval_id, result FROM rule_evaluations WHERE project_id = ? AND rule_id = ? "
        "AND COALESCE(target_trade_id, -1) = COALESCE(?, -1) AND COALESCE(target_date, '') = COALESCE(?, '')",
        (project_id, rule_id, trade_id, date),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO rule_evaluations (project_id, rule_id, area, target_trade_id, target_date, result, "
            "penalty, evidence, hold_reason, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, rule_id, rule.area, trade_id, date, result, penalty, ev, hold_reason, t.isoformat()),
        )
        return "new"
    if row["result"] != "violation" and row["result"] != result:
        conn.execute(
            "UPDATE rule_evaluations SET result = ?, penalty = ?, evidence = ?, hold_reason = ?, updated_at = ?, "
            "published = 0 WHERE eval_id = ?",
            (result, penalty, ev, hold_reason, t.isoformat(), row["eval_id"]),
        )
        return "updated"
    return "kept"


def _project_of_account(conn, account_id: int) -> int:
    return conn.execute("SELECT project_id FROM accounts WHERE account_id = ?", (account_id,)).fetchone()[0]


def _session_open(conn, d: str) -> Optional[datetime]:
    r = conn.execute("SELECT open_time FROM market_calendar WHERE date = ?", (d,)).fetchone()
    return datetime.fromisoformat(f"{d}T{r[0]}") if r else None


# ------------------------------------------------------------------ 매수 체결 때
def on_buy_fill(conn, *, account_id: int, order_id: int, exec_id: int, trade_id: int,
                price: int, qty: int, pre_total_assets: int, t: datetime) -> None:
    pid = _project_of_account(conn, account_id)
    amount = price * qty
    plan = conn.execute(
        "SELECT p.* FROM plans p JOIN trades t ON t.plan_id = p.plan_id WHERE t.trade_id = ?", (trade_id,)
    ).fetchone()
    trade = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
    snap = conn.execute("SELECT * FROM execution_snapshots WHERE execution_id = ?", (exec_id,)).fetchone()
    base = {"order_id": order_id, "execution_id": exec_id, "stock_code": trade["stock_code"],
            "price": price, "qty": qty, "executed_at": t}

    # RM-01 · RM-02: 매수 금액 ÷ 매수 직전 총자산 (현금 + 보유 주식 평가액)
    ratio = amount / pre_total_assets if pre_total_assets else 0
    ev = {**base, "amount": amount, "total_assets": pre_total_assets, "ratio": round(ratio, 4)}
    if ratio > RM01_RATIO:
        record(conn, pid, "RM-01", t, "violation", trade_id=trade_id, evidence=ev)
    if ratio > RM02_RATIO:
        record(conn, pid, "RM-02", t, "violation", trade_id=trade_id, evidence=ev)

    # RM-04: 하나의 주문으로 목표 수량 100% 체결 (목표 수량이 없으면 판정할 수 없어 기록하지 않음)
    if plan is not None and plan["target_qty"] and qty >= plan["target_qty"]:
        record(conn, pid, "RM-04", t, "violation", trade_id=trade_id,
               evidence={**base, "target_qty": plan["target_qty"]})

    # PA-01: 주문 제출 시점의 매수 사유·목표가·손절가 중 하나라도 비었으면
    if plan is None:
        missing = ["매수 사유", "목표가", "손절가"]
    else:
        missing = [label for k, label in
                   (("reason", "매수 사유"), ("target_price", "목표가"), ("stop_price", "손절가")) if not plan[k]]
    if missing:
        record(conn, pid, "PA-01", t, "violation", trade_id=trade_id, evidence={**base, "missing": missing})

    # PA-03: 손절가에 닿은 뒤 같은 종목을 추가 매수
    if trade["stop_reached_at"] and trade["stop_reached_at"] <= t.isoformat():
        record(conn, pid, "PA-03", t, "violation", trade_id=trade_id,
               evidence={**base, "stop_reached_at": trade["stop_reached_at"],
                         "stop_price": plan["stop_price"] if plan else None})

    # PA-04: 당일 장 시작 전에 관심 종목으로 등록하지 않은 종목을 전일 대비 +5% 이상에서 매수
    prev_close = snap["prev_close"] if snap else None
    if prev_close is None:
        record(conn, pid, "PA-04", t, "hold", trade_id=trade_id, evidence=base, hold_reason="전일 종가 없음")
    else:
        rise = price / prev_close - 1
        day = t.date().isoformat()
        open_at = _session_open(conn, day) or t
        planned = conn.execute(
            "SELECT 1 FROM watchlist WHERE project_id = ? AND stock_code = ? AND added_at < ? "
            "AND (removed_at IS NULL OR removed_at > ?)",
            (pid, trade["stock_code"], open_at.isoformat(), t.isoformat()),
        ).fetchone()
        if rise >= PA04_RISE and not planned:
            record(conn, pid, "PA-04", t, "violation", trade_id=trade_id,
                   evidence={**base, "prev_close": prev_close, "rise": round(rise, 4)})

    # BC-02: 연속 3회 이상 손실로 끝난 거래 직후 10분(포함) 안에 아무 종목이나 매수
    _bc02(conn, pid, account_id, t, base)

    # MC-01 ~ MC-03: 체결 순간 스냅샷으로 판정. 지표가 없으면 보류
    rsi = snap["rsi_14"] if snap else None
    if rsi is None:
        record(conn, pid, "MC-01", t, "hold", trade_id=trade_id, evidence=base, hold_reason="RSI 계산 데이터 부족")
    elif rsi >= MC01_RSI:
        record(conn, pid, "MC-01", t, "violation", trade_id=trade_id, evidence={**base, "rsi_14": round(rsi, 1)})

    if not snap or snap["avg_volume_20d"] is None or snap["cum_volume"] is None or snap["elapsed_ratio"] is None:
        record(conn, pid, "MC-02", t, "hold", trade_id=trade_id, evidence=base, hold_reason="거래량 데이터 부족")
    else:
        expected = snap["avg_volume_20d"] * snap["elapsed_ratio"]
        if snap["cum_volume"] < expected * MC02_SHARE:
            record(conn, pid, "MC-02", t, "violation", trade_id=trade_id,
                   evidence={**base, "cum_volume": snap["cum_volume"], "expected_volume": round(expected),
                             "share": round(snap["cum_volume"] / expected, 4) if expected else None})

    ma = snap["ma_20d"] if snap else None
    if ma is None:
        record(conn, pid, "MC-03", t, "hold", trade_id=trade_id, evidence=base, hold_reason="20일 이동평균 데이터 부족")
    elif price >= ma * MC03_RATIO:
        record(conn, pid, "MC-03", t, "violation", trade_id=trade_id,
               evidence={**base, "ma_20d": round(ma), "ratio": round(price / ma, 4)})


def loss_streaks(conn, account_id: int) -> list[tuple]:
    """끝난 거래를 종료 시각 순으로 보며 연속 손실 횟수를 센다. [(trade_id, closed_at, pnl, 연속 횟수)]"""
    rows = conn.execute(
        "SELECT trade_id, closed_at, realized_pnl FROM trades WHERE account_id = ? AND status = 'closed' "
        "ORDER BY closed_at, trade_id", (account_id,),
    ).fetchall()
    out, streak = [], 0
    for r in rows:
        streak = streak + 1 if r["realized_pnl"] < 0 else 0      # 0원 이상이면 끊김
        out.append((r["trade_id"], r["closed_at"], r["realized_pnl"], streak))
    return out


def _bc02(conn, pid: int, account_id: int, t: datetime, base: dict) -> None:
    streaks = [s for s in loss_streaks(conn, account_id) if s[1] <= t.isoformat()]
    if not streaks:
        return
    trade_id, closed_at, _, streak = streaks[-1]
    if streak >= BC02_STREAK and t - datetime.fromisoformat(closed_at) <= BC02_WINDOW:
        # 대상은 감시를 시작시킨 거래 → 한 감시 구간에서 여러 번 사도 한 번만 감점
        record(conn, pid, "BC-02", t, "violation", trade_id=trade_id,
               evidence={**base, "loss_streak": streak, "watch_started_at": closed_at})


# ------------------------------------------------------------------ 거래 종료 때
def on_trade_closed(conn, trade_id: int, t: datetime) -> None:
    tr = conn.execute(
        "SELECT t.*, p.target_price FROM trades t LEFT JOIN plans p ON p.plan_id = t.plan_id WHERE t.trade_id = ?",
        (trade_id,),
    ).fetchone()
    pid = _project_of_account(conn, tr["account_id"])
    base = {"stock_code": tr["stock_code"], "opened_at": tr["opened_at"], "closed_at": tr["closed_at"],
            "realized_pnl": tr["realized_pnl"], "realized_return": tr["realized_return"]}

    # PA-02: 목표가에 닿기 전에 끝냈고, 비용 차감 후 수익률이 0% 초과 2% 이하
    reached_before_close = tr["target_reached_at"] and tr["target_reached_at"] <= tr["closed_at"]
    r = tr["realized_return"]
    if tr["target_price"] and not reached_before_close and r is not None and 0 < r <= PA02_MAX_RETURN:
        record(conn, pid, "PA-02", t, "violation", trade_id=trade_id,
               evidence={**base, "target_price": tr["target_price"]})

    # BC-03: 같은 거래일에 3분(포함) 이하 거래가 2회 이상 → 그날 한 번 감점
    day = tr["closed_at"][:10]
    short = []
    for x in conn.execute(
            "SELECT trade_id, opened_at, closed_at FROM trades WHERE account_id = ? AND status = 'closed' "
            "AND substr(closed_at, 1, 10) = ?", (tr["account_id"], day)):
        if datetime.fromisoformat(x["closed_at"]) - datetime.fromisoformat(x["opened_at"]) <= BC03_SECONDS:
            short.append(x["trade_id"])
    if len(short) >= BC03_COUNT:
        record(conn, pid, "BC-03", t, "violation", date=day, evidence={"date": day, "short_trades": short})


# ------------------------------------------------------------------ 장 마감 때
def daily_order_count(conn, account_id: int, day: str) -> int:
    """그날 체결된 주문 수 (같은 주문의 부분 체결은 1회)."""
    return conn.execute(
        "SELECT COUNT(DISTINCT e.order_id) FROM executions e JOIN orders o USING (order_id) "
        "WHERE o.account_id = ? AND substr(e.executed_at, 1, 10) = ?", (account_id, day),
    ).fetchone()[0]


def on_market_close(conn, project_id: int, day_number: int, t: datetime) -> dict:
    """장 마감 처리 2~4단계 (명세서 10-2). 돌려주는 값: {'terminated': bool}"""
    account_id = conn.execute("SELECT account_id FROM accounts WHERE project_id = ?", (project_id,)).fetchone()[0]
    day = t.date().isoformat()

    # RM-03: 손절가에 닿은 뒤 장 마감까지 들고 있는 거래
    for tr in conn.execute(
            "SELECT t.trade_id, t.stock_code, t.stop_reached_at, p.stop_price FROM trades t "
            "LEFT JOIN plans p ON p.plan_id = t.plan_id "
            "WHERE t.account_id = ? AND t.status = 'open' AND t.stop_reached_at IS NOT NULL "
            "AND t.stop_reached_at <= ?", (account_id, t.isoformat())).fetchall():
        record(conn, project_id, "RM-03", t, "violation", trade_id=tr["trade_id"],
               evidence={"stock_code": tr["stock_code"], "stop_price": tr["stop_price"],
                         "stop_reached_at": tr["stop_reached_at"], "held_until": t})

    # BC-01: 하루 주문 수 ≥ 기준 기간 일평균 × 3
    count = daily_order_count(conn, account_id, day)
    terminated = False
    if day_number <= BASELINE_DAYS:
        record(conn, project_id, "BC-01", t, "hold", date=day, evidence={"date": day, "orders": count},
               hold_reason="기준 기간(1~5일차)이라 평균을 재는 중")
        if day_number == BASELINE_DAYS:
            terminated = _finish_baseline(conn, project_id, account_id, t)
    else:
        avg = conn.execute("SELECT baseline_daily_avg FROM projects WHERE project_id = ?", (project_id,)).fetchone()[0]
        if avg and count >= avg * BC01_MULTIPLE:
            record(conn, project_id, "BC-01", t, "violation", date=day,
                   evidence={"date": day, "orders": count, "baseline_daily_avg": avg})
    return {"terminated": terminated}


def _finish_baseline(conn, project_id: int, account_id: int, t: datetime) -> bool:
    """5일차 장 마감: 일평균을 계산하고 1~5일차를 다시 판정한다. 0회면 프로젝트를 끝낸다."""
    days = [r[0] for r in conn.execute(
        "SELECT c.date FROM market_calendar c JOIN projects p ON c.date BETWEEN p.start_date AND p.baseline_end_date "
        "WHERE p.project_id = ? AND c.is_open = 1 ORDER BY c.date", (project_id,))]
    counts = {d: daily_order_count(conn, account_id, d) for d in days}
    avg = sum(counts.values()) / BASELINE_DAYS
    conn.execute("UPDATE projects SET baseline_daily_avg = ? WHERE project_id = ?", (avg, project_id))
    if avg == 0:
        conn.execute("UPDATE projects SET status = 'terminated', end_reason = '체결량 미달' WHERE project_id = ?",
                     (project_id,))
        for d in days:
            record(conn, project_id, "BC-01", t, "void", date=d, evidence={"date": d, "orders": 0, "baseline_daily_avg": 0})
        return True
    for d, n in counts.items():
        ev = {"date": d, "orders": n, "baseline_daily_avg": avg}
        record(conn, project_id, "BC-01", t, "violation" if n >= avg * BC01_MULTIPLE else "void", date=d, evidence=ev)
    return False


def publish(conn, project_id: int) -> list:
    """장 마감 5단계: 아직 공개하지 않은 판정을 모두 공개하고, 이번에 공개한 줄을 돌려준다."""
    rows = conn.execute(
        "SELECT * FROM rule_evaluations WHERE project_id = ? AND published = 0 ORDER BY eval_id", (project_id,)
    ).fetchall()
    conn.execute("UPDATE rule_evaluations SET published = 1 WHERE project_id = ? AND published = 0", (project_id,))
    return rows


def finalize(conn, project_id: int, t: datetime) -> dict:
    """마지막 장 마감: 최종 점수를 한 번 확정한다 (이후 들어온 데이터로 바꾸지 않음)."""
    from .catalog import scores_for_project
    s = scores_for_project(conn, project_id)
    holds = conn.execute(
        "SELECT COUNT(*) FROM rule_evaluations WHERE project_id = ? AND result = 'hold'", (project_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO final_results (project_id, rm_score, pa_score, bc_score, mc_score, overall_score, "
        "unresolved_holds, finalized_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, s["RM"], s["PA"], s["BC"], s["MC"], s["overall"], holds, t.isoformat()),
    )
    conn.execute("UPDATE projects SET status = 'finished' WHERE project_id = ? AND status = 'active'", (project_id,))
    return s
