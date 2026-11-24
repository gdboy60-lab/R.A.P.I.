// 상단 시계 바: 장중에 1초마다 서버의 시계를 진행시키고 화면을 갱신한다.
// 화면이 닫히면 호출이 멈추므로 시계도 멈춘다 (서버가 한 번에 최대 5초만 인정).
(function () {
  const $ = (id) => document.getElementById(id);
  const SPEEDS = [1, 5, 10, 30];
  const LABEL = { pre_open: "장 시작 전", running: "장중", paused: "일시정지됨", closed: "장 마감" };
  let state = null;

  function render(s) {
    if (!s) return;
    if (s.error) { alert(s.error); return; }
    const before = state;
    state = s;
    window.rapiClock = s;
    $("clock-day").textContent = `${s.day}일차 / ${s.total_days}일 · ${LABEL[s.status]}`;
    $("clock-time").textContent = s.time;
    $("btn-speed").textContent = `${s.speed}×`;
    $("btn-toggle").textContent = s.status === "paused" ? "▶" : "❚❚";
    const live = ["running", "paused"].includes(s.status);
    $("btn-open").hidden = s.status !== "pre_open";
    $("btn-toggle").hidden = !live;
    $("btn-speed").hidden = !live;
    $("btn-skip").hidden = !live;
    // 장 마감 뒤에는 결과 화면으로 가는 버튼만 (다음 거래일은 결과 화면에서만 진행)
    $("btn-next").hidden = s.status !== "closed";
    // 장 마감에 닿는 순간 결과 화면으로 자동 전환 (명세서 12-1)
    if (before && before.status !== "closed" && s.status === "closed") {
      location.href = "/close";
      return;
    }
    // 지정가 체결, 장 열림, 날짜 변경처럼 화면 내용이 바뀌면 새로고침한다
    const changed = before && (before.status !== s.status || before.day !== s.day);
    if ((s.events && s.events.some((e) => e.startsWith("order_filled"))) || changed) {
      location.reload();
    }
  }

  async function call(method, path) {
    const r = await fetch(path, { method });
    render(await r.json());
  }

  $("btn-open").onclick = () => call("POST", "/api/clock/open");
  $("btn-toggle").onclick = () =>
    call("POST", state.status === "paused" ? "/api/clock/resume" : "/api/clock/pause");
  $("btn-speed").onclick = () => {
    const next = SPEEDS[(SPEEDS.indexOf(state.speed) + 1) % SPEEDS.length];
    call("POST", `/api/clock/speed?speed=${next}`);
  };
  $("btn-skip").onclick = () => { $("btn-skip").disabled = true; call("POST", "/api/clock/skip"); };
  $("btn-next").onclick = () => { location.href = "/close"; };

  call("GET", "/api/clock");
  setInterval(() => { if (state && state.status === "running") call("GET", "/api/clock"); }, 1000);
})();
