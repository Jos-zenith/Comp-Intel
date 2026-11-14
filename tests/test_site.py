"""
Public website, brand and first-run (Website & Brand PRD v1.0): pages render,
tone rules hold, the sample benchmark leads with the trade-off and is labelled as
demo data, calculators work, and the pilot form validates and is CSRF-protected.
"""
import pytest

import main
import marketing
from helpers import as_role, post, q, token
from workflow.routes import indian_grouping
from workflow.take_home import income_tax

PUBLIC = ["/", "/product", "/india-ctc", "/esop-scenarios", "/benchmarks", "/for-founders", "/for-people",
          "/pricing", "/security", "/pilot", "/privacy", "/terms", "/sample-benchmark"]
BANNED = ["ai-powered", "salary predictor", "revolutioni", "dream salary", "perfect compensation",
          "hire faster with ai", "instant salary insights"]
SAMPLE = "/sample-benchmark?role=Software+Engineer&level=Senior&city=Bengaluru&stage=Series+A"


@pytest.mark.parametrize("url", PUBLIC)
def test_public_pages_render_on_brand_and_on_tone(client, url):
    r = client.get(url)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "css/brand.css" in html and "compintel-mark.svg" in html
    low = html.lower()
    for phrase in BANNED:
        assert phrase not in low, f"{url} uses banned phrase {phrase!r}"


def test_home_leads_with_the_offer_decision_and_labels_demo_data(client):
    html = client.get("/").get_data(as_text=True)
    assert "Make the right offer before it costs you the hire" in html
    assert "Generate a sample offer benchmark" in html
    assert "Demo data — not for live hiring decisions" in html
    assert "Runway impact" in html and "Recommended package" in html


def test_sample_benchmark_shows_tradeoff_and_records_an_event(client):
    html = client.get(SAMPLE).get_data(as_text=True)
    assert "Demo data" in html and "not for live hiring decisions" in html
    for name in ("Conservative", "Recommended", "Aggressive"):
        assert name in html
    assert "runway falls by" in html and "annual cash cost rises by" in html
    events = q("SELECT * FROM site_events WHERE event = 'sample_benchmark'")
    assert len(events) == 1 and "Software Engineer" in events[0]["detail"]
    assert "ip" not in events[0]["detail"].lower()


def test_sample_benchmark_maths():
    with main.app.app_context():
        s = marketing.sample_benchmark("Software Engineer", "Senior", "Bengaluru", "Series A")
    assert s["p25"] <= s["p50"] <= s["p75"]
    cons, rec, agg = s["scenarios"]
    assert cons["cash"] <= rec["cash"] <= agg["cash"]
    assert cons["esop_options"] > agg["esop_options"]  # conservative carries more equity
    assert agg["runway_after"] < cons["runway_after"]
    assert s["aha"]["cost_delta_lakh"] == pytest.approx(agg["annual_cost_lakh"] - cons["annual_cost_lakh"], abs=0.11)
    assert s["aha"]["from_pct"] == 35 and s["aha"]["to_pct"] == 75 and s["aha"]["hire_runway_delta"] < 0
    assert marketing.approx_percentile(s["p50"], s["p25"], s["p50"], s["p75"]) == 50
    assert marketing.approx_percentile(s["p75"], s["p25"], s["p50"], s["p75"]) == 75


def test_sample_uses_visitor_numbers_without_storing_them(client):
    base = client.get(SAMPLE).get_data(as_text=True)
    mine = client.get(SAMPLE + "&cash_balance_cr=5&monthly_burn_lakh=100&hiring_budget_cr=1&planned_hires=5")
    html = mine.get_data(as_text=True)
    assert "Runway now uses your numbers" in html and html != base
    assert q("SELECT detail FROM site_events ORDER BY id DESC LIMIT 1")[0]["detail"].count("cash_balance") == 0


def test_sample_benchmark_rejects_unknown_values(client):
    html = client.get("/sample-benchmark?role=Astronaut&level=Senior&city=Bengaluru&stage=Seed").get_data(as_text=True)
    assert "Pick a role, level, city and funding stage" in html
    assert not q("SELECT * FROM site_events")


def test_ctc_calculator_sums_and_estimates_take_home(client):
    html = client.get("/india-ctc?cash_ctc=24&variable=2.4&city=Bengaluru").get_data(as_text=True)
    assert "&#8377;24,00,000" in html or "₹24,00,000" in html  # Indian digit grouping
    assert "Take-home / month" in html and "Decision support only" in html
    assert "IN-TAX-NEW FY2025-26" in html
    bad = client.get("/india-ctc?cash_ctc=10&variable=9&city=Bengaluru").get_data(as_text=True)
    assert "Variable pay doesn" in bad


def test_new_regime_tax():
    assert income_tax(1_200_000) == 0                    # s.87A rebate
    assert income_tax(1_300_000) == 78_000               # 75,000 slab tax (below 1,00,000 marginal cap) + 4% cess
    assert income_tax(1_210_000) == round(10_000 * 1.04)  # marginal relief just above the rebate limit
    assert income_tax(2_500_000) == round((20_000 + 40_000 + 60_000 + 80_000 + 100_000 + 30_000) * 1.04)


def test_esop_calculator_accepts_percentage_or_options(client):
    html = client.get("/esop-scenarios?mode=pct&grant_pct=0.08&fds=10000000&fmv=250&strike=10&dilution=30"
                      "&conservative=0.5&base=2&upside=5&tax=30").get_data(as_text=True)
    assert "8,000" in html and "Upside" in html and "not option pricing" in html
    assert q("SELECT * FROM site_events WHERE event = 'esop_calculator'")
    underwater = client.get("/esop-scenarios?mode=options&options=1000&fds=10000000&fmv=100&strike=150"
                            "&dilution=30&conservative=0.5&base=2&upside=5&tax=30").get_data(as_text=True)
    assert "underwater today" in underwater


def _application(**over):
    data = {"name": "Asha Rao", "work_email": "asha@acme-saas.in", "company": "Acme SaaS", "persona": "Founder / CEO",
            "employees": "30-99", "hires": "5-10", "cities": ["Bengaluru", "Pune"], "consent": "on"}
    data.update(over)
    return data


def test_pilot_application_validates_and_is_stored(client):
    assert client.post("/pilot", data=_application()).status_code == 400  # no CSRF token
    r = post(client, "/pilot", _application(work_email="asha@gmail.com"))
    assert r.status_code == 400 and "work email" in r.get_data(as_text=True)
    assert post(client, "/pilot", _application(consent="")).status_code == 400
    assert not q("SELECT * FROM pilot_applications")
    r = post(client, "/pilot?from=sample", _application())
    assert r.status_code == 302 and r.headers["Location"].endswith("/pilot/thanks")
    row = q("SELECT * FROM pilot_applications")[0]
    assert row["company"] == "Acme SaaS" and row["source"] == "sample" and "Pune" in row["cities"]
    assert q("SELECT * FROM site_events WHERE event = 'pilot_application'")


def test_pilot_applications_are_admin_only(client):
    post(client, "/pilot", _application())
    as_role(client, "recruiter")
    assert client.get("/app/pilot-applications").status_code == 403
    as_role(client, "admin")
    html = client.get("/app/pilot-applications").get_data(as_text=True)
    assert "asha@acme-saas.in" in html and "Pilot applications" in html


def test_404_is_branded(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "This page doesn't exist" in r.get_data(as_text=True)


def test_indian_digit_grouping():
    assert indian_grouping(999) == "999"
    assert indian_grouping(100000) == "1,00,000"
    assert indian_grouping(1234567) == "12,34,567"
    assert indian_grouping(-25000000) == "-2,50,00,000"


def test_site_works_without_the_demo_workflow(client):
    main.app.config["COMPINTEL_DEMO_MODE"] = False
    try:
        html = client.get(SAMPLE).get_data(as_text=True)
        assert "Create free workspace" not in html and "Apply for the India SaaS pilot" in html
        assert client.get("/").status_code == 200
        assert post(client, "/pilot", {**_application(), "csrf_token": token(client)}).status_code == 302
    finally:
        main.app.config["COMPINTEL_DEMO_MODE"] = True
