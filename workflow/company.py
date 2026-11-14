"""
Company metrics -> versioned health profile.

Works with partial data: any of the four health-score components that can't be
computed from what the company entered is filled with the funding-stage default
score, and the snapshot records exactly which components were real and which
were filled. That's what lets a recruiter get a provisional benchmark from just
funding stage + headcount + ARR band, and see confidence rise as they add more.
"""
from __future__ import annotations

import csv
import io

import saas_metrics as sm

from .affordability import AFFORDABILITY_FIELDS

HEALTH_SCORE_VERSION = "HS-1.0"
HEALTH_SCORE_FORMULA = (
    "Rule of 40 x 30% + NDR x 30% + LTV:CAC x 20% + ARR/employee x 20% "
    "(saas_metrics.saas_health_score). Components that can't be computed are "
    "filled with the funding-stage default score."
)

# Midpoints in Rs. crore, used only when the exact ARR isn't entered.
ARR_BANDS = {
    "Under Rs.5 Cr": 2.5,
    "Rs.5-25 Cr": 15.0,
    "Rs.25-100 Cr": 60.0,
    "Rs.100-500 Cr": 250.0,
    "Rs.500 Cr+": 750.0,
}

# (field, label, unit, min, max). These are the CP-02 metrics plus operating
# margin, which Rule of 40 needs, and customer count, which lets LTV be derived.
METRIC_FIELDS = [
    ("arr_cr", "ARR", "Rs. crore", 0, 100000),
    ("arr_growth_pct", "ARR growth (YoY)", "%", -100, 1000),
    ("operating_margin_pct", "Operating / EBITDA margin", "%", -500, 100),
    ("ndr_pct", "Net dollar retention", "%", 0, 300),
    ("annual_churn_pct", "Annual revenue churn", "%", 0, 100),
    ("cac_lakh", "CAC (per new customer)", "Rs. lakh", 0, 100000),
    ("ltv_lakh", "LTV (per customer)", "Rs. lakh", 0, 1000000),
    ("gross_margin_pct", "Gross margin", "%", -100, 100),
    ("customers", "Paying customers", "count", 0, 10_000_000),
    ("headcount", "Headcount", "people", 1, 1_000_000),
] + AFFORDABILITY_FIELDS  # cash, burn, hiring budget, planned hires: runway impact only, not the health score
FIELD_LABELS = {f[0]: f[1] for f in METRIC_FIELDS}
# Fields that count towards the "% of metrics complete" figure.
COMPLETENESS_FIELDS = ["arr_cr", "arr_growth_pct", "operating_margin_pct", "ndr_pct",
                       "annual_churn_pct", "cac_lakh", "ltv_lakh", "gross_margin_pct", "headcount"]

COMPONENT_LABELS = {
    "rule_of_40": "Rule of 40",
    "ndr": "NDR",
    "ltv_cac": "LTV:CAC",
    "arr_per_employee": "ARR per employee",
}
COMPONENT_NEEDS = {
    "rule_of_40": "ARR growth and operating margin",
    "ndr": "NDR (or annual churn as a floor estimate)",
    "ltv_cac": "CAC plus LTV (or gross margin, churn and customer count to derive it)",
    "arr_per_employee": "ARR (or ARR band) and headcount",
}


class MetricsError(ValueError):
    pass


def parse_metric_inputs(form) -> dict:
    """Pull metric fields from a form/dict. Blank -> None. Out-of-range -> MetricsError."""
    values = {}
    for field, label, unit, lo, hi in METRIC_FIELDS:
        raw = form.get(field)
        if raw in (None, ""):
            values[field] = None
            continue
        try:
            value = float(str(raw).replace(",", ""))
        except ValueError:
            raise MetricsError(f"{label} must be a number.")
        if not (lo <= value <= hi):
            raise MetricsError(f"{label} must be between {lo:g} and {hi:g} {unit}.")
        values[field] = value
    band = form.get("arr_band") or None
    if band is not None and band not in ARR_BANDS:
        raise MetricsError("Unrecognised ARR band.")
    values["arr_band"] = band
    return values


def compute_health_profile(inputs: dict, funding_stage: str, stage_default_health: float,
                           inr_per_usd: float) -> dict:
    """Returns derived metrics, per-component scores and the blended health score."""
    derived = {}
    arr_cr = inputs.get("arr_cr")
    arr_source = "entered"
    if arr_cr is None and inputs.get("arr_band"):
        arr_cr = ARR_BANDS[inputs["arr_band"]]
        arr_source = f"midpoint of {inputs['arr_band']} band"
    arr_inr = arr_cr * 1e7 if arr_cr is not None else None
    derived["arr_cr_used"] = arr_cr
    derived["arr_source"] = arr_source if arr_cr is not None else None

    headcount = inputs.get("headcount")
    growth = inputs.get("arr_growth_pct")
    margin = inputs.get("operating_margin_pct")
    churn = inputs.get("annual_churn_pct")
    gm = inputs.get("gross_margin_pct")

    ndr = inputs.get("ndr_pct")
    derived["ndr_source"] = "entered" if ndr is not None else None
    if ndr is None and churn is not None:
        ndr = 100 - churn
        derived["ndr_source"] = "100% minus churn (floor estimate - ignores expansion revenue)"
    derived["ndr_pct"] = ndr

    ltv = inputs.get("ltv_lakh")
    derived["ltv_source"] = "entered" if ltv is not None else None
    customers = inputs.get("customers")
    if ltv is None and arr_inr and customers and gm is not None and churn:
        arpa_lakh = arr_inr / customers / 1e5
        ltv = sm.compute_ltv(arpa_lakh, gm / 100, churn / 100)
        derived["ltv_source"] = "ARR/customers x gross margin / churn"
    derived["ltv_lakh"] = ltv

    cac = inputs.get("cac_lakh")
    ltv_cac = sm.ltv_to_cac_ratio(ltv, cac) if (ltv is not None and cac) else None
    derived["ltv_cac"] = ltv_cac

    rule40 = sm.rule_of_40(growth, margin) if (growth is not None and margin is not None) else None
    derived["rule_of_40"] = rule40

    arr_pe_usd = (arr_inr / headcount / inr_per_usd) if (arr_inr and headcount) else None
    derived["arr_per_employee_usd"] = arr_pe_usd
    derived["inr_per_usd"] = inr_per_usd

    # Component scorers are the same ones saas_health_score() uses.
    raw_components = {
        "rule_of_40": sm._rule_of_40_score(rule40) if rule40 is not None else None,
        "ndr": sm._ndr_score(ndr) if ndr is not None else None,
        "ltv_cac": sm._ltv_cac_score(ltv_cac) if ltv_cac is not None else None,
        "arr_per_employee": sm._arr_per_employee_score(arr_pe_usd) if arr_pe_usd is not None else None,
    }
    components = {}
    coverage = 0.0
    for key, weight in sm.HEALTH_SCORE_WEIGHTS.items():
        value = raw_components[key]
        if value is None:
            components[key] = {"score": round(stage_default_health, 1), "filled": True, "weight": weight}
        else:
            components[key] = {"score": round(value, 1), "filled": False, "weight": weight}
            coverage += weight
    score = sum(c["score"] * c["weight"] for c in components.values())

    present = [f for f in COMPLETENESS_FIELDS if inputs.get(f) is not None]
    if inputs.get("arr_cr") is None and inputs.get("arr_band"):
        present.append("arr_cr")  # band counts as a (coarse) ARR answer
    completeness = len(set(present)) / len(COMPLETENESS_FIELDS)

    missing_impact = [
        {
            "component": COMPONENT_LABELS[k],
            "weight_pct": round(c["weight"] * 100),
            "needs": COMPONENT_NEEDS[k],
        }
        for k, c in components.items() if c["filled"]
    ]

    return {
        "health_score": round(score, 1),
        "health_band": sm.health_band_from_score(score),
        "components": components,
        "coverage": round(coverage, 2),
        "completeness": round(completeness, 2),
        "missing_impact": missing_impact,
        "derived": derived,
        "funding_stage": funding_stage,
        "stage_default_health": stage_default_health,
        "version": HEALTH_SCORE_VERSION,
    }


def metrics_csv_template() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["field", "value", "unit", "description"])
    for field, label, unit, _lo, _hi in METRIC_FIELDS:
        writer.writerow([field, "", unit, label])
    writer.writerow(["arr_band", "", "one of: " + " | ".join(ARR_BANDS), "Use instead of arr_cr if exact ARR is unknown"])
    return buf.getvalue()


def parse_metrics_csv(text: str) -> dict:
    """Accepts the vertical template (field,value,...). Unknown fields are rejected
    so a typo can't silently drop a metric."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "field" not in reader.fieldnames or "value" not in reader.fieldnames:
        raise MetricsError("CSV must have 'field' and 'value' columns - download the template.")
    known = {f[0] for f in METRIC_FIELDS} | {"arr_band"}
    form = {}
    for row in reader:
        field = (row.get("field") or "").strip()
        if not field:
            continue
        if field not in known:
            raise MetricsError(f"Unknown field in CSV: {field!r}.")
        form[field] = (row.get("value") or "").strip()
    return parse_metric_inputs(form)
