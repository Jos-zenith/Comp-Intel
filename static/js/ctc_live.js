/** Live CTC validation against POST /api/v1/ctc/validate as the sliders move. */
document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("ctc-live");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const cash = $("live-cash"), variable = $("live-var"), city = $("live-city");
  let inflight = null, timer = null;

  const row = (label, value, cls) => {
    const div = document.createElement("div");
    div.className = "explain-row";
    const top = document.createElement("div");
    top.className = "explain-row-top";
    const l = document.createElement("span"); l.className = "explain-row-label"; l.textContent = label;
    const v = document.createElement("span"); v.textContent = value; if (cls) v.className = cls;
    top.append(l, v); div.appendChild(top);
    return div;
  };

  async function check() {
    const c = parseFloat(cash.value), vp = parseFloat(variable.value);
    $("live-cash-out").textContent = `Rs.${c.toFixed(1)} LPA`;
    $("live-var-out").textContent = `${vp}%`;
    if (inflight) inflight.abort();
    inflight = new AbortController();
    try {
      const res = await fetch(root.dataset.api, {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: inflight.signal,
        body: JSON.stringify({ cash_ctc_lpa: c, variable_lpa: +(c * vp / 100).toFixed(2), city: city.value }),
      });
      const data = await res.json();
      if (!res.ok) { $("live-verdict-text").textContent = data.error || "Error"; return; }
      $("live-ruleset").textContent = `${data.ruleset.id} v${data.ruleset.version}`;
      const blocks = data.summary.block, warns = data.summary.warn;
      $("live-verdict-text").textContent = blocks ? `${blocks} blocking issue${blocks > 1 ? "s" : ""}`
        : warns ? `Compliant, ${warns} warning${warns > 1 ? "s" : ""}` : "Compliant";
      $("live-verdict").style.background = blocks ? "linear-gradient(135deg, #b91c1c, #7f1d1d)" : "";
      const rows = $("live-rows"); rows.innerHTML = "";
      data.structure.rows.forEach((r) => rows.appendChild(row(r.component, `Rs.${r.lpa.toFixed(2)} LPA`)));
      rows.appendChild(row("PF / gratuity wage base (statutory)", `Rs.${(data.structure.statutory_wage_inr / 1e5).toFixed(2)} LPA`));
      const checks = $("live-checks"); checks.innerHTML = "";
      data.checks.forEach((ch) => {
        const d = row(ch.title, ch.status.toUpperCase());
        const p = document.createElement("p"); p.className = "result-hint"; p.textContent = ch.detail; d.appendChild(p);
        checks.appendChild(d);
      });
      $("live-disclaimer").textContent = data.disclaimer;
    } catch (err) {
      if (err.name !== "AbortError") $("live-verdict-text").textContent = "Couldn't reach the validation API.";
    }
  }
  const schedule = () => { clearTimeout(timer); timer = setTimeout(check, 120); };
  [cash, variable, city].forEach((el) => el.addEventListener("input", schedule));
  check();
});
