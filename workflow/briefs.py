"""
Offer briefs (OF-01 / OF-02).

internal_brief_pdf: everything an approver needs - benchmark, cohort,
confidence, checks, route, approvals, snapshot hash.
candidate_summary: only the package itself. Deliberately takes the package and
company display fields, never the benchmark, so peer data, internal bands and
model internals can't leak into it by accident.
"""
from __future__ import annotations

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .checks import DISCLAIMER
from .esop import scenarios as esop_scenarios

_REPLACEMENTS = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "₹": "Rs.", "·": "-"}

DEMO_LINE = "DEMO ENVIRONMENT - benchmarks use a synthetic, benchmark-calibrated dataset, not real market data."


def pdf_safe(text) -> str:
    text = str(text)
    for char, rep in _REPLACEMENTS.items():
        text = text.replace(char, rep)
    return text.encode("latin-1", errors="replace").decode("latin-1")


class _BriefPDF(FPDF):
    ACCENT = (255, 122, 89)
    TEXT = (26, 29, 36)
    MUTED = (91, 98, 112)

    def __init__(self, title: str, subtitle: str, footer_text: str, watermark: str | None = None):
        super().__init__()
        self._title, self._subtitle, self._footer = title, subtitle, footer_text
        self._watermark = watermark
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        if self._watermark:
            # Diagonal watermark so a demo document can't pass for a real one.
            self.set_font("Helvetica", "B", 44)
            self.set_text_color(235, 228, 226)
            with self.rotation(35, 105, 160):
                self.set_xy(5, 150)
                self.cell(200, 20, pdf_safe(self._watermark), align="C")
        self.set_fill_color(*self.ACCENT)
        self.rect(0, 0, 210, 22, "F")
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 15)
        self.set_xy(12, 5)
        self.cell(0, 8, pdf_safe(self._title))
        self.set_font("Helvetica", "", 9)
        self.set_xy(12, 13)
        self.cell(0, 6, pdf_safe(self._subtitle))
        self.set_y(28)

    def footer(self):
        self.set_y(-18)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        self.multi_cell(0, 4, pdf_safe(f"{self._footer}  |  Page {self.page_no()}"), align="C")

    def section(self, title):
        self.ln(3)
        self.set_text_color(*self.TEXT)
        self.set_font("Helvetica", "B", 11)
        self.cell(0, 7, pdf_safe(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(225, 227, 235)
        self.line(self.l_margin, self.get_y(), 210 - self.r_margin, self.get_y())
        self.ln(2)

    def kv(self, key, value, bold=False):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*self.MUTED)
        self.cell(62, 6, pdf_safe(key))
        self.set_text_color(*self.TEXT)
        self.set_font("Helvetica", "B" if bold else "", 9)
        self.multi_cell(0, 6, pdf_safe(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def para(self, text, size=8.5, italic=False):
        self.set_font("Helvetica", "I" if italic else "", size)
        self.set_text_color(*self.MUTED)
        self.multi_cell(0, 4.6, pdf_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _bytes(pdf: FPDF) -> bytes:
    out = pdf.output()
    return out.encode("latin-1") if isinstance(out, str) else bytes(out)


def _lpa(v):
    return f"Rs.{v:,.2f} LPA"


def internal_brief_pdf(*, company, request_row, submission, snapshot: dict, route, actions, state) -> bytes:
    bm, pkg, ev = snapshot["benchmark"], snapshot["package"], snapshot["evaluation"]
    demo = bm.get("data_mode", "demo") == "demo"
    pdf = _BriefPDF("Internal offer approval brief",
                    f"{company['name']} - {request_row['role']} ({request_row['level']}), {request_row['city']} - "
                    f"revision {submission['revision']}",
                    f"CONFIDENTIAL - internal only. {DEMO_LINE if demo else 'Decision support only.'}",
                    watermark="DEMO DATA - NOT FOR HIRING" if demo else None)
    pdf.add_page()
    if demo:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(183, 28, 28)
        pdf.multi_cell(0, 5, pdf_safe("Demo data - not for live hiring decisions. This benchmark comes from a synthetic "
                                      "dataset."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.section("Candidate & role")
    pdf.kv("Candidate", f"{request_row['candidate_name'] or '[purged]'} ({request_row['candidate_pseudo_id']})")
    pdf.kv("Role / level / city", f"{request_row['role']} / {request_row['level']} / {request_row['city']}")
    pdf.kv("Experience", f"{request_row['years_experience']:g} years (model level: {bm['inputs']['model_level']})")
    if request_row["expected_ctc"]:
        pdf.kv("Candidate expectation", _lpa(request_row["expected_ctc"]))
    if request_row["competing_offer"]:
        pdf.kv("Competing offer", _lpa(request_row["competing_offer"]))
    pdf.kv("Approval status", state["state"].replace("_", " ").title(), bold=True)

    pdf.section("Proposed package")
    pdf.kv("Cash CTC", f"{_lpa(pkg['cash_ctc'])}  (fixed {_lpa(pkg['fixed_pay'])} + variable {_lpa(pkg['variable_pay'])})", bold=True)
    pdf.kv("ESOP", f"{pkg['esop_options']:,} options, {pkg['vesting_years']:g}-year vest, {pkg['cliff_months']}-month cliff")
    costs = ev["costs"]
    pdf.kv("One-time cash", f"Rs.{costs['one_time_cash']:,.2f} lakh (joining {pkg['joining_bonus']:g}, relocation "
                            f"{pkg['relocation']:g}, notice buyout {pkg['notice_buyout']:g}, other {pkg['other_one_time']:g})")
    pdf.kv("Year-one cash cost", f"Rs.{costs['year_one_cash']:,.2f} lakh")
    pdf.kv("Recurring cash cost", _lpa(costs["recurring_cash"]))
    if costs.get("dilution_pct") is not None:
        pdf.kv("ESOP dilution", f"{costs['dilution_pct']:.3f}% fully diluted (grant value Rs.{costs['esop_grant_value_lakh']:,.2f} lakh at FMV)")
    if pkg.get("override_reason"):
        pdf.kv("Override reason", f"{pkg['override_reason']} {pkg.get('override_note') or ''}")
    imp = ev.get("impact")
    if imp:
        pdf.kv("Monthly burn increase", f"Rs.{imp['monthly_burn_increase_lakh']:,.2f} lakh")
        if imp.get("available") and imp.get("runway_after") is not None:
            before = f"{imp['runway_before']:.1f}" if imp.get("runway_before") is not None else "break-even"
            pdf.kv("Runway", f"{before} -> {imp['runway_after']:.1f} months")
        else:
            pdf.kv("Runway", "Unknown - cash in bank and monthly burn not on file")
        if imp.get("plan_variance_pct") is not None:
            pdf.kv("vs hiring-plan average", f"{imp['plan_variance_pct']:+.1f}% (Rs.{imp['per_hire_budget_lakh']:,.2f} lakh per hire)")
    es = ev.get("esop_scenarios")
    if es:
        pdf.kv("ESOP scenarios (illustrative)", ", ".join(f"{s['name']} Rs.{s['gross_lakh']:,.2f} lakh"
                                                          for s in es["scenarios"])
               + f"; strike Rs.{es['strike_price_inr']:,.2f}, exercise cost Rs.{es['exercise_cost_lakh']:,.2f} lakh")

    pdf.section("Benchmark")
    disp = bm.get("display") or {"mode": "full", "show_quantiles": True, "show_target": True,
                                 "lo": bm.get("p25"), "hi": bm.get("p75"), "notice": None}
    if disp["mode"] == "none":
        pdf.kv("Market range", "None - not enough reliable data. Package entered manually.", bold=True)
    else:
        if disp["show_quantiles"]:
            pdf.kv("P25 / P50 / P75", f"Rs.{bm['p25']:.2f} / {bm['p50']:.2f} / {bm['p75']:.2f} LPA")
        pdf.kv("Range used for checks", f"Rs.{disp['lo']:.2f} - {disp['hi']:.2f} LPA ({disp['mode']})")
        if disp["show_target"]:
            pos = bm.get("positioning")
            if pos:
                pdf.kv("Recommended target", f"{_lpa(bm['target'])} (P{bm['target_percentile']:g}: policy "
                       f"P{pos['policy_percentile']:g}, {pos['percentile_shift']:+g} for {pos['band']} health)", bold=True)
            else:
                pdf.kv("Recommended target", f"{_lpa(bm['target'])} (policy P{bm['target_percentile']:g})", bold=True)
            if bm.get("generic_p50"):  # benchmarks made before health stopped moving the market range
                pdf.kv("Generic median (stage default health)", _lpa(bm["generic_p50"]))
        if ev.get("compa_peer"):
            pdf.kv("Compa-ratio", f"{ev['compa_peer']} vs peer median" +
                   (f", {ev['compa_band']} vs band midpoint" if ev.get("compa_band") else ""))
    if disp.get("notice"):
        pdf.para(disp["notice"])
    c = bm["cohort"]
    if c.get("suppressed"):
        pdf.kv("Peer cohort", f"Suppressed - {c.get('reason')}")
    else:
        pdf.kv("Peer cohort", f"{c['definition']} - n={c['n']} across {c['companies']} companies, data vintage {c['vintage']}")
    pdf.kv("Confidence", f"{bm['confidence']['level']} ({bm['confidence']['score']}/100)")
    pdf.kv("Health score", f"{bm['health']['score']:.0f}/100 ({bm['health']['band']}), {round(bm['health']['coverage'] * 100)}% from real metrics")
    for d in bm["drivers"]:
        pdf.para(f"- {d}")

    pdf.section("Checks")
    for chk in ev["checks"]:
        if chk["status"] == "pass":
            continue
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*pdf.TEXT)
        pdf.multi_cell(0, 5, pdf_safe(f"[{chk['status'].upper()}] {chk['category']}: {chk['title']}"),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.para(chk["detail"])
    passed = sum(1 for chk in ev["checks"] if chk["status"] == "pass")
    pdf.para(f"{passed} further checks passed.", italic=True)

    pdf.section("Approval route & decisions")
    for step in route:
        done = state["done"].get(step["step"])
        status = f"approved by {done['user_name']} on {done['ts'][:16].replace('T', ' ')}" if done else "pending"
        pdf.kv(f"Step {step['step']}: {step['role_label']}", f"{status} - triggered by: {', '.join(step['reasons'])}")
    for a in actions:
        if a["comment"]:
            pdf.para(f"{a['ts'][:16].replace('T', ' ')} {a['user_name']} ({a['action']}): {a['comment']}")

    pdf.section("Reproducibility")
    pdf.kv("Snapshot hash", submission["snapshot_hash"][:32])
    pdf.kv("Model version", bm["model_version"])
    if bm["cohort"].get("cohort_hash"):
        pdf.kv("Peer cohort hash", bm["cohort"]["cohort_hash"][:32])
    pdf.kv("Health score version", bm["health_score_version"])
    pdf.kv("Ruleset", f"{ev['ruleset']['id']} v{ev['ruleset']['version']} (effective {ev['ruleset']['effective_from']})")
    pdf.ln(2)
    pdf.para(DISCLAIMER, italic=True)
    return _bytes(pdf)


def candidate_summary_context(*, company, request_row, package: dict, structure: dict, settings: dict) -> dict:
    """Only package-level facts. No benchmark argument exists on purpose."""
    fmv = float(settings.get("fmv_per_share_inr", 0) or 0)
    return {
        "company_name": company["name"],
        "role": request_row["role"],
        "level": request_row["level"],
        "city": request_row["city"],
        "start_date": request_row["start_date"],
        "candidate_name": request_row["candidate_name"],
        "package": package,
        "rows": structure["rows"],
        "esop_value_lakh": round(package["esop_options"] * fmv / 1e5, 2) if fmv else None,
        "fmv_per_share": fmv,
        # Package-level only: grant, strike, the company's FMV and its stated scenario assumptions.
        "esop_scenarios": esop_scenarios(package["esop_options"], settings, package.get("strike_price_inr")),
    }


def candidate_summary_pdf(ctx: dict) -> bytes:
    pkg = ctx["package"]
    demo = ctx.get("demo", True)
    pdf = _BriefPDF(f"Offer summary - {ctx['company_name']}",
                    f"{ctx['role']} ({ctx['level']}), {ctx['city']}",
                    "Indicative summary for discussion. The signed offer letter governs." +
                    (" DEMO - generated from sample data, not a real offer." if demo else ""),
                    watermark="SAMPLE - NOT A REAL OFFER" if demo else None)
    pdf.add_page()
    pdf.section("Your offer")
    if ctx["candidate_name"]:
        pdf.kv("Prepared for", ctx["candidate_name"])
    pdf.kv("Role", f"{ctx['role']} ({ctx['level']})")
    pdf.kv("Location", ctx["city"])
    if ctx["start_date"]:
        pdf.kv("Proposed start date", ctx["start_date"])
    pdf.kv("Annual cash CTC", _lpa(pkg["cash_ctc"]), bold=True)
    pdf.section("Annual cash CTC breakup")
    for row in ctx["rows"]:
        pdf.kv(row["component"], f"Rs.{row['inr']:,}")
    pdf.section("One-time components")
    for key, label in (("joining_bonus", "Joining bonus"), ("relocation", "Relocation support"),
                       ("notice_buyout", "Notice-period buyout"), ("other_one_time", "Other")):
        if pkg[key]:
            pdf.kv(label, f"Rs.{pkg[key]:,.2f} lakh")
    if not any(pkg[k] for k in ("joining_bonus", "relocation", "notice_buyout", "other_one_time")):
        pdf.para("None.")
    if pkg["esop_options"]:
        pdf.section("Employee stock options")
        pdf.kv("Options granted", f"{pkg['esop_options']:,}")
        pdf.kv("Vesting", f"{pkg['vesting_years']:g} years with a {pkg['cliff_months']}-month cliff")
        if ctx["esop_value_lakh"]:
            pdf.kv("Value at current FMV", f"Rs.{ctx['esop_value_lakh']:,.2f} lakh (Rs.{ctx['fmv_per_share']:,.0f}/share)")
        es = ctx.get("esop_scenarios")
        if es:
            pdf.kv("Strike price", f"Rs.{es['strike_price_inr']:,.2f} per option")
            pdf.kv("Cost to exercise all options", f"Rs.{es['exercise_cost_lakh']:,.2f} lakh")
            for s in es["scenarios"]:
                pdf.kv(f"{s['name']} scenario ({s['exit_multiple']:g}x today's FMV)",
                       f"Rs.{s['gross_lakh']:,.2f} lakh before tax" + (" - no gain" if s["underwater"] else ""))
            pdf.para(f"Each scenario assumes {es['future_dilution_pct']:g}% dilution from future funding rounds before a "
                     f"sale or IPO. Figures are before tax. {es['label']}", italic=True)
        pdf.para("Option value depends on the company's future valuation and a liquidity event; it is not guaranteed.")
    return _bytes(pdf)
