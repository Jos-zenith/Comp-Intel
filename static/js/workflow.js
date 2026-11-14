/**
 * Offer builder: live P25/P50/P75 marker, cost maths, and server-side checks
 * re-run as the recruiter edits (nothing is saved until they click Save).
 */
/**
 * Evaluation tabs (Checks / Cost & runway / ESOP / CTC). Without JS every section
 * stays visible; with it, one at a time, and the choice survives the live re-render.
 */
let activeEvTab = null;
function initEvTabs(root) {
  const nav = root && root.querySelector(".ev-tabnav");
  if (!nav) return;
  const buttons = [...nav.querySelectorAll("[data-tab]")];
  const select = (name) => {
    if (!buttons.some((b) => b.dataset.tab === name)) name = "checks";
    activeEvTab = name;
    buttons.forEach((b) => { b.setAttribute("aria-selected", b.dataset.tab === name ? "true" : "false"); });
    root.querySelectorAll(".ev-tab").forEach((s) => { s.hidden = s.dataset.tab !== name; });
  };
  buttons.forEach((b) => b.addEventListener("click", () => select(b.dataset.tab)));
  nav.hidden = false;
  select(activeEvTab || "checks");
}

document.addEventListener("DOMContentLoaded", () => {
  initEvTabs(document.getElementById("checks-body"));
  const form = document.getElementById("package-form");
  if (!form || form.querySelector("fieldset[disabled]")) return;

  const num = (name) => {
    const el = form.elements[name];
    const v = el ? parseFloat(el.value) : NaN;
    return Number.isFinite(v) ? v : 0;
  };
  const fmt = (v, d = 2) => v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

  const fmv = parseFloat(form.dataset.fmv) || 0;
  const fds = parseFloat(form.dataset.fds) || 0;
  const target = parseFloat(form.dataset.target) || 0;
  const range = document.getElementById("pkg-range");
  const pin = range ? range.querySelector(".mrange-pin") : null;
  const checksBody = document.getElementById("checks-body");
  const submitBtn = document.getElementById("submit-btn");
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };

  function updateLocal() {
    const cash = num("fixed_pay") + num("variable_pay");
    const oneTime = num("joining_bonus") + num("relocation") + num("notice_buyout") + num("other_one_time");
    const options = num("esop_options");
    set("cash-ctc-out", `Rs.${fmt(cash)} LPA`);
    set("gap-out", target ? `${((cash / target - 1) * 100 >= 0 ? "+" : "")}${((cash / target - 1) * 100).toFixed(1)}%` : "-");
    set("cost-recurring", `Rs.${fmt(cash)} LPA`);
    set("cost-onetime", `Rs.${fmt(oneTime)} lakh`);
    set("cost-yearone", `Rs.${fmt(cash + oneTime)} lakh`);
    set("cost-esop", `Rs.${fmt(options * fmv / 1e5)} lakh`);
    set("cost-dilution", fds ? `${(options / fds * 100).toFixed(3)}%` : "-");
    if (range && pin) {
      const lo = parseFloat(range.dataset.lo), hi = parseFloat(range.dataset.hi);
      const pct = Math.max(0, Math.min(100, ((cash - lo) / (hi - lo)) * 100));
      pin.style.left = `${pct}%`;
      const label = pin.querySelector(".pin-label");
      if (label) label.textContent = cash.toFixed(1);
    }
  }

  let timer = null;
  let inflight = null;
  function scheduleChecks() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!checksBody || !form.dataset.evaluateUrl) return;
      if (inflight) inflight.abort();
      inflight = new AbortController();
      try {
        const res = await fetch(form.dataset.evaluateUrl, { method: "POST", body: new FormData(form), signal: inflight.signal });
        if (!res.ok) return;
        checksBody.innerHTML = await res.text();
        initEvTabs(checksBody);
        if (submitBtn) submitBtn.disabled = Boolean(checksBody.querySelector(".ci-block, .flash-error"));
      } catch (err) {
        if (err.name !== "AbortError") console.warn("Live checks failed", err);
      }
    }, 350);
  }

  form.addEventListener("input", () => { updateLocal(); scheduleChecks(); });
  form.addEventListener("change", scheduleChecks);
  updateLocal();
});
