"""One-click demo workspace: a sample company with history, bands, open
requisitions and a few past outcomes, so every screen has something to show."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import current_app

from . import db
from .approvals import DEFAULT_RULES
from .auth import audit
from .company import compute_health_profile

DEMO_COMPANY = {
    "name": "Northwind Ledger (demo)",
    "domain": "demo.compintel",
    "sector": "B2B SaaS - finance automation",
    "hq_city": "Bengaluru",
    "funding_stage": "Series A",
    "target_market": "Cross-border",
    "pricing_model": "Subscription",
    "employee_count": 120,
    "arr_band": "Rs.25-100 Cr",
    "hiring_locations": ["Bengaluru", "Pune", "Chennai", "Remote (other)"],
    "work_model": "Hybrid",
}

OLD_METRICS = {"arr_band": "Rs.25-100 Cr", "headcount": 95, "ndr_pct": 108}
CURRENT_METRICS = {"arr_cr": 60, "arr_growth_pct": 95, "operating_margin_pct": -25, "ndr_pct": 118,
                   "annual_churn_pct": 8, "cac_lakh": 6, "ltv_lakh": 24, "gross_margin_pct": 76,
                   "customers": 450, "headcount": 120, "arr_band": None,
                   # Affordability: ~19 months of runway; Rs.12 Cr for 30 planned hires = Rs.40 lakh per hire.
                   "cash_balance_cr": 85, "monthly_burn_lakh": 450, "hiring_budget_cr": 12, "planned_hires": 30}

REQUISITIONS = [
    ("Software Engineer", "Senior", "Bengaluru", 48, 2, "Payments platform team"),
    ("Account Executive", "Mid", "Pune", 20, 3, "Mid-market India + SEA"),
    ("Product Manager", "Mid", "Bengaluru", 38, 1, "Reconciliation product"),
]

PAST_OUTCOMES = [
    ("Software Engineer", "Senior", "Bengaluru", "accepted", 41.0, None),
    ("Software Engineer", "Senior", "Bengaluru", "accepted", 44.5, None),
    ("Software Engineer", "Senior", "Bengaluru", "accepted", 39.8, None),
    ("Software Engineer", "Senior", "Pune", "declined", None, "Accepted competing offer"),
    ("Account Executive", "Mid", "Pune", "accepted", 17.0, None),
    ("Account Executive", "Mid", "Pune", "accepted", 18.2, None),
    ("Account Executive", "Mid", "Pune", "accepted", 19.1, None),
    ("Account Executive", "Mid", "Chennai", "declined", None, "Compensation (cash)"),
    ("Product Manager", "Mid", "Bengaluru", "accepted", 33.0, None),
]


def _metrics_row(company_id, user_id, inputs, created_at, stage_default, settings):
    profile = compute_health_profile(inputs, DEMO_COMPANY["funding_stage"], stage_default, float(settings["inr_per_usd"]))
    db.execute(
        "INSERT INTO metric_snapshots (company_id, created_at, created_by, source, inputs, derived, health_score, "
        "health_band, health_score_version, completeness) VALUES (?, ?, ?, 'demo seed', ?, ?, ?, ?, ?, ?)",
        (company_id, created_at, user_id, db.dumps(inputs), db.dumps(profile), profile["health_score"],
         profile["health_band"], profile["version"], profile["completeness"]))
    return profile


def seed_demo_workspace(user, cohorts):
    from .routes import DEFAULT_SETTINGS, contributor_key  # avoid import cycle at module load

    now = datetime.now(timezone.utc).replace(microsecond=0)
    iso = now.isoformat()
    c = DEMO_COMPANY
    cid = db.execute(
        "INSERT INTO companies (name, domain, sector, hq_city, funding_stage, target_market, pricing_model, employee_count, "
        "arr_band, hiring_locations, work_model, settings, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
        (c["name"], c["domain"], c["sector"], c["hq_city"], c["funding_stage"], c["target_market"], c["pricing_model"],
         c["employee_count"], c["arr_band"], db.dumps(c["hiring_locations"]), c["work_model"], iso, iso))
    for name, kind, value, role in DEFAULT_RULES:
        db.execute("INSERT INTO approval_rules (company_id, name, condition_kind, condition_value, approver_role) "
                   "VALUES (?, ?, ?, ?, ?)", (cid, name, kind, value, role))

    stage_default = float(current_app.config["STAGE_DEFAULT_HEALTH"].get(c["funding_stage"], 50.0))
    _metrics_row(cid, user["id"], OLD_METRICS, (now - timedelta(days=210)).isoformat(), stage_default, DEFAULT_SETTINGS)
    current = _metrics_row(cid, user["id"], CURRENT_METRICS, (now - timedelta(days=12)).isoformat(), stage_default,
                           DEFAULT_SETTINGS)

    for row in cohorts.band_seed(c["funding_stage"], c["target_market"]):
        mid = round(row["cash_ctc_lpa"], 1)
        db.execute("INSERT OR IGNORE INTO bands (company_id, role, level, city_tier, min_lpa, mid_lpa, max_lpa, source, "
                   "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, 'seeded from demo dataset', ?, ?)",
                   (cid, row["role"], row["level"], row["saas_hub_tier"], round(mid * 0.8, 1), mid, round(mid * 1.2, 1),
                    iso, user["id"]))

    hm = db.query("SELECT id FROM users WHERE role = 'hiring_manager' ORDER BY id", one=True)
    for role, level, city, budget, openings, notes in REQUISITIONS:
        db.execute("INSERT INTO requisitions (company_id, role, level, city, budget_max_lpa, openings, hiring_manager_id, "
                   "notes, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (cid, role, level, city, budget, openings, hm["id"] if hm else None, notes, user["id"], iso))

    from saas_domain import CITIES
    for i, (role, level, city, status, final, reason) in enumerate(PAST_OUTCOMES):
        decided = (now - timedelta(days=30 + i * 9)).date().isoformat()
        oid = db.execute(
            "INSERT INTO outcomes (company_id, request_id, external, role, level, city, status, decision_date, final_cash_ctc, "
            "decline_reason, notes, created_by, created_at, updated_at, employment_type, health_band) VALUES (?, NULL, 1, "
            "?, ?, ?, ?, ?, ?, ?, 'Imported historical offer (demo seed)', ?, ?, ?, 'Full-time', ?)",
            (cid, role, level, city, status, decided, final, reason, user["id"], iso, iso, current["health_band"]))
        db.execute("INSERT INTO benchmark_observations (outcome_id, contributor_key, role, level, city_tier, funding_stage, "
                   "health_band, status, final_cash_ctc, decline_reason, observed_month) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (oid, contributor_key(cid), role, level, CITIES[city][0], c["funding_stage"], current["health_band"],
                    status, final, reason, decided[:7]))

    audit("load_demo_workspace", "company", cid, {"name": c["name"]}, cid)
    db.commit()
    return cid


def upgrade_demo_workspace() -> list[str]:
    """Brings a demo workspace seeded by an older version up to the current seed,
    without rewriting history (metric snapshots are immutable, so it appends one).
    Idempotent; only touches the seeded demo company. Returns what it changed."""
    from .auth import system_audit

    company = db.query("SELECT * FROM companies WHERE domain = ?", (DEMO_COMPANY["domain"],), one=True)
    if company is None:
        return []
    changed = []
    latest = db.query("SELECT * FROM metric_snapshots WHERE company_id = ? ORDER BY id DESC LIMIT 1",
                      (company["id"],), one=True)
    if latest is not None and latest["source"].startswith("demo seed") \
            and "cash_balance_cr" not in db.loads(latest["inputs"], {}):
        from .routes import DEFAULT_SETTINGS
        stage_default = float(current_app.config["STAGE_DEFAULT_HEALTH"].get(company["funding_stage"], 50.0))
        profile = compute_health_profile(CURRENT_METRICS, company["funding_stage"], stage_default,
                                         float(DEFAULT_SETTINGS["inr_per_usd"]))
        db.execute(
            "INSERT INTO metric_snapshots (company_id, created_at, created_by, source, inputs, derived, health_score, "
            "health_band, health_score_version, completeness) VALUES (?, ?, NULL, 'demo seed (upgrade)', ?, ?, ?, ?, ?, ?)",
            (company["id"], db.now_iso(), db.dumps(CURRENT_METRICS), db.dumps(profile), profile["health_score"],
             profile["health_band"], profile["version"], profile["completeness"]))
        changed.append("added cash, burn and hiring plan to the demo metrics")
    missing = db.query("SELECT b.id, o.decline_reason FROM benchmark_observations b JOIN outcomes o ON o.id = b.outcome_id "
                       "WHERE o.company_id = ? AND b.decline_reason IS NULL AND o.decline_reason IS NOT NULL",
                       (company["id"],))
    for row in missing:
        db.execute("UPDATE benchmark_observations SET decline_reason = ? WHERE id = ?", (row["decline_reason"], row["id"]))
    if missing:
        changed.append(f"copied {len(missing)} decline reasons to the shared observations")
    if changed:
        system_audit("upgrade_demo_workspace", "company", company["id"], {"changes": changed}, company["id"],
                     {"source": "startup"})
        db.commit()
    return changed
