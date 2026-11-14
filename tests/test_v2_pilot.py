"""
PRD v2 (real-data pilot): observed-offer benchmarks, partner data import with
consent, privacy thresholds, affordability / runway impact, ESOP scenarios and
offer-outcome telemetry.
"""
import io
from datetime import date, timedelta

import pytest

from helpers import approve_all, as_role, benchmark, demo, new_offer, package_form, post, q, run_sql, submit
from workflow import affordability, esop, pilot
from workflow import db as wdb

HEADER = ",".join(pilot.PARTNER_CSV_COLUMNS)


def _row(ref, cash, *, role="Software Engineer", level="Senior", city="Bengaluru", stage="Series A",
         outcome="accepted", days_ago=40, esop=9000, counter=None, reason=""):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    final = "" if outcome != "accepted" else cash
    return (f"{ref},{stage},80,Rs.25-100 Cr,110-120%,{city},{role},{level},Full-time,{d},"
            f"{cash},10,{esop},{counter if counter else ''},,{final},{esop if outcome == 'accepted' else ''},"
            f"{outcome},{reason},")


def partner_csv(values, refs=("P-01", "P-02", "P-03", "P-04"), extra=()):
    rows = [_row(refs[i % len(refs)], v, days_ago=30 + i) for i, v in enumerate(values)]
    return (HEADER + "\n" + "\n".join(list(rows) + list(extra)) + "\n").encode()


def import_csv(c, data: bytes, attest=True, basis="design_partner_dsa", name="partners.csv"):
    form = {"file": (io.BytesIO(data), name), "consent_basis": basis}
    if attest:
        form["consent_attested"] = "on"
    return post(c, "/app/network/import", form, content_type="multipart/form-data")


def to_pilot(c):
    as_role(c, "admin")
    assert post(c, "/app/settings/mode", {"mode": "pilot", "confirm_no_data": "on"}).status_code == 302


VALUES = [38.0, 40.5, 42.0, 43.0, 44.0, 45.5, 46.0, 47.0, 48.5, 50.0, 52.0, 55.0]


# ---------------------------------------------------------------- pilot benchmark from real data

def test_pilot_benchmark_uses_observed_offers_and_reproduces(client):
    demo(client)
    to_pilot(client)
    assert import_csv(client, partner_csv(VALUES)).status_code == 302
    assert len(q("SELECT * FROM benchmark_observations WHERE source = 'partner_import'")) == len(VALUES)
    batch = q("SELECT * FROM network_import_batches")[0]
    assert batch["contributors"] == 4 and batch["consent_basis"].startswith("Design-partner")
    # Company refs are hashed, never stored.
    assert "P-01" not in str(q("SELECT * FROM benchmark_observations"))

    as_role(client, "recruiter")
    rid = new_offer(client)  # requisition 1: Software Engineer, Senior, Bengaluru
    bm = benchmark(rid)
    cohort = bm["cohort"]
    assert bm["data_mode"] == "pilot" and cohort["engine"] == pilot.ENGINE_VERSION
    # The demo seed shared 3 accepted offers for the same role from the demo company itself.
    seeded = [41.0, 44.5, 39.8]
    expected = sorted(VALUES + seeded)
    assert cohort["n"] == len(expected) and cohort["contributors"] == 5
    assert bm["p50"] == round(pilot.quantile(expected, 0.5), 2)
    assert bm["p25"] <= bm["p50"] <= bm["p75"]
    assert bm["confidence"]["level"] in ("High", "Medium")
    assert "Observed accepted offers" in bm["confidence"]["reasons"][-1]
    assert bm["esop"]["source"] == "observed" and bm["esop"]["options_mid"] > 0

    page = client.get(f"/app/offers/{rid}").get_data(as_text=True)
    assert "Demo data - not for live hiring decisions" not in page
    assert "Same cohort, not accepted" in page

    submit(client, rid, package_form(bm))
    approve_all(client, rid)
    page = client.get(f"/app/offers/{rid}/submissions/1?verify=1").get_data(as_text=True)
    assert "identical" in page and "rows hash matches" in page

    # Partner withdraws: rows leave future benchmarks, the snapshot stays, verification says why.
    as_role(client, "admin")
    assert post(client, f"/app/network/batches/{batch['id']}/remove").status_code == 302
    assert not q("SELECT * FROM benchmark_observations WHERE source = 'partner_import'")
    assert q("SELECT removed_at FROM network_import_batches")[0]["removed_at"]
    as_role(client, "recruiter")
    page = client.get(f"/app/offers/{rid}/submissions/1?verify=1").get_data(as_text=True)
    assert "have since been removed" in page
    assert benchmark(new_offer(client))["confidence"]["level"] == "Insufficient"


def test_pilot_cohort_needs_enough_contributing_companies(client):
    demo(client)
    to_pilot(client)
    # 12 offers from one partner + 3 from the demo company = 2 contributors, under the minimum of 3.
    import_csv(client, partner_csv(VALUES, refs=("ONLY-ONE",)))
    as_role(client, "recruiter")
    bm = benchmark(new_offer(client))
    assert bm["p50"] is None and bm["confidence"]["level"] == "Insufficient"
    assert "2 contributor(s)" in bm["cohort"]["reason"]


def test_declined_offers_inform_but_do_not_set_the_range(client):
    demo(client)
    to_pilot(client)
    declines = [_row("P-09", 90, outcome="declined", reason="Accepted competing offer") for _ in range(1)]
    import_csv(client, partner_csv(VALUES, extra=declines))
    as_role(client, "recruiter")
    bm = benchmark(new_offer(client))
    assert bm["p75"] < 90 and bm["cohort"]["declined_n"] >= 1


def test_partner_import_requires_consent_admin_and_valid_rows(client):
    demo(client)
    to_pilot(client)
    import_csv(client, partner_csv(VALUES), attest=False)
    assert not q("SELECT * FROM network_import_batches")
    bad = partner_csv(VALUES, extra=[_row("someone@acme.in", 40)])
    import_csv(client, bad)
    assert not q("SELECT * FROM network_import_batches")  # one bad row rejects the whole file
    future = partner_csv(VALUES, extra=[_row("P-01", 40, days_ago=-5)])
    import_csv(client, future)
    assert not q("SELECT * FROM network_import_batches")
    data = partner_csv(VALUES)
    import_csv(client, data)
    import_csv(client, data)  # identical file
    assert len(q("SELECT * FROM network_import_batches")) == 1
    as_role(client, "recruiter")
    assert import_csv(client, partner_csv([30.0])).status_code == 403


# ---------------------------------------------------------------- affordability / runway

def test_runway_maths():
    ctx = affordability.context_from_inputs({"cash_balance_cr": 10, "monthly_burn_lakh": 50,
                                             "hiring_budget_cr": 2, "planned_hires": 5})
    assert ctx["runway_months"] == 20 and ctx["per_hire_budget_lakh"] == 40
    imp = affordability.offer_impact(ctx, 60, 0)
    assert imp["monthly_burn_increase_lakh"] == 5
    assert imp["runway_after"] == pytest.approx(1000 / 55, abs=0.01)
    assert imp["runway_delta_months"] == pytest.approx(1000 / 55 - 20, abs=0.01)
    assert imp["plan_variance_pct"] == 50
    profitable = affordability.context_from_inputs({"cash_balance_cr": 10, "monthly_burn_lakh": 0})
    assert profitable["default_alive"] and affordability.offer_impact(profitable, 60)["runway_delta_months"] is None


def test_position_costs_show_p50_to_p75_runway_impact(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    assert bm["affordability"]["runway_months"] == pytest.approx(8500 / 450, abs=0.1)
    submit(client, rid, package_form(bm))
    ev = wdb.loads(q("SELECT snapshot FROM submissions WHERE request_id = ?", (rid,))[0]["snapshot"])["evaluation"]
    rows = {r["label"]: r for r in ev["position_costs"]}
    assert rows["P75"]["annual_cost_lakh"] > rows["P50"]["annual_cost_lakh"]
    assert rows["P75"]["runway_vs_p50"] < 0
    assert any(c["id"] == "runway" and c["status"] == "pass" for c in ev["checks"])
    assert ev["impact"]["runway_after"] < ev["impact"]["runway_before"]


def test_runway_floor_breach_routes_to_finance(client):
    demo(client)
    settings = wdb.loads(q("SELECT settings FROM companies")[0]["settings"]) or {}
    run_sql("UPDATE companies SET settings = ?", (wdb.dumps({**settings, "runway_floor_months": 60}),))
    rid = new_offer(client)
    submit(client, rid, package_form(benchmark(rid)))
    sub = q("SELECT * FROM submissions WHERE request_id = ?", (rid,))[0]
    checks = wdb.loads(sub["snapshot"])["evaluation"]["checks"]
    assert any(c["id"] == "runway" and c["status"] == "exception" for c in checks)
    assert "finance" in [s["role"] for s in wdb.loads(sub["route"])]


def test_missing_cash_and_burn_lower_confidence_and_warn(client):
    demo(client)
    as_role(client, "finance")
    assert post(client, "/app/company/metrics", {"headcount": 120, "arr_band": "Rs.25-100 Cr"}).status_code == 302
    as_role(client, "recruiter")
    rid = new_offer(client)
    bm = benchmark(rid)
    assert any("runway impact can't be calculated" in r for r in bm["confidence"]["reasons"])
    submit(client, rid, package_form(bm))
    checks = wdb.loads(q("SELECT snapshot FROM submissions WHERE request_id = ?", (rid,))[0]["snapshot"])["evaluation"]["checks"]
    assert any(c["id"] == "runway" and c["status"] == "warn" for c in checks)


# ---------------------------------------------------------------- ESOP scenarios

def test_esop_scenarios_and_underwater():
    out = esop.scenarios(1000, {"fmv_per_share_inr": 250, "fully_diluted_shares": 10_000_000}, strike=200)
    by = {s["name"]: s for s in out["scenarios"]}
    assert by["Conservative"]["underwater"]  # 250 x 0.5 x 0.7 = 87.5 < 200
    assert by["Base"]["gross_lakh"] == 1.5 and by["Base"]["net_lakh"] == 1.05  # (350 - 200) x 1000
    assert out["exercise_cost_lakh"] == 2.0 and not out["underwater_now"]
    assert out["ownership_pct"] == 0.01 and out["ownership_after_dilution_pct"] == 0.007
    assert esop.scenarios(0, {"fmv_per_share_inr": 250}) is None


def test_strike_above_fmv_warns_and_candidate_sees_scenarios(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    submit(client, rid, package_form(bm, strike_price_inr=400))  # FMV is 250
    checks = wdb.loads(q("SELECT snapshot FROM submissions WHERE request_id = ?", (rid,))[0]["snapshot"])["evaluation"]["checks"]
    assert any(c["id"] == "esop_underwater" for c in checks)
    approve_all(client, rid)
    page = client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    if "CTC structure warnings" in page:
        post(client, f"/app/offers/{rid}/candidate-summary", {"confirm": "on"})
        page = client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    assert "What the options could be worth" in page and "Upside" in page and "no gain" in page
    assert client.get(f"/app/offers/{rid}/candidate-summary?format=pdf").data[:4] == b"%PDF"


# ---------------------------------------------------------------- outcome telemetry

def test_outcome_records_initial_offer_and_counter(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    form = package_form(bm)
    submit(client, rid, form)
    approve_all(client, rid)
    cash = round(form["fixed_pay"] + form["variable_pay"], 2)
    r = post(client, f"/app/offers/{rid}/outcome", {"status": "accepted", "final_cash_ctc": cash + 2,
                                                    "counter_cash_ctc": cash + 4, "decision_date": date.today().isoformat(),
                                                    "initial_cash_ctc": 1})  # ignored for in-app offers
    assert r.status_code == 302
    o = q("SELECT * FROM outcomes WHERE request_id = ?", (rid,))[0]
    assert o["initial_cash_ctc"] == pytest.approx(cash) and o["counter_cash_ctc"] == pytest.approx(cash + 4)
    obs = q("SELECT * FROM benchmark_observations WHERE outcome_id = ?", (o["id"],))[0]
    assert obs["counter_cash_ctc"] == pytest.approx(cash + 4) and obs["employee_band"] == "100-199"
    page = client.get("/app/insights").get_data(as_text=True)
    assert "Negotiation telemetry" in page and "(1 of 10)" in page and "Pilot readiness" in page


def test_outcome_csv_accepts_telemetry_columns(client):
    demo(client)
    csv_text = ("role,level,city,employment_type,status,sent_date,decision_date,final_cash_ctc,final_esop_options,"
                "competing_offer,decline_reason,notes,initial_cash_ctc,initial_esop_options,counter_cash_ctc,"
                "counter_esop_options\n"
                "Software Engineer,Mid,Pune,Full-time,declined,2026-09-01,2026-09-04,,,30,Timing / start date,,22,0,27,\n")
    assert post(client, "/app/outcomes/import", {"csv_text": csv_text}).status_code == 302
    o = q("SELECT * FROM outcomes WHERE counter_cash_ctc IS NOT NULL")[0]
    assert o["initial_cash_ctc"] == 22 and o["counter_cash_ctc"] == 27 and o["decline_reason"] == "Timing / start date"
