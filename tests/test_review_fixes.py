"""Regression tests for the product review: range stability, cohort reconciliation,
demo runway, pilot-readiness logic, the no-data mode guard, and the offer-page layout."""
import json

import main
import marketing
from helpers import as_role, benchmark, demo, new_offer, package_form, post, q, run_sql
from workflow import db as wdb
from workflow.seed import CURRENT_METRICS, upgrade_demo_workspace


# ---------------------------------------------------------------- range stability

def test_served_range_uses_calibrated_ratios_not_lopsided_quantiles():
    calib = json.loads((main.ARTIFACTS_DIR / "interval_calibration.json").read_text())
    assert 45 <= calib["heldout_p25_p75_coverage_pct"] <= 55
    s = marketing.hero_sample()  # was 27.9 / 33.1 / 34.4: a 1.2 lakh upper gap vs 5.2 below
    lo_gap, hi_gap = s["p50"] - s["p25"], s["p75"] - s["p50"]
    assert 0.6 <= lo_gap / hi_gap <= 1.4
    r = calib["by_level"]["Senior"]
    assert abs(s["p25"] / s["p50"] - r["p25_ratio"]) < 0.01 and abs(s["p75"] / s["p50"] - r["p75_ratio"]) < 0.01


def test_model_version_includes_the_calibration():
    assert main._artifact_version().startswith("hgb-quantile-")
    import hashlib
    digest = hashlib.sha256()
    for name in ("preprocessor.pkl", "model_quantile_p25.pkl", "model_quantile_p50.pkl", "model_quantile_p75.pkl"):
        digest.update((main.ARTIFACTS_DIR / name).read_bytes())
    assert main._artifact_version() != "hgb-quantile-" + digest.hexdigest()[:12]


# ---------------------------------------------------------------- cohort reconciliation

def test_widened_cohort_is_reconciled_on_the_offer(client):
    demo(client)
    rid = new_offer(client)  # SE / Senior / Bengaluru: exact cohort too small, widened to Tier 1
    bm = benchmark(rid)
    rec = bm["cohort"]["reconciliation"]
    assert bm["cohort"]["widened"] and rec["n"] == bm["cohort"]["n"]
    # The raw cohort and the model disagree, but the gap is explained by composition...
    assert bm["p50"] - rec["raw_p50"] > 3
    assert abs(rec["composition_lpa"]) > abs(rec["model_error_lpa"])
    assert any("outside Bengaluru" in m for m in rec["mix"]) and any("domestic" in m for m in rec["mix"])
    # ...and the cohort adjusted to this candidate lands near the model's median.
    assert abs(rec["adjusted_p50"] / bm["p50"] - 1) < 0.08
    html = client.get(f"/app/offers/{rid}").get_data(as_text=True)
    assert "Why the market range differs from the raw cohort" in html and "Cohort adjusted to this candidate" in html


def test_model_cohort_disagreement_lowers_confidence():
    from workflow.benchmark import confidence
    cohort = {"rung": 0, "n": 50, "definition": "x", "vintage": None,
              "reconciliation": {"model_vs_cohort_pct": 18.0}}
    profile = {"coverage": 1.0, "missing_impact": []}
    conf = confidence(cohort, profile, 10, {})
    assert conf["score"] == 90 and any("18% above what this peer cohort was actually paid" in r for r in conf["reasons"])
    cohort["reconciliation"]["model_vs_cohort_pct"] = 3.0
    assert confidence(cohort, profile, 10, {})["score"] == 100


# ---------------------------------------------------------------- demo runway

def test_fresh_demo_shows_runway(client):
    demo(client)
    rid = new_offer(client)
    html = post(client, f"/app/offers/{rid}/evaluate", package_form(benchmark(rid))).get_data(as_text=True)
    assert "Runway impact unknown" not in html and "runway 18.9 -&gt;" in html


def test_old_demo_workspace_is_upgraded_without_rewriting_history(client):
    demo(client)
    company_id = q("SELECT id FROM companies")[0]["id"]
    # What a workspace seeded before runway inputs existed looks like.
    old_inputs = {k: v for k, v in CURRENT_METRICS.items()
                  if k not in ("cash_balance_cr", "monthly_burn_lakh", "hiring_budget_cr", "planned_hires")}
    latest = q("SELECT * FROM metric_snapshots ORDER BY id DESC LIMIT 1")[0]
    run_sql("INSERT INTO metric_snapshots (company_id, created_at, created_by, source, inputs, derived, health_score, "
            "health_band, health_score_version, completeness) VALUES (?, ?, NULL, 'demo seed', ?, ?, ?, ?, ?, ?)",
            (company_id, latest["created_at"], wdb.dumps(old_inputs), latest["derived"], latest["health_score"],
             latest["health_band"], latest["health_score_version"], latest["completeness"]))
    run_sql("UPDATE benchmark_observations SET decline_reason = NULL")
    before = len(q("SELECT id FROM metric_snapshots"))
    with main.app.app_context():
        changed = upgrade_demo_workspace()
        assert upgrade_demo_workspace() == []  # idempotent
    assert len(changed) == 2 and len(q("SELECT id FROM metric_snapshots")) == before + 1
    newest = json.loads(q("SELECT inputs FROM metric_snapshots ORDER BY id DESC LIMIT 1")[0]["inputs"])
    assert newest["cash_balance_cr"] == 85 and newest["monthly_burn_lakh"] == 450
    assert q("SELECT * FROM audit_log WHERE action = 'upgrade_demo_workspace'")
    declined = q("SELECT decline_reason FROM benchmark_observations WHERE status = 'declined'")
    assert declined and all(r["decline_reason"] for r in declined)


# ---------------------------------------------------------------- pilot readiness

def test_readiness_does_not_mark_zero_or_unmeasured_as_met(client):
    demo(client)
    html = client.get("/app/insights").get_data(as_text=True)
    start = html.index("Pilot readiness")
    panel = html[start:html.index("</table>", start)]
    rows = panel.split("<tr>")[1:]
    coverage = next(r for r in rows if "P25-P75 coverage" in r)
    reasons = next(r for r in rows if "Decline reasons captured" in r)
    assert "not yet" in coverage and "no accepted offers with a range yet" in coverage
    # The seed's two declines both have a reason, in the workspace AND the shared network.
    assert "100% of 2 declines" in reasons and "met" in reasons
    assert "(this workspace)" in html


def test_coverage_counts_only_offers_that_had_a_range(client):
    demo(client)
    with main.app.app_context():
        from workflow.routes import _kpis
        k = _kpis(q("SELECT id FROM companies")[0]["id"])
    assert k["in_range_pct"] is None and k["in_range_n"] == 0  # 7 accepted outside offers, none had a range


# ---------------------------------------------------------------- no-data mode guard

def test_switching_to_pilot_without_usable_data_needs_confirmation(client):
    demo(client)
    as_role(client, "admin")
    page = client.get("/app/settings").get_data(as_text=True)
    assert "No real-data cohort is ready" in page
    post(client, "/app/settings/mode", {"mode": "pilot"})
    assert q("SELECT mode FROM companies")[0]["mode"] == "demo"
    assert "Pilot mode isn't usable yet" in client.get("/app/network").get_data(as_text=True)
    post(client, "/app/settings/mode", {"mode": "pilot", "confirm_no_data": "on"})
    assert q("SELECT mode FROM companies")[0]["mode"] == "pilot"


# ---------------------------------------------------------------- offer page layout

def test_evaluation_is_tabbed_and_cash_is_separate_from_equity(client):
    demo(client)
    rid = new_offer(client)
    partial = post(client, f"/app/offers/{rid}/evaluate", package_form(benchmark(rid))).get_data(as_text=True)
    for tab in ("checks", "cost", "esop", "ctc"):
        assert f'data-tab="{tab}"' in partial
    page = client.get(f"/app/offers/{rid}").get_data(as_text=True)
    assert "Equity - not cash, not in the total above" in page
    assert "an authored stage default, not a sourced market figure" in page
