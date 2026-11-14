"""
SQLite storage for the offer workflow (stdlib only, no ORM).

Tenant data lives in plain tables keyed by company_id. Anything the PRD calls
immutable — metric snapshots, offer submissions, approval actions, the audit
log — is protected by triggers that abort any UPDATE or DELETE, so history
can't be rewritten even by a bug in the app code.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT,
    sector TEXT,
    hq_city TEXT,
    funding_stage TEXT NOT NULL,
    target_market TEXT NOT NULL,
    pricing_model TEXT NOT NULL,
    employee_count INTEGER,
    arr_band TEXT,
    hiring_locations TEXT NOT NULL DEFAULT '[]',
    work_model TEXT,
    settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    created_at TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    source TEXT NOT NULL,
    inputs TEXT NOT NULL,
    derived TEXT NOT NULL,
    health_score REAL NOT NULL,
    health_band TEXT NOT NULL,
    health_score_version TEXT NOT NULL,
    completeness REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bands (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    role TEXT NOT NULL,
    level TEXT NOT NULL,
    city_tier TEXT NOT NULL,
    min_lpa REAL NOT NULL,
    mid_lpa REAL NOT NULL,
    max_lpa REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    UNIQUE (company_id, role, level, city_tier)
);

CREATE TABLE IF NOT EXISTS requisitions (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    role TEXT NOT NULL,
    level TEXT NOT NULL,
    city TEXT NOT NULL,
    budget_max_lpa REAL,
    openings INTEGER NOT NULL DEFAULT 1,
    hiring_manager_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open',
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_requests (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    requisition_id INTEGER REFERENCES requisitions(id),
    candidate_name TEXT,
    candidate_pseudo_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    level TEXT NOT NULL,
    city TEXT NOT NULL,
    years_experience REAL NOT NULL,
    skill_category TEXT NOT NULL,
    education_pedigree TEXT NOT NULL,
    current_ctc REAL,
    expected_ctc REAL,
    competing_offer REAL,
    start_date TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    metrics_snapshot_id INTEGER REFERENCES metric_snapshots(id),
    benchmark TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES offer_requests(id),
    label TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    UNIQUE (request_id, label)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES offer_requests(id),
    revision INTEGER NOT NULL,
    package_label TEXT NOT NULL,
    submitted_by INTEGER REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    route TEXT NOT NULL,
    UNIQUE (request_id, revision)
);

CREATE TABLE IF NOT EXISTS approval_actions (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    step INTEGER,
    approver_role TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    comment TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_rules (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL,
    condition_kind TEXT NOT NULL,
    condition_value TEXT,
    approver_role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    request_id INTEGER UNIQUE REFERENCES offer_requests(id),
    external INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    level TEXT NOT NULL,
    city TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_date TEXT,
    verbal_acceptance_date TEXT,
    decision_date TEXT,
    final_cash_ctc REAL,
    final_esop_options INTEGER,
    competing_offer REAL,
    decline_reason TEXT,
    rec_p25 REAL,
    rec_p50 REAL,
    rec_p75 REAL,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Anonymised copy of outcomes, written only when the company has opted in to
-- data sharing. No names, no company identity, city reduced to its tier.
CREATE TABLE IF NOT EXISTS benchmark_observations (
    id INTEGER PRIMARY KEY,
    outcome_id INTEGER NOT NULL UNIQUE REFERENCES outcomes(id),
    contributor_key TEXT NOT NULL,
    role TEXT NOT NULL,
    level TEXT NOT NULL,
    city_tier TEXT NOT NULL,
    funding_stage TEXT NOT NULL,
    health_band TEXT,
    status TEXT NOT NULL,
    final_cash_ctc REAL,
    observed_month TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    message TEXT NOT NULL,
    link TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    company_id INTEGER,
    user_id INTEGER,
    user_role TEXT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""

IMMUTABLE_TABLES = ("metric_snapshots", "submissions", "approval_actions", "audit_log")


def _trigger_statements() -> list[str]:
    stmts = []
    for table in IMMUTABLE_TABLES:
        for op in ("UPDATE", "DELETE"):
            stmts.append(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
                         f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END")
    return stmts


def _immutability_triggers() -> str:
    return ";\n".join(_trigger_statements()) + ";"


DEMO_USERS = [
    ("Riya Menon", "riya.recruiter@demo.compintel", "recruiter"),
    ("Arjun Rao", "arjun.hm@demo.compintel", "hiring_manager"),
    ("Kavya Iyer", "kavya.people@demo.compintel", "people_ops"),
    ("Vikram Shah", "vikram.finance@demo.compintel", "finance"),
    ("Neha Kapoor", "neha.admin@demo.compintel", "admin"),
    ("Sanjay Pillai", "sanjay.exec@demo.compintel", "exec"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path() -> Path:
    configured = current_app.config.get("COMPINTEL_DB") or os.environ.get("COMPINTEL_DB")
    if configured:
        return Path(configured)
    return Path(current_app.root_path) / "instance" / "compintel_demo.db"


def get_db() -> sqlite3.Connection:
    if "compintel_db" not in g:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The cohort worker writes from its own thread/connection; wait for a lock rather than fail.
        conn = sqlite3.connect(path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.compintel_db = conn
    return g.compintel_db


def close_db(_exc=None):
    conn = g.pop("compintel_db", None)
    if conn is not None:
        conn.close()


# --------------------------------------------------------------------------
# Migrations. Each runs once, in order, and is recorded in schema_migrations.
# Never edit a released migration; add a new one.
# --------------------------------------------------------------------------

def _m001_initial(conn):
    conn.executescript(SCHEMA)
    conn.executescript(_immutability_triggers())


def _m002_integrity_modes_employment(conn):
    from .integrity import GENESIS, action_hash, audit_entry_hash

    for stmt in (
        "ALTER TABLE audit_log ADD COLUMN context TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT",
        "ALTER TABLE audit_log ADD COLUMN entry_hash TEXT",
        "ALTER TABLE approval_actions ADD COLUMN content_hash TEXT",
        "ALTER TABLE submissions ADD COLUMN context TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE companies ADD COLUMN mode TEXT NOT NULL DEFAULT 'demo'",
        "ALTER TABLE requisitions ADD COLUMN employment_type TEXT NOT NULL DEFAULT 'Full-time'",
        "ALTER TABLE offer_requests ADD COLUMN employment_type TEXT NOT NULL DEFAULT 'Full-time'",
        "ALTER TABLE outcomes ADD COLUMN employment_type TEXT NOT NULL DEFAULT 'Full-time'",
        "ALTER TABLE outcomes ADD COLUMN health_band TEXT",
    ):
        conn.execute(stmt)
    # Backfill hashes for rows written before the chain existed. The only place
    # the immutability triggers are lifted, and only inside this migration.
    for table in ("audit_log", "approval_actions"):
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
    prev = GENESIS
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall():
        row = dict(row)
        row["prev_hash"] = prev
        h = audit_entry_hash(prev, row)
        conn.execute("UPDATE audit_log SET prev_hash = ?, entry_hash = ? WHERE id = ?", (prev, h, row["id"]))
        prev = h
    snaps = {r["id"]: r["snapshot_hash"] for r in conn.execute("SELECT id, snapshot_hash FROM submissions")}
    for row in conn.execute("SELECT * FROM approval_actions").fetchall():
        conn.execute("UPDATE approval_actions SET content_hash = ? WHERE id = ?",
                     (action_hash(row, snaps.get(row["submission_id"], "")), row["id"]))
    for stmt in _trigger_statements():
        conn.execute(stmt)


def _m003_onboarding(conn):
    conn.executescript("""
    -- Per-user first-run state, so onboarding can be role-specific and time-to-value measured.
    CREATE TABLE IF NOT EXISTS user_onboarding (
        user_id INTEGER PRIMARY KEY REFERENCES users(id),
        first_seen_at TEXT NOT NULL,
        first_benchmark_at TEXT,
        dismissed_at TEXT
    );
    -- A recruiter asks Finance for company metrics instead of being blocked on them.
    CREATE TABLE IF NOT EXISTS metrics_requests (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        requested_by INTEGER NOT NULL REFERENCES users(id),
        requested_at TEXT NOT NULL,
        note TEXT,
        fulfilled_at TEXT,
        fulfilled_snapshot_id INTEGER REFERENCES metric_snapshots(id)
    );
    """)


def _m004_pilot_network_telemetry(conn):
    """V2 pilot: the benchmark network can hold design-partner records (no tenant
    outcome behind them), each row carries the offer-outcome telemetry the PRD asks
    for, and outcomes record the initial offer and any candidate counter."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS network_import_batches (
        id INTEGER PRIMARY KEY,
        imported_by INTEGER REFERENCES users(id),
        imported_at TEXT NOT NULL,
        filename TEXT,
        file_sha256 TEXT NOT NULL UNIQUE,
        rows INTEGER NOT NULL,
        contributors INTEGER NOT NULL,
        consent_basis TEXT NOT NULL,
        note TEXT,
        removed_at TEXT,
        removed_by INTEGER REFERENCES users(id)
    );
    CREATE TABLE benchmark_observations_v2 (
        id INTEGER PRIMARY KEY,
        outcome_id INTEGER UNIQUE REFERENCES outcomes(id),
        batch_id INTEGER REFERENCES network_import_batches(id),
        source TEXT NOT NULL DEFAULT 'tenant_outcome',
        record_hash TEXT UNIQUE,
        consent_basis TEXT,
        contributor_key TEXT NOT NULL,
        role TEXT NOT NULL,
        level TEXT NOT NULL,
        city_tier TEXT NOT NULL,
        funding_stage TEXT NOT NULL,
        employment_type TEXT,
        employee_band TEXT,
        arr_band TEXT,
        ndr_band TEXT,
        health_band TEXT,
        status TEXT NOT NULL,
        initial_cash_ctc REAL,
        variable_pct REAL,
        initial_esop_options INTEGER,
        counter_cash_ctc REAL,
        counter_esop_options INTEGER,
        final_cash_ctc REAL,
        final_esop_options INTEGER,
        decline_reason TEXT,
        competing_offer REAL,
        observed_month TEXT NOT NULL
    );
    INSERT INTO benchmark_observations_v2 (id, outcome_id, contributor_key, role, level, city_tier, funding_stage,
        health_band, status, final_cash_ctc, observed_month, source, consent_basis)
        SELECT id, outcome_id, contributor_key, role, level, city_tier, funding_stage, health_band, status,
               final_cash_ctc, observed_month, 'tenant_outcome', 'Workspace data-sharing setting'
        FROM benchmark_observations;
    DROP TABLE benchmark_observations;
    ALTER TABLE benchmark_observations_v2 RENAME TO benchmark_observations;
    CREATE INDEX IF NOT EXISTS ix_obs_lookup ON benchmark_observations (role, level, status, observed_month);
    """)
    for stmt in (
        "ALTER TABLE outcomes ADD COLUMN initial_cash_ctc REAL",
        "ALTER TABLE outcomes ADD COLUMN initial_esop_options INTEGER",
        "ALTER TABLE outcomes ADD COLUMN counter_cash_ctc REAL",
        "ALTER TABLE outcomes ADD COLUMN counter_esop_options INTEGER",
    ):
        conn.execute(stmt)


def _m005_site(conn):
    """Public site: pilot applications (submitted deliberately by the applicant) and
    anonymous funnel events (no IP, no user agent, no free text)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS pilot_applications (
        id INTEGER PRIMARY KEY,
        submitted_at TEXT NOT NULL,
        name TEXT NOT NULL,
        work_email TEXT NOT NULL,
        company TEXT NOT NULL,
        persona TEXT NOT NULL,
        employees TEXT NOT NULL,
        hires TEXT NOT NULL,
        cities TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        consent_contact INTEGER NOT NULL,
        source TEXT,
        status TEXT NOT NULL DEFAULT 'new'
    );
    CREATE TABLE IF NOT EXISTS site_events (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS ix_site_events_event ON site_events (event, ts);
    """)


def _m006_realtime(conn):
    """V3 real-time layer: HRIS/ATS connections, the normalised (PII-free) offer-event
    log, streamed cohort snapshots, and a link from outcomes to their source record."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS integration_connections (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        provider TEXT NOT NULL,
        name TEXT NOT NULL,
        hook_key TEXT NOT NULL UNIQUE,
        secret TEXT NOT NULL,
        field_map TEXT NOT NULL DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        last_event_at TEXT,
        last_error TEXT,
        last_error_at TEXT
    );
    -- One row per normalised record (or per rejected delivery). Raw payloads are never
    -- stored: they can carry candidate names and emails. Only their SHA-256 is kept.
    CREATE TABLE IF NOT EXISTS offer_events (
        id INTEGER PRIMARY KEY,
        connection_id INTEGER REFERENCES integration_connections(id),
        company_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        received_at TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        record_index INTEGER NOT NULL DEFAULT 0,
        source_ref TEXT,
        schema_version TEXT,
        kind TEXT,
        status TEXT,
        role TEXT,
        level TEXT,
        city TEXT,
        city_tier TEXT,
        fixed_ctc_lpa REAL,
        variable_ctc_lpa REAL,
        cash_ctc_lpa REAL,
        esop_grant INTEGER,
        company_stage TEXT,
        counter_cash_ctc_lpa REAL,
        occurred_at TEXT,
        disposition TEXT NOT NULL,
        error TEXT,
        outcome_id INTEGER REFERENCES outcomes(id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_offer_events_delivery ON offer_events (connection_id, payload_sha256, record_index);
    CREATE INDEX IF NOT EXISTS ix_offer_events_recent ON offer_events (company_id, id);
    CREATE TABLE IF NOT EXISTS cohort_snapshots (
        id INTEGER PRIMARY KEY,
        cohort_key TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        trigger TEXT NOT NULL,
        n INTEGER NOT NULL,
        contributors INTEGER NOT NULL,
        p25 REAL, p50 REAL, p75 REAL,
        suppressed INTEGER NOT NULL,
        alert INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_cohort_snapshots_key ON cohort_snapshots (cohort_key, id);
    CREATE INDEX IF NOT EXISTS ix_obs_cohort ON benchmark_observations (role, level, city_tier, funding_stage, observed_month);
    """)
    conn.execute("ALTER TABLE outcomes ADD COLUMN source_ref TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_outcomes_source_ref ON outcomes (company_id, source_ref) "
                 "WHERE source_ref IS NOT NULL")


MIGRATIONS = [
    (1, "initial schema", _m001_initial),
    (2, "audit hash chain, session context, workspace mode, employment type", _m002_integrity_modes_employment),
    (3, "per-user onboarding state and metrics requests", _m003_onboarding),
    (4, "pilot benchmark network, partner import batches, offer telemetry", _m004_pilot_network_telemetry),
    (5, "public site: pilot applications and anonymous funnel events", _m005_site),
    (6, "real-time layer: integrations, offer events, cohort snapshots", _m006_realtime),
]


def migrate(conn) -> list[int]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "applied_at TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    ran = []
    for version, name, fn in MIGRATIONS:
        if version in applied:
            continue
        try:
            fn(conn)
            conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                         (version, name, now_iso()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ran.append(version)
    return ran


def init_db(app):
    with app.app_context():
        conn = get_db()
        migrate(conn)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.executemany("INSERT INTO users (name, email, role) VALUES (?, ?, ?)", DEMO_USERS)
        conn.commit()
        close_db()


def query(sql: str, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, params=()) -> int:
    cur = get_db().execute(sql, params)
    return cur.lastrowid


def commit():
    get_db().commit()


def loads(value, default=None):
    if value in (None, ""):
        return default
    return json.loads(value)


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)
