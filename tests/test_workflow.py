"""
End-to-end tests for the offer workflow, mapped to the PRD acceptance criteria.
Run: python -m pytest tests -q
"""
import io
import random
import sqlite3

import pytest

import main
from helpers import (as_role, benchmark, candidate_doc, demo, new_offer, package_form, post, q, submit, users)
from workflow import db as wdb
from workflow.checks import structure_ctc


# ---------------------------------------------------------------- setup & roles

def test_workspace_creation_is_admin_only(client):
    r = post(client, "/app/company/profile", {"name": "X", "funding_stage": "Seed", "target_market": "Domestic",
                                              "pricing_model": "Subscription", "employee_count": 20})
    assert r.status_code == 403
    assert q("SELECT action FROM audit_log WHERE action = 'permission_denied'")


def test_csrf_required(client):
    as_role(client, "admin")
    r = client.post("/app/company/demo", data={})
    assert r.status_code == 400


def test_provisional_benchmark_from_profile_only(client):
    as_role(client, "admin")
    r = post(client, "/app/company/profile", {"name": "Tiny Co", "funding_stage": "Seed", "target_market": "Domestic",
                                              "pricing_model": "Subscription", "employee_count": 30,
                                              "arr_band": "Under Rs.5 Cr", "hiring_locations": ["Pune"]})
    assert r.status_code == 302
    as_role(client, "recruiter")
    rid_resp = post(client, "/app/offers/new", {"role": "Software Engineer", "level": "Mid", "city": "Pune",
                                                "years_experience": 3, "skill_category": "Core",
                                                "education_pedigree": "Recognised university"})
    assert rid_resp.status_code == 302
    rid = int(rid_resp.headers["Location"].split("/")[-1])
    bm = benchmark(rid)
    assert bm["p25"] <= bm["p50"] <= bm["p75"]  # BM-08
    assert bm["health"]["coverage"] == pytest.approx(0.2)  # only ARR/employee is real
    assert any("No company metrics" in r for r in bm["confidence"]["reasons"])
    page = client.get(f"/app/offers/{rid}").get_data(as_text=True)
    assert "Peer cohort" in page and "Data vintage" in page


# ---------------------------------------------------------------- metrics

def test_metrics_snapshots_are_versioned_and_immutable(client):
    demo(client)
    as_role(client, "finance")
    before = q("SELECT COUNT(*) n FROM metric_snapshots")[0]["n"]
    base = {"arr_band": "Rs.25-100 Cr", "headcount": 120}
    post(client, "/app/company/metrics", base)
    h1 = q("SELECT health_score FROM metric_snapshots ORDER BY id DESC LIMIT 1")[0]["health_score"]
    post(client, "/app/company/metrics", {**base, "ndr_pct": 135, "gross_margin_pct": 80, "cac_lakh": 2, "ltv_lakh": 14})
    h2 = q("SELECT health_score FROM metric_snapshots ORDER BY id DESC LIMIT 1")[0]["health_score"]
    assert q("SELECT COUNT(*) n FROM metric_snapshots")[0]["n"] == before + 2
    assert h2 != h1
    with main.app.app_context():
        with pytest.raises(sqlite3.DatabaseError):
            wdb.execute("UPDATE metric_snapshots SET health_score = 1")
        with pytest.raises(sqlite3.DatabaseError):
            wdb.execute("DELETE FROM audit_log")


def test_metrics_csv_upload(client):
    demo(client)
    as_role(client, "admin")
    csv_text = "field,value\narr_cr,40\nheadcount,100\nndr_pct,110\n"
    r = post(client, "/app/company/metrics-upload", {"file": (io.BytesIO(csv_text.encode()), "m.csv")},
             content_type="multipart/form-data")
    assert r.status_code == 302
    assert q("SELECT source FROM metric_snapshots ORDER BY id DESC LIMIT 1")[0]["source"] == "csv"
    bad = "field,value\nnot_a_field,1\n"
    post(client, "/app/company/metrics-upload", {"file": (io.BytesIO(bad.encode()), "m.csv")},
         content_type="multipart/form-data")
    assert q("SELECT source FROM metric_snapshots ORDER BY id DESC LIMIT 1")[0]["source"] == "csv"


# ---------------------------------------------------------------- full flow

def test_full_offer_flow(client):
    demo(client)
    rid = new_offer(client)  # Senior SWE Bengaluru requisition
    bm = benchmark(rid)
    assert bm["drivers"] and bm["cohort"]["n"] >= 10

    # live checks render without saving
    r = post(client, f"/app/offers/{rid}/evaluate", package_form(bm))
    assert r.status_code == 200 and "CTC structure" in r.get_data(as_text=True)

    assert submit(client, rid, package_form(bm)).status_code == 302
    sub = q("SELECT * FROM submissions WHERE request_id = ?", (rid,))[0]
    route = [s["role"] for s in wdb.loads(sub["route"])]
    assert route[0] == "hiring_manager" and "people_ops" in route  # Senior -> People Ops
    assert q("SELECT status FROM offer_requests WHERE id = ?", (rid,))[0]["status"] == "pending_approval"

    # locked while in approval
    assert post(client, f"/app/offers/{rid}/package", {**package_form(bm), "action": "save"}).status_code == 400

    # wrong role can't approve
    as_role(client, "finance")
    assert post(client, f"/app/offers/{rid}/decision", {"action": "approve"}).status_code == 403

    for role in route:
        as_role(client, role)
        assert post(client, f"/app/offers/{rid}/decision", {"action": "approve", "comment": "ok"}).status_code == 302
    assert q("SELECT status FROM offer_requests WHERE id = ?", (rid,))[0]["status"] == "approved"

    as_role(client, "recruiter")
    pdf = client.get(f"/app/offers/{rid}/brief.pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"

    summary = client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    doc = candidate_doc(summary)
    for leak in ("P25", "P50", "P75", "cohort", "band", "compa-ratio", "model", "health", "peer"):
        assert leak.lower() not in doc.lower(), leak  # OF-02
    assert client.get(f"/app/offers/{rid}/candidate-summary?format=pdf").data[:4] == b"%PDF"

    r = post(client, f"/app/offers/{rid}/outcome", {"status": "accepted", "final_cash_ctc": bm["target"],
                                                     "decision_date": "2026-10-20"})
    assert r.status_code == 302
    assert q("SELECT status FROM offer_requests WHERE id = ?", (rid,))[0]["status"] == "accepted"
    oid = q("SELECT id FROM outcomes WHERE request_id = ?", (rid,))[0]["id"]
    obs = q("SELECT * FROM benchmark_observations WHERE outcome_id = ?", (oid,))
    assert obs and "Test Candidate" not in str(obs) and obs[0]["city_tier"] == "Tier 1 SaaS Hub"

    page = client.get("/app/insights").get_data(as_text=True)
    assert "Acceptance and calibration" in page
    dash = client.get("/app/").get_data(as_text=True)
    assert "North-star" in dash


def test_policy_breaches_route_to_exception_approvers(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    high = round(bm["p75"] * 1.25, 2)
    form = package_form(bm, cash=high, override_reason="critical_skill", override_note="scarce skill")
    assert submit(client, rid, form).status_code == 302
    sub = q("SELECT * FROM submissions WHERE request_id = ?", (rid,))[0]
    snap = wdb.loads(sub["snapshot"])
    assert "market_high" in snap["evaluation"]["exceptions"]
    assert "finance" in [s["role"] for s in wdb.loads(sub["route"])]


def test_override_without_reason_blocks_submit(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    form = package_form(bm, cash=round(bm["target"] * 1.2, 2))
    submit(client, rid, form)
    assert not q("SELECT * FROM submissions WHERE request_id = ?", (rid,))
    assert q("SELECT status FROM offer_requests WHERE id = ?", (rid,))[0]["status"] == "draft"


def test_request_changes_keeps_history(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    submit(client, rid, package_form(bm))
    as_role(client, "hiring_manager")
    assert post(client, f"/app/offers/{rid}/decision", {"action": "request_changes"}).status_code == 302  # needs comment
    assert q("SELECT status FROM offer_requests WHERE id = ?", (rid,))[0]["status"] == "pending_approval"
    post(client, f"/app/offers/{rid}/decision", {"action": "request_changes", "comment": "Lower the ESOP"})
    assert q("SELECT status FROM offer_requests WHERE id = ?", (rid,))[0]["status"] == "changes_requested"
    as_role(client, "recruiter")
    submit(client, rid, package_form(bm, esop_options=100))
    subs = q("SELECT revision FROM submissions WHERE request_id = ? ORDER BY revision", (rid,))
    assert [s["revision"] for s in subs] == [1, 2]
    assert q("SELECT COUNT(*) n FROM approval_actions")[0]["n"] == 1


def test_snapshot_reproduces(client):
    demo(client)
    rid = new_offer(client)
    submit(client, rid, package_form(benchmark(rid)))
    page = client.get(f"/app/offers/{rid}/submissions/1?verify=1").get_data(as_text=True)
    assert "Hash matches" in page and "identical" in page


def test_policy_change_does_not_alter_submitted_offer(client):
    demo(client)
    rid = new_offer(client)
    submit(client, rid, package_form(benchmark(rid)))
    before = q("SELECT route FROM submissions WHERE request_id = ?", (rid,))[0]["route"]
    as_role(client, "admin")
    post(client, "/app/settings/rules", {"action": "add", "name": "CFO sees all", "condition_kind": "always",
                                         "condition_value": "", "approver_role": "finance"})
    post(client, "/app/settings/policy", {"target_percentile": 70})
    assert q("SELECT route FROM submissions WHERE request_id = ?", (rid,))[0]["route"] == before


# ---------------------------------------------------------------- permissions & privacy

def test_exec_sees_only_approved_offers(client):
    demo(client)
    rid = new_offer(client)
    as_role(client, "exec")
    assert client.get(f"/app/offers/{rid}").status_code == 403
    assert "CAND-" not in client.get("/app/offers").get_data(as_text=True)


def test_benchmark_export_and_audit_are_role_gated(client):
    demo(client)
    as_role(client, "exec")
    assert client.get("/app/offers.csv").status_code == 403
    assert client.get("/app/audit.csv").status_code == 403
    as_role(client, "admin")
    r = client.get("/app/audit.csv")
    assert r.status_code == 200 and b"load_demo_workspace" in r.data


def test_snapshots_never_contain_candidate_name(client):
    demo(client)
    rid = new_offer(client, candidate_name="Priya Secretname")
    submit(client, rid, package_form(benchmark(rid)))
    assert "Secretname" not in q("SELECT snapshot FROM submissions")[0]["snapshot"]


def test_external_outcome_import(client):
    demo(client)
    before = q("SELECT COUNT(*) n FROM outcomes")[0]["n"]
    csv_text = ("role,level,city,status,sent_date,decision_date,final_cash_ctc,final_esop_options,competing_offer,decline_reason,notes\n"
                "Software Engineer,Mid,Pune,accepted,2026-09-01,2026-09-05,21,0,,,\n"
                "QA Engineer,Junior,Kochi,declined,,2026-09-07,,,9,Compensation (cash),\n")
    r = post(client, "/app/outcomes/import", {"file": (io.BytesIO(csv_text.encode()), "o.csv")},
             content_type="multipart/form-data")
    assert r.status_code == 302
    assert q("SELECT COUNT(*) n FROM outcomes")[0]["n"] == before + 2


# ---------------------------------------------------------------- CTC structure

def test_ctc_breakup_always_sums_exactly():
    rng = random.Random(7)
    for _ in range(500):
        cash = round(rng.uniform(3, 150), 2)
        variable = round(cash * rng.uniform(0, 0.4), 2)
        s = structure_ctc(cash, variable, rng.choice(["Bengaluru", "Kochi"]))
        if s["fits"]:
            assert s["sum_inr"] == s["total_inr"]
            assert all(r["inr"] >= 0 for r in s["rows"])


def test_oversized_variable_blocks():
    s = structure_ctc(20, 12, "Bengaluru")
    assert not s["fits"]


def test_existing_pages_still_work(client):
    for path in ("/", "/predict_form", "/compare", "/saas-metrics", "/compliance-check", "/about-tech"):
        assert client.get(path).status_code == 200, path
    page = client.get("/predict_form").get_data(as_text=True)
    assert "Demo environment" in page
