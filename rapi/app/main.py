"""R.A.P.I 웹 서버 (FastAPI).

실행:  python -m uvicorn app.main:app --reload
지금 있는 화면: 로그인 → 프로젝트 시작 → 홈 · 시장 · 종목 상세(주문) · 거래 기록 · 장 마감 결과 · 피드백 · 최종 리포트
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import settings
from .db import DEFAULT_DB_PATH, connect, init_db
from .engine.clock import CLOSED, ClockError
from .engine.clock_store import clock_view, load_clock, save_clock
from .engine.prices import ReplayFeed
from .ai import feedback as ai_feedback
from .ai import report as ai_report
from .day_close import close_day
from .engine.trading import (OrderError, TickWatcher, available_cash, cancel_order,
                             place_order, sellable_qty)
from .projects import ProjectError, create_guest, create_project
from .rules.catalog import RULES, area_scores, scores_for_project
from .rules.describe import describe

BASE = Path(__file__).resolve().parent


def ensure_db(db_path=DEFAULT_DB_PATH) -> bool:
    """rapi.db 가 없으면 테이블을 만들고 개발용 예시 기간을 넣는다.

    rapi.db 는 재배포 금지 때문에 깃허브에 올리지 않으므로, 저장소를 새로 받은 직후에도
    서버가 바로 켜지게 하려는 것이다. 이미 있으면 아무것도 하지 않는다.
    """
    if Path(db_path).exists():
        return False
    from .seed_dev import seed
    conn = connect(db_path)
    try:
        init_db(conn)
        seed(conn)
    finally:
        conn.close()
    return True


ensure_db()
# debug=True: 오류가 나면 브라우저에 원인(Traceback)을 보여준다. 개발 중에만 켜고, 공개 시연 때는 False 로 바꾼다.
app = FastAPI(title="R.A.P.I", debug=True)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["won"] = lambda v: "-" if v is None else f"{int(v):,}"
# css·js 를 고치면 주소 뒤 숫자가 바뀌어서, 브라우저가 예전 파일을 쓰지 않고 새로 받는다
templates.env.globals["static_v"] = lambda name: int((BASE / "static" / name).stat().st_mtime)

COOKIE = "rapi_project"   # 체험 계정: 이 기기(브라우저)에서만 이어보기 (FR-32). 시연용 단순 방식.


class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
def _need_login(request: Request, exc: NeedLogin):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "로그인이 필요해요"}, status_code=401)
    return RedirectResponse("/logout", status_code=303)


def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def current_project(request: Request, conn=Depends(get_conn)) -> int:
    """쿠키의 프로젝트가 실제로 있는지까지 확인한다 (DB를 새로 만든 뒤 남은 쿠키 대비)."""
    pid = request.cookies.get(COOKIE)
    if not pid or not pid.isdigit() or not conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?", (int(pid),)).fetchone():
        raise NeedLogin()
    return int(pid)


def _int(v) -> int | None:
    v = (v or "").replace(",", "").strip()
    return int(v) if v.isdigit() else None


def _stocks(conn, project_id: int):
    return conn.execute(
        "SELECT s.stock_code, s.display_name FROM replay_stocks s JOIN projects p USING (period_id) "
        "WHERE p.project_id = ? ORDER BY s.display_name",
        (project_id,),
    ).fetchall()


def _source_note(conn, project_id: int) -> str:
    label = conn.execute(
        "SELECT r.label FROM replay_periods r JOIN projects p USING (period_id) WHERE p.project_id = ?",
        (project_id,),
    ).fetchone()[0]
    return settings.DEV_DATA_NOTE if label.startswith("개발용") else settings.DATA_SOURCE_NOTE


def _quote_view(feed: ReplayFeed, code: str, t: datetime, pre_open: bool) -> dict:
    q = feed.quote(code, t)
    if q is None:
        return {"price": None, "change": None}
    price = q.prev_close if pre_open and q.prev_close else q.price
    change = (price / q.prev_close - 1) * 100 if q.prev_close else None
    return {"price": price, "change": change}


def _page(request, template, conn, project_id, **ctx):
    clock = load_clock(conn, project_id)
    ctx.update(clock=clock_view(clock), source_note=_source_note(conn, project_id),
               error=request.query_params.get("error"), ok=request.query_params.get("ok"))
    return templates.TemplateResponse(request, template, ctx)


# ------------------------------------------------------------------ 시작
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if request.cookies.get(COOKIE):
        return RedirectResponse("/home", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.get("/logout")
def logout():
    """체험 기록 쿠키를 지우고 로그인 화면으로. 시연 중 처음으로 돌아갈 때 쓴다."""
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie("rapi_user")
    return resp


@app.post("/guest")
def start_as_guest(conn=Depends(get_conn)):
    user_id = create_guest(conn)
    resp = RedirectResponse("/start", status_code=303)
    resp.set_cookie("rapi_user", str(user_id), max_age=60 * 60 * 24 * 60)
    return resp


@app.get("/start", response_class=HTMLResponse)
def choose_period(request: Request, conn=Depends(get_conn)):
    periods = conn.execute(
        "SELECT p.period_id, p.label, p.trading_days, COUNT(s.stock_code) AS n_stocks "
        "FROM replay_periods p LEFT JOIN replay_stocks s USING (period_id) "
        "GROUP BY p.period_id ORDER BY p.period_id"
    ).fetchall()
    return templates.TemplateResponse(request, "start.html", {"periods": periods})


@app.post("/start/{period_id}")
def start_project(period_id: int, request: Request, conn=Depends(get_conn)):
    user_id = request.cookies.get("rapi_user")
    if not user_id or not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (int(user_id),)).fetchone():
        return RedirectResponse("/logout", status_code=303)
    try:
        project_id = create_project(conn, int(user_id), period_id)
    except ProjectError as e:
        # 이미 진행 중인 프로젝트가 있으면 오류 대신 그 프로젝트로 이어간다
        row = conn.execute(
            "SELECT project_id FROM projects WHERE user_id = ? AND status IN ('pending', 'active')",
            (int(user_id),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail=str(e))
        project_id = row[0]
    resp = RedirectResponse("/home", status_code=303)
    resp.set_cookie(COOKIE, str(project_id), max_age=60 * 60 * 24 * 60)
    return resp


# ------------------------------------------------------------------ 탭 화면
@app.get("/home", response_class=HTMLResponse)
def home(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    feed, pre = ReplayFeed(conn), clock.status == "pre_open"
    acc = conn.execute("SELECT account_id, cash FROM accounts WHERE project_id = ?", (project_id,)).fetchone()
    names = dict((r[0], r[1]) for r in _stocks(conn, project_id))
    holdings = []
    for h in conn.execute(
            "SELECT h.stock_code, h.quantity, p.stop_price, p.target_price, t.stop_reached_at "
            "FROM holdings h LEFT JOIN trades t ON t.trade_id = h.trade_id "
            "LEFT JOIN plans p ON p.plan_id = t.plan_id WHERE h.account_id = ? AND h.quantity > 0",
            (acc[0],)).fetchall():
        qv = _quote_view(feed, h["stock_code"], clock.sim_now, pre)
        holdings.append({**dict(h), "name": names.get(h["stock_code"], h["stock_code"]), **qv})
    total = acc[1] + sum((h["price"] or 0) * h["quantity"] for h in holdings)
    scores = scores_for_project(conn, project_id, published_only=True)   # 장중에는 공개된 감점만
    return _page(request, "home.html", conn, project_id, tab="home", finished=_finished(conn, project_id),
                 scores=scores, cash=acc[1], total=total, holdings=holdings)


@app.get("/market", response_class=HTMLResponse)
def market(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    feed, pre = ReplayFeed(conn), clock.status == "pre_open"
    watched = {r[0] for r in conn.execute(
        "SELECT stock_code FROM watchlist WHERE project_id = ? AND removed_at IS NULL", (project_id,))}
    rows = [{"code": s[0], "name": s[1], "watched": s[0] in watched,
             **_quote_view(feed, s[0], clock.sim_now, pre)} for s in _stocks(conn, project_id)]
    only_watch = request.query_params.get("filter") == "watch"
    if only_watch:
        rows = [r for r in rows if r["watched"]]
    return _page(request, "market.html", conn, project_id, tab="market", rows=rows,
                 n_watch=len(watched), n_all=len(_stocks(conn, project_id)), only_watch=only_watch,
                 today_action=_latest_action(conn, project_id, clock.day_number))


@app.post("/watch/{code}")
def toggle_watch(code: str, request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    back = request.headers.get("referer") or "/market"
    existing = conn.execute(
        "SELECT watch_id FROM watchlist WHERE project_id = ? AND stock_code = ? AND removed_at IS NULL",
        (project_id, code),
    ).fetchone()
    # 장 시작 전에 넣은 종목은 '당일 장 시작 전 등록'으로 인정된다 (PA-04).
    # 장 시작 전 화면의 시각은 개장 시각과 같으므로 1초 앞으로 기록해 개장 전임을 분명히 한다.
    t = clock.sim_now - timedelta(seconds=1) if clock.status == "pre_open" else clock.sim_now
    if existing:
        conn.execute("UPDATE watchlist SET removed_at = ? WHERE watch_id = ?", (t.isoformat(), existing[0]))
    else:
        try:
            conn.execute("INSERT INTO watchlist (project_id, stock_code, added_at) VALUES (?, ?, ?)",
                         (project_id, code, t.isoformat()))
        except Exception:
            return RedirectResponse(f"/market?error={urlquote('관심 종목은 20개까지예요')}", status_code=303)
    return RedirectResponse(back, status_code=303)


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock(code: str, request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    names = dict((r[0], r[1]) for r in _stocks(conn, project_id))
    if code not in names:
        raise HTTPException(status_code=404, detail="이 기간의 종목이 아니에요")
    feed, pre = ReplayFeed(conn), clock.status == "pre_open"
    qv = _quote_view(feed, code, clock.sim_now, pre)
    q = feed.quote(code, clock.sim_now)
    # 오늘 장 시작부터 지금까지 2분 간격 가격 (차트)
    chart = []
    if q and not pre:
        start = clock.today.open_at
        t = start
        while t <= clock.sim_now:
            chart.append(feed.quote(code, t).price)
            t += timedelta(minutes=2)
        chart.append(q.price)
    acc = conn.execute("SELECT account_id, cash FROM accounts WHERE project_id = ?", (project_id,)).fetchone()
    watched = conn.execute(
        "SELECT added_at FROM watchlist WHERE project_id = ? AND stock_code = ? AND removed_at IS NULL",
        (project_id, code)).fetchone()
    open_trade = conn.execute(
        "SELECT t.trade_id, p.reason, p.target_price, p.stop_price, p.target_qty FROM trades t "
        "LEFT JOIN plans p ON p.plan_id = t.plan_id "
        "WHERE t.account_id = ? AND t.stock_code = ? AND t.status = 'open'", (acc[0], code)).fetchone()
    held = conn.execute("SELECT quantity FROM holdings WHERE account_id = ? AND stock_code = ?",
                        (acc[0], code)).fetchone()
    return _page(request, "stock.html", conn, project_id, tab="market", code=code, name=names[code],
                 prev_close=q.prev_close if q else None, chart=chart, watched=watched,
                 open_trade=open_trade, held=held[0] if held else 0,
                 can_buy=available_cash(conn, acc[0], acc[1]), can_sell=sellable_qty(conn, acc[0], code),
                 side=request.query_params.get("side", "buy"), **qv)


@app.post("/order")
def order(request: Request, code: str = Form(...), side: str = Form(...), order_type: str = Form(...),
          qty: str = Form(""), limit_price: str = Form(""), reason: str = Form(""),
          target_price: str = Form(""), stop_price: str = Form(""), target_qty: str = Form(""),
          conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    feed, was = ReplayFeed(conn), clock.status
    # 주문 전에 시계를 지금까지 진행시켜, 주문 시각과 가격이 화면과 맞도록 한다
    clock.advance(datetime.now(), TickWatcher(conn, feed, project_id))
    _after_clock(conn, project_id, clock, was)
    plan = {"reason": reason.strip(), "target_price": _int(target_price),
            "stop_price": _int(stop_price), "target_qty": _int(target_qty)}
    try:
        r = place_order(conn, feed, project_id, clock.sim_now, clock.status, code, side, order_type,
                        _int(qty) or 0, _int(limit_price), plan if side == "buy" else None)
    except OrderError as e:
        save_clock(conn, project_id, clock)
        return RedirectResponse(f"/stock/{code}?side={side}&error={urlquote(str(e))}", status_code=303)
    save_clock(conn, project_id, clock)
    msg = "체결됐어요" if r["filled"] else "주문이 접수됐어요. 지정가에 닿으면 체결돼요"
    return RedirectResponse(f"/stock/{code}?side={side}&ok={urlquote(msg)}", status_code=303)


@app.post("/order/{order_id}/cancel")
def cancel(order_id: int, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    try:
        cancel_order(conn, project_id, order_id, clock.sim_now)
    except OrderError as e:
        return RedirectResponse(f"/trades?error={urlquote(str(e))}", status_code=303)
    return RedirectResponse(f"/trades?ok={urlquote('주문을 취소했어요')}", status_code=303)


@app.get("/trades", response_class=HTMLResponse)
def trades(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    feed, pre = ReplayFeed(conn), clock.status == "pre_open"
    acc = conn.execute("SELECT account_id FROM accounts WHERE project_id = ?", (project_id,)).fetchone()[0]
    names = dict((r[0], r[1]) for r in _stocks(conn, project_id))
    day_of = {r[0]: i + 1 for i, r in enumerate(conn.execute(
        "SELECT c.date FROM market_calendar c JOIN projects p ON c.date BETWEEN p.start_date AND p.end_date "
        "WHERE p.project_id = ? AND c.is_open = 1 ORDER BY c.date", (project_id,)))}

    def when(ts):
        if not ts:
            return ""
        return f"{day_of.get(ts[:10], '?')}일차 {ts[11:16]}"

    open_orders = [{**dict(o), "name": names.get(o["stock_code"]), "when": when(o["submitted_at"])}
                   for o in conn.execute("SELECT * FROM orders WHERE account_id = ? AND status = 'open' "
                                         "ORDER BY order_id DESC", (acc,))]
    rows = conn.execute(
        "SELECT t.*, p.reason, p.target_price, p.stop_price, p.target_qty, h.quantity AS qty, "
        " (SELECT COUNT(*) FROM plan_revisions r WHERE r.plan_id = p.plan_id) AS revisions "
        "FROM trades t LEFT JOIN plans p ON p.plan_id = t.plan_id "
        "LEFT JOIN holdings h ON h.account_id = t.account_id AND h.stock_code = t.stock_code "
        "WHERE t.account_id = ? ORDER BY t.trade_id DESC", (acc,)).fetchall()
    open_trades, closed_trades = [], []
    for t in rows:
        d = {**dict(t), "name": names.get(t["stock_code"]), "opened": when(t["opened_at"]),
             "closed": when(t["closed_at"])}
        if t["status"] == "open":
            cost = conn.execute(
                "SELECT SUM(e.price * e.quantity + e.fee), SUM(e.quantity) FROM executions e "
                "JOIN orders o USING (order_id) WHERE o.trade_id = ? AND o.side = 'buy'", (t["trade_id"],)).fetchone()
            qv = _quote_view(feed, t["stock_code"], clock.sim_now, pre)
            d["avg"] = cost[0] / cost[1] if cost[1] else None
            d["eval_pnl"] = (qv["price"] * (t["qty"] or 0) - cost[0]) if qv["price"] and cost[0] else None
            open_trades.append(d)
        else:
            closed_trades.append(d)
    return _page(request, "trades.html", conn, project_id, tab="trades",
                 open_orders=open_orders, open_trades=open_trades, closed_trades=closed_trades)


# ------------------------------------------------------------------ 장 마감 결과
def _require_active(conn, project_id: int) -> None:
    status = conn.execute("SELECT status FROM projects WHERE project_id = ?", (project_id,)).fetchone()[0]
    if status != "active":
        raise ClockError("끝난 프로젝트예요")


@app.get("/close", response_class=HTMLResponse)
def close_result(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    """장 마감 결과: 그날 공개된 판정과 점수 변화 (명세서 10-3, 12-2)."""
    clock = load_clock(conn, project_id)
    if clock.status != CLOSED:
        return RedirectResponse("/home", status_code=303)
    today = clock.today.date.isoformat()
    names = dict((r[0], r[1]) for r in _stocks(conn, project_id))
    rows = conn.execute(
        "SELECT * FROM rule_evaluations WHERE project_id = ? AND published = 1 "
        "AND substr(COALESCE(updated_at, evaluated_at), 1, 10) = ? AND result IN ('violation', 'hold') "
        "ORDER BY (result = 'violation') DESC, penalty DESC, eval_id", (project_id, today)).fetchall()
    items = [{"rule_id": r["rule_id"], "area": r["area"], "name": RULES[r["rule_id"]].name, "result": r["result"],
              "penalty": r["penalty"], "eval_id": r["eval_id"], "text": describe(r, names)} for r in rows]
    before = area_scores([(r[0], r[1]) for r in conn.execute(
        "SELECT area, penalty FROM rule_evaluations WHERE project_id = ? AND result = 'violation' AND published = 1 "
        "AND substr(COALESCE(updated_at, evaluated_at), 1, 10) < ?", (project_id, today))])
    after = scores_for_project(conn, project_id, published_only=True)
    acc = conn.execute("SELECT account_id FROM accounts WHERE project_id = ?", (project_id,)).fetchone()[0]
    cancelled = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE account_id = ? AND cancel_reason = 'market_close' "
        "AND substr(closed_at, 1, 10) = ?", (acc, today)).fetchone()[0]
    p = conn.execute("SELECT status, end_reason, baseline_daily_avg FROM projects WHERE project_id = ?",
                     (project_id,)).fetchone()
    return templates.TemplateResponse(request, "close.html", {
        "clock": clock_view(clock), "hide_clock": True, "items": items, "before": before, "after": after,
        "n_violations": sum(1 for i in items if i["result"] == "violation"),
        "total_penalty": sum(i["penalty"] for i in items), "cancelled": cancelled,
        "status": p["status"], "end_reason": p["end_reason"], "baseline_avg": p["baseline_daily_avg"],
        "areas": [("RM", "위험 관리"), ("PA", "계획 준수"), ("BC", "행동 일관성"), ("MC", "시장 상황")],
        "feedback": _saved_feedback(conn, project_id, clock.day_number),
    })


@app.post("/next")
def next_day(conn=Depends(get_conn), project_id: int = Depends(current_project)):
    """다음 거래일은 장 마감 결과 화면에서만 진행한다."""
    clock = load_clock(conn, project_id)
    try:
        _require_active(conn, project_id)
        clock.next_day()
    except ClockError:
        return RedirectResponse("/close", status_code=303)
    save_clock(conn, project_id, clock)
    return RedirectResponse("/market", status_code=303)


# ------------------------------------------------------------------ AI 피드백
def _saved_feedback(conn, project_id: int, day_number: int):
    row = conn.execute(
        "SELECT * FROM ai_feedbacks WHERE project_id = ? AND kind = 'daily' AND trading_day = ? "
        "AND status IN ('ok', 'failed')", (project_id, day_number)).fetchone()
    return ai_feedback._view(row) if row else None


def _latest_action(conn, project_id: int, day_number: int):
    """전날 피드백의 '내일 해볼 행동' (장 시작 전 화면·피드백 탭 상단)."""
    fb = _saved_feedback(conn, project_id, day_number - 1)
    return fb["tomorrow_action"] if fb else None


@app.post("/api/feedback/today", response_class=HTMLResponse)
def feedback_today(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    """장 마감 결과 화면이 열린 뒤 부른다. 점수는 이미 공개됐고, 피드백만 이어서 만든다 (명세서 11-6)."""
    clock = load_clock(conn, project_id)
    if clock.status != CLOSED:
        raise HTTPException(status_code=409, detail="장 마감 뒤에만 만들 수 있어요")
    fb = ai_feedback.generate_daily(conn, project_id, clock.day_number, clock.today.date.isoformat())
    conn.commit()
    return templates.TemplateResponse(request, "_feedback.html", {"fb": fb})


@app.get("/feedback", response_class=HTMLResponse)
def feedback_list(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    rows = conn.execute(
        "SELECT * FROM ai_feedbacks WHERE project_id = ? AND kind = 'daily' AND status IN ('ok', 'failed') "
        "ORDER BY trading_day DESC", (project_id,)).fetchall()
    items = []
    for r in rows:
        fb = ai_feedback._view(r)
        day = conn.execute(
            "SELECT c.date FROM market_calendar c JOIN projects p ON c.date BETWEEN p.start_date AND p.end_date "
            "WHERE p.project_id = ? AND c.is_open = 1 ORDER BY c.date LIMIT 1 OFFSET ?",
            (project_id, r["trading_day"] - 1)).fetchone()[0]
        n = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(penalty), 0) FROM rule_evaluations WHERE project_id = ? "
            "AND result = 'violation' AND published = 1 AND substr(COALESCE(updated_at, evaluated_at), 1, 10) = ?",
            (project_id, day)).fetchone()
        items.append({**fb, "n_violations": n[0], "penalty": n[1]})
    today_action = None
    if clock.status == CLOSED:
        today_action = None
    elif items and items[0]["trading_day"] == clock.day_number - 1:
        today_action = items[0]["tomorrow_action"]
    return _page(request, "feedback.html", conn, project_id, tab="feedback", items=items, today_action=today_action)


# ------------------------------------------------------------------ 최종 리포트
def _finished(conn, project_id: int) -> bool:
    s = conn.execute("SELECT status FROM projects WHERE project_id = ?", (project_id,)).fetchone()[0]
    return s in ("finished", "terminated")


def _chart(daily: list[dict], baseline_days: int = ai_report.BASELINE_DAYS) -> dict:
    """일차별 감점 막대 그래프의 좌표 (SVG는 템플릿에서 그린다)."""
    W, H, top, bottom = 358, 150, 22, 24
    n = max(len(daily), 1)
    slot = W / n
    bw = min(22, slot * 0.6)
    peak = max([d["penalty"] for d in daily] + [1])
    plot_h = H - top - bottom
    bars = []
    for i, d in enumerate(daily):
        h = max(2, round(d["penalty"] / peak * plot_h)) if d["penalty"] else 2
        x = i * slot + (slot - bw) / 2
        bars.append({**d, "x": round(x, 1), "w": round(bw, 1), "y": H - bottom - h, "h": h,
                     "cx": round(x + bw / 2, 1), "hx": round(i * slot, 1), "hw": round(slot, 1)})
    divider = round(baseline_days * slot, 1) if len(daily) > baseline_days else None
    return {"W": W, "H": H, "base_y": H - bottom, "bars": bars, "divider": divider, "slot": round(slot, 1)}


def _saved_report(conn, project_id: int):
    row = conn.execute("SELECT output_text, model, status FROM ai_feedbacks WHERE project_id = ? AND kind = 'final' "
                       "AND status IN ('ok', 'failed')", (project_id,)).fetchone()
    if not row:
        return None
    return {**json.loads(row["output_text"]), "model": row["model"], "status": row["status"]}


def _ai_view(ai: dict) -> dict:
    """AI 문장을 화면에 끼우기 쉬운 모양으로 (규칙별 설명은 rule_id로 찾는다)."""
    return {"repeated": {r["rule_id"]: {"text": r["text"], "eval_ids": r["eval_ids"]} for r in ai["repeated"]},
            "change": ai.get("change"), "focus": ai["focus"],
            "is_ai": ai["model"] != "rule-based", "status": ai["status"]}


@app.get("/report", response_class=HTMLResponse)
def report_page(request: Request, conn=Depends(get_conn), project_id: int = Depends(current_project)):
    """최종 리포트 (명세서 11-7, 12-2). 기간이 끝났거나 조기 종료된 프로젝트만."""
    if not _finished(conn, project_id):
        return RedirectResponse("/home", status_code=303)
    data = ai_report.build_report_data(conn, project_id)
    saved = _saved_report(conn, project_id)
    return templates.TemplateResponse(request, "report.html", {
        "hide_clock": True, "d": data, "chart": _chart(data["daily"]),
        "ai": _ai_view(saved) if saved else None,
        "areas": [("RM", "위험 관리"), ("PA", "계획 준수"), ("BC", "행동 일관성"), ("MC", "시장 상황")],
        "source_note": _source_note(conn, project_id),
    })


@app.post("/api/report")
def report_ai(conn=Depends(get_conn), project_id: int = Depends(current_project)):
    """리포트 화면이 열린 뒤 부른다. 숫자는 먼저 보여 주고 AI 설명만 이어서 채운다."""
    if not _finished(conn, project_id):
        raise HTTPException(status_code=409, detail="프로젝트가 끝난 뒤에만 만들 수 있어요")
    out = ai_report.generate_final(conn, project_id)
    conn.commit()
    return JSONResponse(_ai_view(out["ai"]))


# ------------------------------------------------------------------ API
@app.get("/api/quotes")
def quotes(conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    feed, pre = ReplayFeed(conn), clock.status == "pre_open"
    return {s[0]: _quote_view(feed, s[0], clock.sim_now, pre) for s in _stocks(conn, project_id)}


def _after_clock(conn, project_id: int, clock, was: str) -> None:
    """장 마감에 처음 닿은 순간 장 마감 처리를 한 번만 실행한다 (명세서 10-2)."""
    if was != CLOSED and clock.status == CLOSED:
        close_day(conn, project_id, clock)


@app.get("/api/clock")
def get_clock(conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    was = clock.status
    watcher = TickWatcher(conn, ReplayFeed(conn), project_id)
    clock.advance(datetime.now(), watcher)
    save_clock(conn, project_id, clock)
    _after_clock(conn, project_id, clock, was)
    return {**clock_view(clock), "events": watcher.events}


@app.post("/api/clock/{action}")
def control_clock(action: str, speed: int | None = None,
                  conn=Depends(get_conn), project_id: int = Depends(current_project)):
    clock = load_clock(conn, project_id)
    was, now = clock.status, datetime.now()
    watcher = TickWatcher(conn, ReplayFeed(conn), project_id)
    try:
        if action == "open":
            clock.open_market(now)
        elif action == "pause":
            clock.pause(now, watcher)
        elif action == "resume":
            clock.resume(now)
        elif action == "speed" and speed:
            clock.set_speed(speed, now, watcher)
        elif action == "skip":
            clock.skip_to_close(watcher)
        elif action == "next":
            _require_active(conn, project_id)
            clock.next_day()
        else:
            raise HTTPException(status_code=400, detail=f"알 수 없는 조작: {action}")
    except ClockError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    save_clock(conn, project_id, clock)
    _after_clock(conn, project_id, clock, was)
    return {**clock_view(clock), "events": watcher.events}
