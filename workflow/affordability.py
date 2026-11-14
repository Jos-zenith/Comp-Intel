"""
Company affordability and runway impact (PRD v2 section 10.1, AF-01 to AF-07).

Market competitiveness says what a candidate costs; this module says what that
costs the company. It works from four optional company inputs, entered with the
other metrics and versioned in the same immutable snapshots:

  cash_balance_cr     cash in bank (Rs. crore)
  monthly_burn_lakh   net monthly burn (Rs. lakh; 0 = break-even or profitable)
  hiring_budget_cr    annual cash-compensation budget for new hires (Rs. crore)
  planned_hires       hires planned in the next 12 months

Units follow the rest of the app: cash CTC in LPA is Rs. lakh per year, so an
offer's recurring annual cost in lakh is its cash CTC. Cash CTC already includes
employer EPF and gratuity accrual in this app's CTC structure, so it is the
employer's cash cost; nothing is added on top.

Stated assumption: the offer is treated as incremental to today's burn. If the
burn figure already budgets for this hire, the runway impact is overstated.
"""
from __future__ import annotations

AFFORDABILITY_VERSION = "AF-1.0"

# (field, label, unit, min, max) - same shape as company.METRIC_FIELDS.
AFFORDABILITY_FIELDS = [
    ("cash_balance_cr", "Cash in bank", "Rs. crore", 0, 1_000_000),
    ("monthly_burn_lakh", "Net monthly burn (0 if profitable)", "Rs. lakh", 0, 10_000_000),
    ("hiring_budget_cr", "Annual new-hire cash budget", "Rs. crore", 0, 100_000),
    ("planned_hires", "Planned hires, next 12 months", "count", 0, 100_000),
]
RUNWAY_FIELDS = ("cash_balance_cr", "monthly_burn_lakh")
PLAN_FIELDS = ("hiring_budget_cr", "planned_hires")
ASSUMPTION = ("Treats the offer as incremental to today's burn; if your burn already budgets for this hire, "
              "the runway impact is overstated. One-time cash is assumed paid from cash in month one.")


def context_from_inputs(inputs: dict | None) -> dict:
    """The company's affordability position from a metrics snapshot's raw inputs."""
    inputs = inputs or {}
    cash_cr = inputs.get("cash_balance_cr")
    burn = inputs.get("monthly_burn_lakh")
    budget_cr = inputs.get("hiring_budget_cr")
    hires = inputs.get("planned_hires")
    cash_lakh = cash_cr * 100 if cash_cr is not None else None
    runway = None
    default_alive = False
    if cash_lakh is not None and burn is not None:
        if burn > 0:
            runway = cash_lakh / burn
        else:
            default_alive = True
    per_hire = (budget_cr * 100 / hires) if (budget_cr and hires) else None
    labels = {f[0]: f[1] for f in AFFORDABILITY_FIELDS}
    return {
        "version": AFFORDABILITY_VERSION,
        "cash_lakh": round(cash_lakh, 2) if cash_lakh is not None else None,
        "monthly_burn_lakh": burn,
        "runway_months": round(runway, 1) if runway is not None else None,
        "default_alive": default_alive,
        "has_runway": runway is not None or default_alive,
        "hiring_budget_lakh": round(budget_cr * 100, 2) if budget_cr is not None else None,
        "planned_hires": int(hires) if hires is not None else None,
        "per_hire_budget_lakh": round(per_hire, 2) if per_hire is not None else None,
        "missing_runway": [labels[f] for f in RUNWAY_FIELDS if inputs.get(f) is None],
        "missing_plan": [labels[f] for f in PLAN_FIELDS if inputs.get(f) is None],
        "assumption": ASSUMPTION,
    }


def offer_impact(ctx: dict | None, recurring_lakh: float, one_time_lakh: float = 0.0) -> dict:
    """Annual and monthly cost of one offer, and what it does to runway and the hiring plan."""
    out = {
        "annual_cost_lakh": round(recurring_lakh, 2),
        "year_one_cost_lakh": round(recurring_lakh + one_time_lakh, 2),
        "monthly_burn_increase_lakh": round(recurring_lakh / 12, 2),
        "burn_increase_pct": None, "runway_before": None, "runway_after": None, "runway_delta_months": None,
        "plan_variance_pct": None, "per_hire_budget_lakh": None, "available": False,
    }
    if not ctx:
        return out
    if ctx.get("per_hire_budget_lakh"):
        out["per_hire_budget_lakh"] = ctx["per_hire_budget_lakh"]
        out["plan_variance_pct"] = round((recurring_lakh / ctx["per_hire_budget_lakh"] - 1) * 100, 1)
    if not ctx.get("has_runway"):
        return out
    burn = ctx["monthly_burn_lakh"] or 0.0
    monthly = recurring_lakh / 12
    new_burn = burn + monthly
    cash_after = ctx["cash_lakh"] - one_time_lakh
    before = ctx["runway_months"]  # None when default-alive
    after = cash_after / new_burn if new_burn > 0 else None
    out.update(
        available=True,
        burn_increase_pct=round(monthly / burn * 100, 2) if burn else None,
        runway_before=before,
        runway_after=round(after, 2) if after is not None else None,
        runway_delta_months=round(after - before, 2) if (after is not None and before is not None) else None,
    )
    return out


def position_costs(ctx: dict | None, bm: dict, one_time_lakh: float = 0.0) -> list[dict]:
    """Cost of paying at each market position, so a recruiter or Finance can see what
    moving from P50 to P75 does to cash and runway before approval (AF acceptance)."""
    if not bm.get("display", {}).get("show_quantiles") or bm.get("p50") is None:
        return []
    points = [("P25", bm["p25"]), ("P50", bm["p50"]), ("P75", bm["p75"])]
    if bm.get("display", {}).get("show_target") and bm.get("target") is not None:
        points.append((f"Target (P{int(bm.get('target_percentile', 50))})", bm["target"]))
    base = offer_impact(ctx, bm["p50"], one_time_lakh)
    rows = []
    for label, cash in points:
        imp = offer_impact(ctx, cash, one_time_lakh)
        rows.append({
            "label": label, "cash_ctc": round(cash, 2), "annual_cost_lakh": imp["annual_cost_lakh"],
            "vs_p50_lakh": round(cash - bm["p50"], 2),
            "monthly_burn_increase_lakh": imp["monthly_burn_increase_lakh"],
            "runway_after": imp["runway_after"],
            "runway_vs_p50": (round(imp["runway_after"] - base["runway_after"], 2)
                              if imp["runway_after"] is not None and base["runway_after"] is not None else None),
            "plan_variance_pct": imp["plan_variance_pct"],
        })
    return rows
