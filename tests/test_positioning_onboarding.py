"""
Health as a positioning signal (not a market-price input), and recruiter-first onboarding.
"""
import pytest

import main
import saas_metrics as sm
from helpers import as_role, benchmark, demo, new_offer, package_form, post, q
from workflow.checks import parse_package, run_checks

PROFILE = dict(role="Software Engineer", years_experience=6, city="Bengaluru", funding_stage="Series B",
               target_market="Domestic", skill_category="Core", education_pedigree="Recognised university",
               pricing_model="Subscription")


# ---------------------------------------------------------------- positioning

def test_health_never_moves_the_market_range():
    strong = main.generate_prediction(dict(PROFILE, health_score_override=90), apply_signals=False)
    weak = main.generate_prediction(dict(PROFILE, health_score_override=20), apply_signals=False)
    for q_ in ("p25", "p50", "p75"):
        assert strong[q_] == pytest.approx(weak[q_])
    assert "Health Score" not in [c["feature"] for c in strong["contributions"]]
    # ...but it does move where the company aims, and the ESOP tilt.
    assert strong["positioning"]["target_percentile"] == 60
    assert weak["positioning"]["target_percentile"] == 35
    assert strong["target"] > weak["target"]
    assert weak["positioning"]["esop_tilt"] > strong["positioning"]["esop_tilt"]


def test_positioning_can_be_switched_off_and_stays_inside_the_range():
    off = sm.pay_positioning(20, 50, enabled=False)
    assert off["target_percentile"] == 50 and off["esop_tilt"] == 1.0
    assert sm.pay_positioning(95, 70)["target_percentile"] == 75  # clamped
    assert sm.pay_positioning(10, 30)["target_percentile"] == 25


def test_workflow_benchmark_records_positioning(client):
    demo(client)
    bm = benchmark(new_offer(client))
    pos = bm["positioning"]
    assert pos["policy_percentile"] == 50
    assert bm["target_percentile"] == pos["target_percentile"]
    assert bm["p25"] <= bm["target"] <= bm["p75"]
    assert "generic_p50" not in bm
    assert any("market range is the same" in d for d in bm["drivers"])


def test_health_positioning_setting_turns_shift_off(client):
    demo(client)
    as_role(client, "admin")
    post(client, "/app/settings/policy", {"target_percentile": 50, "health_positioning_field": "1"})  # unchecked
    as_role(client, "recruiter")
    bm = benchmark(new_offer(client))
    assert bm["positioning"]["enabled"] is False and bm["target_percentile"] == 50


def test_affordability_warns_cash_tight_company_paying_above_median(client):
    demo(client)
    bm = benchmark(new_offer(client))
    bm["positioning"] = sm.pay_positioning(30, 50)  # At-risk
    pkg = parse_package(package_form(bm, cash=round(bm["p50"] * 1.05, 2)), {})
    ev = run_checks(pkg, bm, offer={"role": "Software Engineer", "level": "Senior", "city": "Bengaluru",
                                    "years_experience": 6, "employment_type": "Full-time"},
                    requisition=None, settings={}, metrics_age_days=1, accepted_peer=None)
    assert any(c["id"] == "affordability" and c["status"] == "warn" for c in ev["checks"])


# ---------------------------------------------------------------- onboarding

def test_recruiter_checklist_is_recruiter_first(client):
    demo(client)
    page = client.get("/app/").get_data(as_text=True)
    assert "Import your past offers" in page and "Ask Finance" in page
    assert page.index("Import your past offers") < page.index("Get your first benchmark")
    assert q("SELECT * FROM user_onboarding")  # first visit recorded


def test_metrics_request_round_trip(client):
    demo(client)
    assert post(client, "/app/company/request-metrics", {"note": "before Friday"}).status_code == 302
    post(client, "/app/company/request-metrics")  # second click doesn't duplicate
    assert len(q("SELECT * FROM metrics_requests WHERE fulfilled_at IS NULL")) == 1
    assert q("SELECT * FROM notifications WHERE role = 'finance' AND message LIKE '%asked for company metrics%'")

    as_role(client, "finance")
    assert "before Friday" in client.get("/app/company").get_data(as_text=True)
    assert post(client, "/app/company/request-metrics").status_code == 403  # finance enters metrics instead
    post(client, "/app/company/metrics", {"arr_band": "Rs.25-100 Cr", "headcount": 120})
    assert not q("SELECT * FROM metrics_requests WHERE fulfilled_at IS NULL")
    assert q("SELECT * FROM notifications WHERE role = 'recruiter' AND message LIKE 'Company metrics are in%'")


def test_time_to_first_benchmark_is_measured_and_guide_can_be_hidden(client):
    demo(client)
    client.get("/app/")
    new_offer(client)
    row = q("SELECT * FROM user_onboarding WHERE first_benchmark_at IS NOT NULL")
    assert row
    page = client.get("/app/").get_data(as_text=True)
    assert "Your first benchmark took" in page
    assert "Welcome," in page
    post(client, "/app/onboarding/dismiss")
    assert "Welcome," not in client.get("/app/").get_data(as_text=True)


def test_pasted_past_offers_import_without_header(client):
    demo(client)
    before = q("SELECT COUNT(*) n FROM outcomes")[0]["n"]
    rows = ("Software Engineer,Mid,Pune,Full-time,accepted,2026-09-01,2026-09-05,21,0,,,\n"
            "QA Engineer,Junior,Kochi,Full-time,declined,,2026-09-07,,,9,Compensation (cash),\n")
    assert post(client, "/app/outcomes/import", {"csv_text": rows}).status_code == 302
    assert q("SELECT COUNT(*) n FROM outcomes")[0]["n"] == before + 2


def test_legacy_tools_live_under_methodology(client):
    home = client.get("/").get_data(as_text=True)
    # Website PRD 14.1: the homepage's one primary CTA is the sample benchmark, not a predictor.
    assert "Predict compensation" not in home and "Generate a sample offer benchmark" in home
    about = client.get("/about-tech").get_data(as_text=True)
    assert "Methodology lab" in about and "Model explorer" in about
