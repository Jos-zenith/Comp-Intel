"""
Authorization, privacy, integrity and confidence-behaviour tests (review follow-ups).
"""
import io
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import main
from helpers import (approve_all, as_role, as_user, benchmark, candidate_doc, demo, new_offer, package_form, post, q,
                     run_sql, submit, users)
from workflow import db as wdb
from workflow.benchmark import display_for
from workflow.checks import run_checks, structure_ctc
from workflow.integrity import verify_all


# ---------------------------------------------------------------- demo-mode guard

def test_workflow_is_off_unless_demo_mode_is_explicit(client):
    main.app.config["COMPINTEL_DEMO_MODE"] = False
    r = client.get("/app/")
    assert r.status_code == 503 and b"COMPINTEL_DEMO_MODE" in r.data
    assert client.post("/app/switch-user", data={"user_id": 1}).status_code == 503
    page = client.get("/predict_form").get_data(as_text=True)
    assert "Acting as" not in page  # no user switcher on a non-demo server
    main.app.config["COMPINTEL_DEMO_MODE"] = True


# ---------------------------------------------------------------- authorization

def test_submitter_cannot_approve_own_offer(client):
    demo(client)
    as_role(client, "people_ops")
    po_id = users(client)["people_ops"]
    rid = new_offer(client)  # Senior -> route includes people_ops
    assert submit(client, rid, package_form(benchmark(rid))).status_code == 302
    as_role(client, "hiring_manager")
    post(client, f"/app/offers/{rid}/decision", {"action": "approve"})
    as_user(client, po_id)
    r = post(client, f"/app/offers/{rid}/decision", {"action": "approve"})
    assert r.status_code == 403 and b"someone else" in r.data
    assert q("SELECT * FROM audit_log WHERE action = 'decision_denied' AND detail LIKE '%own_submission%'")
    # a second People Ops user can approve
    as_role(client, "admin")
    post(client, "/app/settings/users", {"action": "add", "name": "Second PO", "email": "po2@demo.compintel",
                                         "role": "people_ops"})
    po2 = q("SELECT id FROM users WHERE email = 'po2@demo.compintel'")[0]["id"]
    as_user(client, po2)
    assert post(client, f"/app/offers/{rid}/decision", {"action": "approve"}).status_code == 302


def test_recruiter_cannot_approve(client):
    demo(client)
    rid = new_offer(client)
    submit(client, rid, package_form(benchmark(rid)))
    assert post(client, f"/app/offers/{rid}/decision", {"action": "approve"}).status_code == 403


@pytest.mark.parametrize("role,url,allowed", [
    ("finance", "/app/company/metrics", True),          # finance may update metrics
    ("finance", "/app/company/profile", False),         # ...but not the company profile
    ("hiring_manager", "/app/company/metrics", False),
    ("exec", "/app/company/metrics", False),
    ("recruiter", "/app/settings/policy", False),
    ("people_ops", "/app/settings/mode", False),         # only admin changes workspace mode
    ("finance", "/app/settings/purge", False),
])
def test_role_permissions(client, role, url, allowed):
    demo(client)
    as_role(client, role)
    data = {"headcount": 120, "arr_band": "Rs.25-100 Cr"} if "metrics" in url else {"mode": "pilot"}
    r = post(client, url, data)
    assert (r.status_code != 403) == allowed


# ---------------------------------------------------------------- privacy

def test_purge_removes_names_but_keeps_anonymised_outcomes(client):
    demo(client)
    rid = new_offer(client, candidate_name="Ananya Purgeme")
    submit(client, rid, package_form(benchmark(rid)))
    approve_all(client, rid)
    post(client, f"/app/offers/{rid}/outcome", {"status": "accepted", "final_cash_ctc": 40})
    old = (datetime.now(timezone.utc) - timedelta(days=4000)).isoformat()
    run_sql("UPDATE offer_requests SET updated_at = ? WHERE id = ?", (old, rid))
    obs_before = q("SELECT COUNT(*) n FROM benchmark_observations")[0]["n"]
    as_role(client, "admin")
    assert post(client, "/app/settings/purge").status_code == 302
    row = q("SELECT candidate_name, candidate_pseudo_id FROM offer_requests WHERE id = ?", (rid,))[0]
    assert row["candidate_name"] is None and row["candidate_pseudo_id"].startswith("CAND-")
    assert q("SELECT COUNT(*) n FROM benchmark_observations")[0]["n"] == obs_before
    assert q("SELECT status FROM outcomes WHERE request_id = ?", (rid,))[0]["status"] == "accepted"
    assert "Purgeme" not in str(q("SELECT * FROM benchmark_observations"))


def test_consent_off_keeps_outcomes_out_of_benchmark_network(client):
    demo(client)
    as_role(client, "admin")
    post(client, "/app/settings/policy", {"target_percentile": 50})  # checkbox absent -> sharing off
    before = q("SELECT COUNT(*) n FROM benchmark_observations")[0]["n"]
    as_role(client, "recruiter")
    post(client, "/app/outcomes/new", {"role": "QA Engineer", "level": "Mid", "city": "Pune", "status": "accepted",
                                       "final_cash_ctc": 12})
    assert q("SELECT COUNT(*) n FROM outcomes WHERE role = 'QA Engineer'")[0]["n"] == 1
    assert q("SELECT COUNT(*) n FROM benchmark_observations")[0]["n"] == before


# ---------------------------------------------------------------- integrity

def test_submissions_and_actions_are_immutable(client):
    demo(client)
    rid = new_offer(client)
    submit(client, rid, package_form(benchmark(rid)))
    as_role(client, "hiring_manager")
    post(client, f"/app/offers/{rid}/decision", {"action": "approve"})
    with main.app.app_context():
        for sql in ("UPDATE submissions SET snapshot = '{}'", "DELETE FROM submissions",
                    "UPDATE approval_actions SET action = 'reject'", "UPDATE audit_log SET detail = '{}'"):
            with pytest.raises(sqlite3.DatabaseError):
                wdb.execute(sql)


def test_hash_chain_verifies_and_detects_tampering(client):
    demo(client)
    rid = new_offer(client)
    submit(client, rid, package_form(benchmark(rid)))
    approve_all(client, rid)
    as_role(client, "admin")
    page = client.get("/app/audit?verify=1").get_data(as_text=True)
    assert "Intact" in page
    sub = q("SELECT * FROM submissions WHERE request_id = ?", (rid,))[0]
    entry = q("SELECT detail FROM audit_log WHERE action = 'submit_offer'")[0]
    assert sub["snapshot_hash"] in entry["detail"] and "cohort_hash" in entry["detail"]
    ctx = wdb.loads(sub["context"])
    assert ctx.get("session") and "ip" in ctx

    # Bypass the app *and* the triggers, as an attacker with file access could.
    path = main.app.config["COMPINTEL_DB"]
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET detail = '{\"tampered\": true}' WHERE action = 'submit_offer'")
    conn.execute("DROP TRIGGER approval_actions_no_update")
    conn.execute("UPDATE approval_actions SET comment = 'edited' WHERE id = (SELECT MIN(id) FROM approval_actions)")
    conn.commit()
    conn.row_factory = sqlite3.Row
    result = verify_all(conn)
    conn.close()
    assert not result["ok"]
    assert any("Audit entry" in p for p in result["problems"])
    assert any("Approval action" in p for p in result["problems"])


def test_migration_upgrades_old_database_and_backfills_chain(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    wdb._m001_initial(conn)  # what a pre-migration install looked like
    conn.execute("INSERT INTO audit_log (ts, action, entity, detail) VALUES ('2026-01-01T00:00:00+00:00', 'x', 'y', '{}')")
    conn.execute("INSERT INTO audit_log (ts, action, entity, detail) VALUES ('2026-01-02T00:00:00+00:00', 'z', 'y', '{}')")
    conn.commit()
    ran = wdb.migrate(conn)
    assert ran == [1, 2, 3, 4, 5, 6]  # v1 is idempotent on an existing schema
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"user_onboarding", "metrics_requests", "network_import_batches"} <= tables
    obs_cols = {r["name"] for r in conn.execute("PRAGMA table_info(benchmark_observations)")}
    assert {"batch_id", "source", "counter_cash_ctc", "record_hash"} <= obs_cols
    out_cols = {r["name"] for r in conn.execute("PRAGMA table_info(outcomes)")}
    assert {"initial_cash_ctc", "counter_cash_ctc"} <= out_cols
    assert verify_all(conn)["ok"]
    assert wdb.migrate(conn) == []
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    assert "mode" in cols
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE audit_log SET detail = '{}'")
    conn.close()


# ---------------------------------------------------------------- confidence changes behaviour

def test_medium_confidence_widens_the_range(client):
    demo(client)
    bm = benchmark(new_offer(client))
    assert bm["confidence"]["level"] == "Medium"
    assert bm["display"]["mode"] == "widened"
    assert bm["display"]["lo"] < bm["p25"] and bm["display"]["hi"] > bm["p75"]
    assert any("peer observations" in r for r in bm["confidence"]["reasons"])


def _bm_with(bm, level):
    out = dict(bm)
    out["confidence"] = dict(bm["confidence"], level=level)
    out["display"] = display_for(level, bm["p25"], bm["p75"])
    return out


def test_low_confidence_needs_acknowledgment_and_hides_target(client):
    demo(client)
    rid = new_offer(client)
    bm = _bm_with(benchmark(rid), "Low")
    assert not bm["display"]["show_target"] and not bm["display"]["show_quantiles"]
    offer = {"role": "Software Engineer", "level": "Senior", "city": "Bengaluru", "years_experience": 6,
             "employment_type": "Full-time"}
    kw = dict(offer=offer, requisition=None, settings={}, metrics_age_days=1, accepted_peer=None)
    from workflow.checks import parse_package
    pkg = parse_package(package_form(bm, cash=bm["p50"]), {})
    ev = run_checks(pkg, bm, **kw)
    assert any(c["id"] == "override" and c["status"] == "block" for c in ev["checks"])
    pkg["low_confidence_ack"] = True
    ev = run_checks(pkg, bm, **kw)
    assert not any(c["id"] == "override" and c["status"] == "block" for c in ev["checks"])


def test_insufficient_cohort_blocks_recommendation(client):
    demo(client)
    # Even the widest cohort (role family + level, ~3k rows) can't reach this, so the benchmark is suppressed.
    settings = wdb.loads(q("SELECT settings FROM companies")[0]["settings"]) or {}
    run_sql("UPDATE companies SET settings = ?", (wdb.dumps({**settings, "min_cohort_size": 10**6}),))
    rid = new_offer(client)
    bm = benchmark(rid)
    assert bm["confidence"]["level"] == "Insufficient" and bm["display"]["mode"] == "none"
    page = client.get(f"/app/offers/{rid}").get_data(as_text=True)
    assert "No market range" in page and "Manual-entry reason" in page
    submit(client, rid, package_form(bm, cash=40))
    assert not q("SELECT * FROM submissions WHERE request_id = ?", (rid,))  # blocked without reason
    submit(client, rid, package_form(bm, cash=40, override_reason="competing_offer"))
    assert q("SELECT * FROM submissions WHERE request_id = ?", (rid,))


def test_pilot_mode_never_uses_synthetic_model(client):
    demo(client)
    as_role(client, "admin")
    assert post(client, "/app/settings/mode", {"mode": "pilot", "confirm_no_data": "on"}).status_code == 302
    as_role(client, "recruiter")
    rid = new_offer(client)
    bm = benchmark(rid)
    assert bm["p50"] is None and bm["data_mode"] == "pilot" and bm["display"]["mode"] == "none"
    submit(client, rid, package_form(bm, cash=42, override_reason="budget_constraint"))
    approve_all(client, rid)
    assert client.get(f"/app/offers/{rid}/brief.pdf").data[:4] == b"%PDF"
    page = client.get(f"/app/offers/{rid}/submissions/1?verify=1").get_data(as_text=True)
    assert "No model was run" in page
    summary = client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    assert "SAMPLE - NOT A REAL OFFER" not in summary  # watermark is for demo-data offers only


def test_demo_offers_are_watermarked(client):
    demo(client)
    rid = new_offer(client)
    page = client.get(f"/app/offers/{rid}").get_data(as_text=True)
    assert "Demo data - not for live hiring decisions" in page
    submit(client, rid, package_form(benchmark(rid)))
    approve_all(client, rid)
    assert "SAMPLE - NOT A REAL OFFER" in client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)


# ---------------------------------------------------------------- internal parity

def test_internal_parity_is_narrow_and_suppressed_when_small(client):
    demo(client)
    swe = benchmark(new_offer(client, requisition_id=1))  # 3 accepted SWE/Senior/Bengaluru/Full-time in seed
    assert swe["accepted_peer"]["n"] == 3 and not swe["accepted_peer"]["suppressed"]
    assert "Bengaluru" in swe["accepted_peer"]["definition"] and "Full-time" in swe["accepted_peer"]["definition"]
    pm_rid = new_offer(client, requisition_id=3, years=4)  # only 1 accepted PM/Mid/Bengaluru
    as_role(client, "recruiter")
    r = post(client, f"/app/offers/{pm_rid}/evaluate", package_form(benchmark(pm_rid)))
    html = r.get_data(as_text=True)
    assert "Not enough comparable accepted offers" in html and "not a pay-equity analysis" in html


# ---------------------------------------------------------------- CTC legal boundaries

def test_statutory_wage_is_separate_from_basic():
    s = structure_ctc(30, 3, "Bengaluru")
    assert s["sum_inr"] == s["total_inr"]
    assert s["statutory_wage_inr"] >= s["remuneration_inr"] * 0.5
    basic = next(r["inr"] for r in s["rows"] if r["component"] == "Basic + DA")
    assert s["statutory_wage_inr"] == basic + s["wage_addback_inr"]


def test_ctc_warnings_require_acknowledgment_before_candidate_export(client):
    demo(client)
    rid = new_offer(client)
    bm = benchmark(rid)
    form = package_form(bm, variable_share=0.3)  # squeezes HRA below convention -> CTC warning
    assert submit(client, rid, form).status_code == 302
    approve_all(client, rid)
    page = client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    assert "This package has CTC structure warnings" in page
    post(client, f"/app/offers/{rid}/candidate-summary", {})  # no confirmation
    assert "This package has CTC structure warnings" in client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    post(client, f"/app/offers/{rid}/candidate-summary", {"confirm": "on"})
    page = client.get(f"/app/offers/{rid}/candidate-summary").get_data(as_text=True)
    assert "acknowledged by" in page
    doc = candidate_doc(page)
    assert "P50" not in doc and "cohort" not in doc.lower()
    assert q("SELECT * FROM audit_log WHERE action = 'acknowledge_ctc_warnings'")


def test_ctc_panel_labels_decision_support(client):
    demo(client)
    rid = new_offer(client)
    html = post(client, f"/app/offers/{rid}/evaluate", package_form(benchmark(rid))).get_data(as_text=True)
    assert "Decision support only" in html and "2026.04-r2" in html and "Statutory wage" in html


# ---------------------------------------------------------------- currency fix on the legacy calculator

def test_legacy_calculator_scores_arr_per_employee_in_usd(client):
    form = {"mrr": 5_000_000, "prior_mrr": 4_800_000, "revenue": 60_000_000, "cogs": 15_000_000,
            "customers_lost": 5, "customers_start": 100, "sm_spend": 10_000_000, "new_customers": 20,
            "arpa": 600_000, "headcount": 100, "profit_margin_pct": -10, "prior_quarter_revenue": 14_000_000,
            "current_quarter_revenue": 15_000_000, "prior_quarter_sm_spend": 2_500_000, "tam": 1e10, "sam": 1e9,
            "som": 1e8}
    bundle = main.compute_saas_metrics_bundle(form)
    # Rs.6 Cr ARR / 100 people = Rs.6 lakh each = ~$6.8k, far below the $200k benchmark.
    assert bundle["arr_per_employee_usd"] == pytest.approx(600_000 / 88, rel=1e-6)
    assert bundle["health"].components["arr_per_employee"] < 5
