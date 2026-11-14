"""
Offer package: parsing, cost maths, CTC structuring, and the six check groups
(market, internal equity, budget, ESOP, CTC structure, data quality).

Every check returns a status:
  pass       - fine
  info       - worth knowing, no action needed
  warn       - risk the recruiter should look at, doesn't block or reroute
  exception  - policy breach: allowed, but adds an approval step (see approvals.py)
  block      - can't be submitted until fixed
and a plain-language detail line, so no warning is ever unexplained (CR acceptance).
"""
from __future__ import annotations

import ctc_engine

from . import affordability as afford
from . import esop as esop_mod
from .take_home import estimate_take_home

# Versioned so a rule change never silently alters a historical offer: each
# submission stores the ruleset it was checked against (CR-04). The rules
# themselves live in rulesets/in_ctc/<version>.json and run in ctc_engine.
_ACTIVE_RULESET = ctc_engine.active()
RULESET = _ACTIVE_RULESET.meta()

DISCLAIMER = ctc_engine.disclaimer(_ACTIVE_RULESET)

OVERRIDE_REASONS = {
    "competing_offer": "Competing offer",
    "critical_skill": "Critical / scarce skill",
    "internal_parity": "Internal parity",
    "candidate_expectation": "Candidate expectation",
    "budget_constraint": "Budget constraint",
    "level_adjustment": "Level adjusted after interviews",
    "other": "Other (see note)",
}

STATUS_ORDER = {"block": 0, "exception": 1, "warn": 2, "info": 3, "pass": 4}

PACKAGE_MONEY_FIELDS = [
    ("fixed_pay", "Fixed pay (LPA)", True),
    ("variable_pay", "Variable pay (LPA)", False),
    ("joining_bonus", "Joining bonus (Rs. lakh)", False),
    ("relocation", "Relocation (Rs. lakh)", False),
    ("notice_buyout", "Notice-period buyout (Rs. lakh)", False),
    ("other_one_time", "Other one-time (Rs. lakh)", False),
]


class PackageError(ValueError):
    pass


def parse_package(form, settings: dict) -> dict:
    pkg = {}
    for field, label, required in PACKAGE_MONEY_FIELDS:
        raw = form.get(field)
        if raw in (None, ""):
            if required:
                raise PackageError(f"{label} is required.")
            pkg[field] = 0.0
            continue
        try:
            value = float(raw)
        except ValueError:
            raise PackageError(f"{label} must be a number.")
        if value < 0 or value > 10000:
            raise PackageError(f"{label} must be between 0 and 10,000.")
        pkg[field] = round(value, 2)
    if pkg["fixed_pay"] <= 0:
        raise PackageError("Fixed pay must be greater than zero.")
    pkg["cash_ctc"] = round(pkg["fixed_pay"] + pkg["variable_pay"], 2)

    try:
        pkg["esop_options"] = int(float(form.get("esop_options") or 0))
        pkg["vesting_years"] = float(form.get("vesting_years") or settings.get("default_vesting_years", 4))
        pkg["cliff_months"] = int(float(form.get("cliff_months") or settings.get("default_cliff_months", 12)))
    except ValueError:
        raise PackageError("ESOP options, vesting years and cliff must be numbers.")
    if pkg["esop_options"] < 0:
        raise PackageError("ESOP options can't be negative.")
    if not (1 <= pkg["vesting_years"] <= 10):
        raise PackageError("Vesting must be between 1 and 10 years.")
    if not (0 <= pkg["cliff_months"] <= 24):
        raise PackageError("Cliff must be between 0 and 24 months.")
    strike = form.get("strike_price_inr")
    if strike in (None, ""):
        pkg["strike_price_inr"] = float(settings.get("strike_price_inr", esop_mod.DEFAULTS["strike_price_inr"]))
    else:
        try:
            pkg["strike_price_inr"] = round(float(strike), 2)
        except ValueError:
            raise PackageError("Strike price must be a number.")
        if not (0 <= pkg["strike_price_inr"] <= 10**7):
            raise PackageError("Strike price must be between 0 and 1,00,00,000.")

    reason = form.get("override_reason") or ""
    if reason and reason not in OVERRIDE_REASONS:
        raise PackageError("Unrecognised override reason.")
    pkg["override_reason"] = reason
    pkg["override_note"] = (form.get("override_note") or "").strip()[:500]
    pkg["low_confidence_ack"] = form.get("low_confidence_ack") in ("on", "1", "true", True)
    pkg["other_one_time_note"] = (form.get("other_one_time_note") or "").strip()[:200]
    pkg["notes"] = (form.get("notes") or "").strip()[:1000]
    return pkg


def compute_costs(pkg: dict, settings: dict) -> dict:
    one_time = pkg["joining_bonus"] + pkg["relocation"] + pkg["notice_buyout"] + pkg["other_one_time"]
    fmv = float(settings.get("fmv_per_share_inr", 0) or 0)
    fds = float(settings.get("fully_diluted_shares", 0) or 0)
    grant_value_lakh = pkg["esop_options"] * fmv / 1e5
    return {
        "recurring_cash": round(pkg["cash_ctc"], 2),
        "one_time_cash": round(one_time, 2),
        "year_one_cash": round(pkg["cash_ctc"] + one_time, 2),
        "esop_grant_value_lakh": round(grant_value_lakh, 2),
        "esop_annual_value_lpa": round(grant_value_lakh / pkg["vesting_years"], 2) if pkg["vesting_years"] else 0,
        "dilution_pct": round(pkg["esop_options"] / fds * 100, 4) if fds else None,
    }


def structure_ctc(cash_ctc_lpa: float, variable_lpa: float, city: str) -> dict:
    """CTC breakup under the active ruleset (see ctc_engine.structure)."""
    return ctc_engine.structure(cash_ctc_lpa, variable_lpa, city, _ACTIVE_RULESET)


def _check(cid, category, status, title, detail, exception=None, rule=False):
    out = {"id": cid, "category": category, "status": status, "title": title, "detail": detail,
           "exception": exception}
    if rule:
        out["ruleset"] = f"{RULESET['id']} v{RULESET['version']}"
        out["effective_from"] = RULESET["effective_from"]
    return out


def _market_checks(checks: list, cash: float, market: dict | None):
    """Live clearing-price signal from accepted AND declined offers (workflow/clearing.py).
    Advisory: it warns, it never blocks or adds an approver, because it moves with
    the market while the rest of the submission is a frozen record."""
    if not market or market.get("status") != "ok" or not market.get("clearing_price_lpa"):
        return
    clearing = market["clearing_price_lpa"]
    where = f"{market['cohort']}, last {market['window_days']} days, {market['n_decided']} decided offers"
    if cash < clearing:
        checks.append(_check("market_clearing", "Market", "warn", "Below the market clearing price",
                             f"Rs.{cash:.2f} LPA is under the Rs.{clearing:.2f} LPA level where offers in this cohort "
                             f"have been accepted at least half the time ({where}). Expect a higher decline risk."))
    elif market.get("alert"):
        checks.append(_check("market_clearing", "Market", "info", "Market is moving above P50",
                             f"P50 offers in this cohort are being declined {market['decline_at_p50_pct']:.0f}% of the "
                             f"time ({where}). This offer is at or above the Rs.{clearing:.2f} LPA clearing price."))
    else:
        checks.append(_check("market_clearing", "Market", "pass", "At or above the market clearing price",
                             f"Clearing price Rs.{clearing:.2f} LPA ({where})."))


def run_checks(pkg: dict, bm: dict, *, offer: dict, requisition, settings: dict,
               metrics_age_days, accepted_peer, market: dict | None = None) -> dict:
    """accepted_peer: {"definition", "n", "median", "suppressed", "min_n"} from
    routes.accepted_peer_stats(), or None. market: the live clearing-price summary
    for this offer's cohort (clearing.for_offer), or None."""
    checks = []
    cash = pkg["cash_ctc"]
    disp = bm["display"]
    target = bm["target"] if disp["show_target"] else None

    # --- Market competitiveness: against the range actually shown for this confidence level ---
    lo, hi = disp.get("lo"), disp.get("hi")
    range_name = {"full": "P25-P75 market range", "widened": "widened market range",
                  "broad": "broad market band"}.get(disp["mode"])
    compa_peer = cash / bm["p50"] if bm.get("p50") else None
    if disp["mode"] == "none":
        checks.append(_check("market_range", "Market", "info", "No market benchmark",
                             "There isn't enough reliable data for a market range, so this package isn't "
                             "checked against one."))
    elif cash > hi:
        checks.append(_check("market_range", "Market", "exception", f"Above the {range_name}",
                             f"Cash CTC Rs.{cash:.2f} LPA is {(cash / hi - 1) * 100:.1f}% above the top of the "
                             f"{range_name} (Rs.{hi:.2f}). Needs exception approval.", "market_high"))
    elif cash < lo:
        checks.append(_check("market_range", "Market", "warn", f"Below the {range_name}",
                             f"Cash CTC Rs.{cash:.2f} LPA is {(1 - cash / lo) * 100:.1f}% below the bottom of the "
                             f"{range_name} (Rs.{lo:.2f}) - higher risk the candidate declines."))
    else:
        tail = f"; compa-ratio vs peer median {compa_peer:.2f}." if compa_peer and disp["show_quantiles"] else "."
        checks.append(_check("market_range", "Market", "pass", f"Inside the {range_name}",
                             f"Rs.{lo:.2f}-{hi:.2f} LPA{tail}"))
    if offer.get("expected_ctc") and cash < offer["expected_ctc"]:
        checks.append(_check("expectation", "Market", "warn", "Below candidate expectation",
                             f"Rs.{cash:.2f} LPA vs the Rs.{offer['expected_ctc']:.2f} LPA the candidate expects "
                             f"({(cash / offer['expected_ctc'] - 1) * 100:+.1f}%)."))
    if offer.get("competing_offer") and cash < offer["competing_offer"]:
        checks.append(_check("competing", "Market", "warn", "Below competing offer",
                             f"Competing offer is Rs.{offer['competing_offer']:.2f} LPA "
                             f"({(offer['competing_offer'] / cash - 1) * 100:.1f}% higher)."))

    # --- Recommendation: override reason / low-confidence acknowledgment / manual entry ---
    reason_text = ""
    if pkg.get("override_reason"):
        reason_text = OVERRIDE_REASONS[pkg["override_reason"]]
        if pkg.get("override_note"):
            reason_text += " - " + pkg["override_note"]
    if disp["manual_reason_required"]:
        if not pkg.get("override_reason"):
            checks.append(_check("override", "Recommendation", "block", "Manual entry reason required",
                                 "There's no recommendation to anchor this package. Choose a reason so approvers "
                                 "know how the numbers were set."))
        else:
            checks.append(_check("override", "Recommendation", "info", "Manually entered package",
                                 f"No recommendation available. Reason: {reason_text}.", "override"))
    elif disp["ack_required"]:
        if not pkg.get("low_confidence_ack"):
            checks.append(_check("override", "Recommendation", "block", "Acknowledge the low-confidence benchmark",
                                 "Only a broad band is available. Tick the acknowledgment to confirm you've "
                                 "read the confidence reasons before submitting."))
        else:
            extra = f" Reason given: {reason_text}." if reason_text else ""
            checks.append(_check("override", "Recommendation", "info", "Low-confidence benchmark acknowledged",
                                 "Recruiter confirmed the benchmark is low confidence." + extra, "override"))
    else:
        tol = float(settings.get("override_tolerance_pct", 5))
        gap = (cash / target - 1) * 100 if target else 0
        if abs(gap) > tol:
            if not pkg.get("override_reason"):
                checks.append(_check("override", "Recommendation", "block", "Override reason required",
                                     f"Cash CTC is {gap:+.1f}% from the Rs.{target:.2f} LPA recommendation "
                                     f"(tolerance +/-{tol:g}%). Pick an override reason before submitting."))
            else:
                checks.append(_check("override", "Recommendation", "info", "Recommendation overridden",
                                     f"{gap:+.1f}% vs recommendation. Reason: {reason_text}.", "override"))
        else:
            checks.append(_check("override", "Recommendation", "pass", "Within tolerance of recommendation",
                                 f"{gap:+.1f}% vs the Rs.{target:.2f} LPA target (tolerance +/-{tol:g}%)."))

    # --- Internal band ---
    band = bm.get("band")
    if band:
        compa_band = cash / band["mid_lpa"] if band["mid_lpa"] else None
        if cash > band["max_lpa"]:
            checks.append(_check("band", "Internal band", "exception", "Above internal band",
                                 f"Rs.{cash:.2f} LPA exceeds the band maximum Rs.{band['max_lpa']:.2f} "
                                 f"(compa-ratio to midpoint {compa_band:.2f}).", "band"))
        elif cash < band["min_lpa"]:
            checks.append(_check("band", "Internal band", "warn", "Below internal band",
                                 f"Rs.{cash:.2f} LPA is under the band minimum Rs.{band['min_lpa']:.2f}."))
        else:
            checks.append(_check("band", "Internal band", "pass", "Inside internal band",
                                 f"Band Rs.{band['min_lpa']:.2f}-{band['max_lpa']:.2f} LPA; "
                                 f"compa-ratio to midpoint {compa_band:.2f}."))
    else:
        checks.append(_check("band", "Internal band", "warn", "No internal band defined",
                             f"No band for {offer['role']} / {offer['level']} in this city tier. "
                             f"Add one under Job architecture."))

    # --- Internal parity: narrowly defined, suppressed when the comparison group is small ---
    eq_tol = float(settings.get("internal_equity_tolerance_pct", 15))
    not_equity = "This is a like-for-like offer comparison, not a pay-equity analysis."
    if accepted_peer:
        if accepted_peer["suppressed"]:
            checks.append(_check("equity", "Internal parity", "info", "Not enough comparable accepted offers",
                                 f"{accepted_peer['n']} accepted offer(s) for {accepted_peer['definition']}; at least "
                                 f"{accepted_peer['min_n']} are needed before comparing. {not_equity}"))
        else:
            med = accepted_peer["median"]
            diff = (cash / med - 1) * 100
            base = (f"{diff:+.1f}% vs the Rs.{med:.2f} LPA median of {accepted_peer['n']} accepted offers for "
                    f"{accepted_peer['definition']} (tolerance {eq_tol:g}%). {not_equity}")
            if diff > eq_tol:
                checks.append(_check("equity", "Internal parity", "exception", "Above comparable accepted offers",
                                     base, "equity"))
            elif diff < -eq_tol:
                checks.append(_check("equity", "Internal parity", "warn", "Below comparable accepted offers", base))
            else:
                checks.append(_check("equity", "Internal parity", "pass", "In line with comparable accepted offers",
                                     base))

    # --- Budget ---
    costs = compute_costs(pkg, settings)
    budget = requisition["budget_max_lpa"] if requisition is not None else None
    if budget:
        if cash > budget:
            checks.append(_check("budget", "Budget", "exception", "Over requisition budget",
                                 f"Cash CTC Rs.{cash:.2f} LPA exceeds the Rs.{budget:.2f} LPA budget by "
                                 f"Rs.{cash - budget:.2f}. Year-one cash cost Rs.{costs['year_one_cash']:.2f} lakh.",
                                 "budget"))
        else:
            checks.append(_check("budget", "Budget", "pass", "Within requisition budget",
                                 f"Rs.{budget - cash:.2f} LPA headroom. Year-one cash cost "
                                 f"Rs.{costs['year_one_cash']:.2f} lakh (incl. Rs.{costs['one_time_cash']:.2f} one-time)."))
    else:
        checks.append(_check("budget", "Budget", "warn", "No requisition budget set",
                             "Budget can't be checked - set a budget on the requisition."))
    # Affordability: company health decides how hard the company can push on cash,
    # not what the market pays. Advisory only - it never blocks or reroutes.
    pos = bm.get("positioning")
    if pos and pos.get("enabled") and bm.get("p50"):
        if pos["affordability"] in ("tight", "strained") and cash > bm["p50"]:
            checks.append(_check("affordability", "Budget", "warn", "Above market median while cash is tight",
                                 f"Health score {pos['health_score']:.0f}/100 ({pos['band']}). Cash CTC "
                                 f"Rs.{cash:.2f} LPA is above the Rs.{bm['p50']:.2f} LPA market median. Consider "
                                 f"moving part of it into ESOP or one-time pay."))
        elif pos["affordability"] == "comfortable" and pos["percentile_shift"] > 0 and bm.get("p25") \
                and cash < bm["p25"]:
            checks.append(_check("affordability", "Budget", "info", "Below market while the company can afford more",
                                 f"Health score {pos['health_score']:.0f}/100 ({pos['band']}) supports paying above "
                                 f"the median; this offer is below P25 (Rs.{bm['p25']:.2f} LPA)."))
    one_time_cap = float(settings.get("one_time_cap_pct", 20))
    if cash and costs["one_time_cash"] > cash * one_time_cap / 100:
        checks.append(_check("one_time", "Budget", "exception", "High one-time components",
                             f"One-time cash Rs.{costs['one_time_cash']:.2f} lakh is over {one_time_cap:g}% "
                             f"of cash CTC.", "budget"))

    # --- Runway and hiring plan (AF-04 to AF-07) ---
    afford_ctx = bm.get("affordability")
    impact = afford.offer_impact(afford_ctx, costs["recurring_cash"], costs["one_time_cash"])
    if afford_ctx is None or not afford_ctx.get("has_runway"):
        missing = (afford_ctx or {}).get("missing_runway") or ["Cash in bank", "Net monthly burn"]
        checks.append(_check("runway", "Affordability", "warn", "Runway impact unknown",
                             f"{' and '.join(missing)} not on file, so Finance can't see what this offer does to "
                             f"runway. Add them under Company & metrics."))
    else:
        floor = float(settings.get("runway_floor_months", 12))
        max_hit = float(settings.get("max_runway_impact_months", 1.0))
        after, delta = impact["runway_after"], impact["runway_delta_months"]
        base = (f"Adds Rs.{impact['monthly_burn_increase_lakh']:.2f} lakh/month to burn"
                + (f" ({impact['burn_increase_pct']:.2f}% of current burn)" if impact["burn_increase_pct"] else "")
                + (f"; runway {impact['runway_before']:.1f} -> {after:.1f} months" if delta is not None
                   else f"; runway after this hire {after:.1f} months" if after is not None else ""))
        if after is not None and after < floor:
            checks.append(_check("runway", "Affordability", "exception", f"Runway below {floor:g}-month floor",
                                 base + f". That's under the {floor:g}-month policy floor. Needs Finance approval.",
                                 "budget"))
        elif delta is not None and -delta > max_hit:
            checks.append(_check("runway", "Affordability", "warn", "Material runway reduction",
                                 base + f", a {-delta:.2f}-month reduction (policy flags more than {max_hit:g})."))
        else:
            checks.append(_check("runway", "Affordability", "pass", "Runway impact within policy", base + "."))
    if impact["plan_variance_pct"] is not None:
        tol = float(settings.get("hiring_plan_tolerance_pct", 15))
        var = impact["plan_variance_pct"]
        detail = (f"Rs.{costs['recurring_cash']:.2f} lakh/year vs an average Rs.{impact['per_hire_budget_lakh']:.2f} "
                  f"lakh per planned hire ({var:+.1f}%, tolerance {tol:g}%).")
        checks.append(_check("hiring_plan", "Affordability", "warn" if var > tol else "pass",
                             "Above the hiring-plan average" if var > tol else "In line with the hiring plan", detail))

    # --- ESOP ---
    if pkg["esop_options"] > 0:
        pool = float(settings.get("esop_pool_available", 0) or 0)
        cap = float(settings.get("max_dilution_per_hire_pct", 0.25))
        if pool and pkg["esop_options"] > pool:
            checks.append(_check("esop_pool", "ESOP", "block", "Exceeds available ESOP pool",
                                 f"{pkg['esop_options']:,} options requested; only {int(pool):,} left in the pool."))
        if costs["dilution_pct"] is not None and costs["dilution_pct"] > cap:
            checks.append(_check("esop_dilution", "ESOP", "exception", "Dilution above per-hire cap",
                                 f"{costs['dilution_pct']:.3f}% fully diluted vs a {cap:g}% per-hire cap.", "esop"))
        elif costs["dilution_pct"] is not None:
            checks.append(_check("esop_dilution", "ESOP", "pass", "Dilution within per-hire cap",
                                 f"{costs['dilution_pct']:.3f}% fully diluted (cap {cap:g}%)."))
        rec = bm["esop"]
        if rec.get("options_high") and pkg["esop_options"] > rec["options_high"]:
            checks.append(_check("esop_range", "ESOP", "warn", "Above recommended grant range",
                                 f"Recommended {rec['options_low']:,}-{rec['options_high']:,} options."))
        strike = pkg.get("strike_price_inr")
        fmv = float(settings.get("fmv_per_share_inr", 0) or 0)
        if strike is not None and fmv and strike >= fmv:
            checks.append(_check("esop_underwater", "ESOP", "warn", "Options are underwater today",
                                 f"Strike Rs.{strike:,.2f} is at or above the current FMV of Rs.{fmv:,.2f}, so the "
                                 f"grant has no intrinsic value now. Explain this to the candidate."))
        if pkg["cliff_months"] < 12:
            checks.append(_check("esop_cliff", "ESOP", "warn", "Cliff shorter than 12 months",
                                 "Indian rules generally require at least one year between grant and vesting "
                                 "(Companies (Share Capital and Debentures) Rules 2014, r.12(6); SEBI SBEB for "
                                 "listed companies). Confirm your scheme terms.", rule=True))

    # --- CTC structure and statutory wage (decision support, versioned rules in ctc_engine) ---
    structure = structure_ctc(cash, pkg["variable_pay"], offer["city"])
    for c in ctc_engine.evaluate(structure, offer["city"], _ACTIVE_RULESET):
        checks.append(_check(c["id"], c["category"], c["status"], c["title"], c["detail"], rule=True))

    # --- Market clearing price (live outcome telemetry; advisory, never blocks) ---
    _market_checks(checks, cash, market)

    # --- Data quality ---
    conf = bm["confidence"]
    if conf["level"] in ("Low", "Insufficient"):
        checks.append(_check("dq_confidence", "Data quality", "warn", f"{conf['level']} benchmark confidence",
                             " ".join(conf["reasons"][:3])))
    if metrics_age_days is None:
        checks.append(_check("dq_metrics", "Data quality", "warn", "No company metrics on file",
                             "Health score is provisional (profile only)."))
    elif metrics_age_days > int(settings.get("metrics_stale_days", 180)):
        checks.append(_check("dq_metrics", "Data quality", "warn", "Company metrics are stale",
                             f"Last metrics update {metrics_age_days} days ago."))
    if bm["inputs"]["model_level"] != offer["level"]:
        checks.append(_check("dq_level", "Data quality", "warn", "Experience and requisition level differ",
                             f"{offer['years_experience']:g} years maps to {bm['inputs']['model_level']}; "
                             f"the requisition level is {offer['level']}."))
    if bm["cohort"].get("widened"):
        checks.append(_check("dq_cohort", "Data quality", "info", "Peer cohort was widened",
                             f"Used: {bm['cohort']['definition']} (n={bm['cohort']['n']})."))

    checks.sort(key=lambda c: STATUS_ORDER[c["status"]])
    exceptions = sorted({c["exception"] for c in checks if c["exception"] and c["status"] in ("exception", "info")})
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in STATUS_ORDER}
    ctc_warnings = [c for c in checks if c["category"] in ("CTC structure", "Statutory wage") and c["status"] == "warn"]
    return {
        "checks": checks,
        "exceptions": exceptions,
        "summary": summary,
        "blocked": summary["block"] > 0,
        "ctc_warnings": ctc_warnings,
        "costs": costs,
        "affordability": afford_ctx,
        "impact": impact,
        "position_costs": afford.position_costs(afford_ctx, bm, costs["one_time_cash"]),
        "esop_scenarios": esop_mod.scenarios(pkg["esop_options"], settings, pkg.get("strike_price_inr")),
        "take_home": estimate_take_home(structure) if structure["fits"] else None,
        "structure": structure,
        "compa_peer": round(compa_peer, 2) if compa_peer else None,
        "compa_band": round(cash / band["mid_lpa"], 2) if band and band["mid_lpa"] else None,
        "ruleset": RULESET,
        "disclaimer": DISCLAIMER,
        "market": market,
    }


