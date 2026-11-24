// 시세가 보이는 화면: 장중에는 1초마다 현재가와 등락률을 새로 가져온다.
// 가격이 들어갈 곳에 data-code="종목코드" 와 .q-price / .q-change 를 붙여 두면 된다.
(function () {
  const won = (v) => v == null ? "-" : Number(v).toLocaleString("ko-KR");
  async function refresh() {
    const s = window.rapiClock;
    if (!s || s.status !== "running") return;
    const r = await fetch("/api/quotes");
    if (!r.ok) return;
    const q = await r.json();
    document.querySelectorAll("[data-code]").forEach((el) => {
      const v = q[el.dataset.code];
      if (!v) return;
      const p = el.querySelector(".q-price"), c = el.querySelector(".q-change");
      if (p) p.textContent = won(v.price) + (p.classList.contains("big") ? "원" : "");
      if (c) {
        c.textContent = v.change == null ? "" : (v.change >= 0 ? "+" : "") + v.change.toFixed(1) + "%";
        c.className = c.className.replace(/\b(up|down)\b/g, "").trim() + (v.change >= 0 ? " up" : " down");
      }
    });
  }
  setInterval(refresh, 1000);
})();
