/**
 * Live market: renders a cohort snapshot and re-renders it whenever the server
 * pushes a new one over Server-Sent Events (/app/market/stream). EventSource
 * reconnects by itself when the server closes the stream after its time limit.
 */
document.addEventListener("DOMContentLoaded", () => {
  const panel = document.getElementById("live-panel");
  const initialEl = document.getElementById("live-initial");
  if (!panel || !initialEl) return;

  const $ = (id) => document.getElementById(id);
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  const lpa = (v) => (v === null || v === undefined ? "-" : `Rs.${Number(v).toFixed(1)} LPA`);
  const conn = $("live-conn");
  let lastId = null;

  function place(role, value, lo, hi) {
    const el = panel.querySelector(`[data-role="${role}"]`);
    if (!el) return;
    if (value === null || value === undefined) { el.hidden = true; return; }
    el.hidden = false;
    el.style.left = `${Math.max(0, Math.min(100, ((value - lo) / (hi - lo)) * 100))}%`;
  }

  function render(s) {
    const c = s.clearing || {};
    $("live-suppressed").hidden = !s.suppressed;
    $("live-bar-wrap").hidden = Boolean(s.suppressed);
    set("live-reason", s.reason || "");
    set("live-p25", lpa(s.p25)); set("live-p50", lpa(s.p50)); set("live-p75", lpa(s.p75));
    set("live-n", s.n); set("live-contrib", s.contributors); set("live-effn", s.effective_n);
    set("live-updated", (s.computed_at || "").slice(0, 16).replace("T", " "));
    set("live-latest", s.latest_month || "-");

    if (!s.suppressed) {
      const pts = [s.p25, s.p75, c.clearing_price_lpa].filter((v) => v !== null && v !== undefined);
      const lo = Math.min(...pts) * 0.88, hi = Math.max(...pts) * 1.12;
      place("p25", s.p25, lo, hi); place("p50", s.p50, lo, hi); place("p75", s.p75, lo, hi);
      place("p25-label", s.p25, lo, hi); place("p50-label", s.p50, lo, hi); place("p75-label", s.p75, lo, hi);
      place("clearing", c.status === "ok" ? c.clearing_price_lpa : null, lo, hi);
      const iqr = panel.querySelector('[data-role="iqr"]');
      const a = ((s.p25 - lo) / (hi - lo)) * 100, b = ((s.p75 - lo) / (hi - lo)) * 100;
      iqr.style.left = `${a}%`; iqr.style.width = `${b - a}%`;
      panel.querySelector('[data-role="p25-label"]').textContent = `P25 ${s.p25.toFixed(1)}`;
      panel.querySelector('[data-role="p50-label"]').textContent = `P50 ${s.p50.toFixed(1)}`;
      panel.querySelector('[data-role="p75-label"]').textContent = `P75 ${s.p75.toFixed(1)}`;
    }

    set("clearing-message", c.message || "");
    set("clearing-price", lpa(c.clearing_price_lpa));
    set("close-price", lpa(c.close_price_lpa));
    set("close-rate", c.close_rate_pct);
    set("decline-p50", c.decline_at_p50_pct === null || c.decline_at_p50_pct === undefined ? "-" : `${Math.round(c.decline_at_p50_pct)}%`);
    set("decline-trend", c.trend_pp === null || c.trend_pp === undefined ? "-" : `${c.trend_pp >= 0 ? "+" : ""}${c.trend_pp.toFixed(1)} pts`);
    set("near-p50", c.near_p50 ? `${c.near_p50.n} offers, ${c.near_p50.decline_pct ?? "-"}% declined` : "-");
    set("clearing-n", `${c.n_decided} (${c.accepted} accepted, ${c.declined} declined)`);
    const curve = $("curve"), body = $("curve-body");
    if (curve && body) {
      curve.hidden = !(c.curve_by_p50 && c.curve_by_p50.length);
      body.innerHTML = "";
      (c.curve_by_p50 || []).forEach((pt) => {
        const tr = document.createElement("tr");
        [`${Math.round(pt.ratio * 100)}% of P50`, pt.lpa.toFixed(1), `${pt.accept_pct}%`].forEach((t, i) => {
          const td = document.createElement("td"); td.textContent = t; if (i) td.className = "num"; tr.appendChild(td);
        });
        body.appendChild(tr);
      });
    }
    $("live-alert").hidden = !s.alert;
    set("live-alert-text", c.message || "");

    if (s.snapshot_id && s.snapshot_id !== lastId && lastId !== null) {
      panel.classList.remove("live-flash"); void panel.offsetWidth; panel.classList.add("live-flash");
    }
    if (s.snapshot_id) lastId = s.snapshot_id;
    else if (lastId === null) lastId = 0;
  }

  render(JSON.parse(initialEl.textContent));

  if (!window.EventSource) { if (conn) conn.textContent = "live updates unsupported"; return; }
  const es = new EventSource(panel.dataset.streamUrl);
  es.addEventListener("snapshot", (e) => render(JSON.parse(e.data)));
  es.onopen = () => { if (conn) { conn.innerHTML = '<i class="fas fa-circle" style="font-size:0.5rem;color:var(--accent);"></i> live'; } };
  es.onerror = () => { if (conn) { conn.innerHTML = '<i class="fas fa-circle" style="font-size:0.5rem;"></i> reconnecting'; } };
});
