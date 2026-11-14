"""
Views for the V3 real-time layer, on the same /app blueprint (same demo gate, CSRF,
roles and audit as the rest of the workflow):

  /app/market               live cohort percentiles + clearing price, SSE-updated
  /app/market/stream        Server-Sent Events for one cohort
  /app/market/snapshot.json the current snapshot as JSON
  /app/integrations         HRIS/ATS connections, delivery log, dry-run preview
"""
from __future__ import annotations

import hashlib
import json
import math
import queue
import random
import time
from datetime import date

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for

from saas_domain import CITIES, FUNDING_STAGES

from . import db, streaming
from .auth import audit, current_user, require
from .integrations import ingest
from .integrations import providers as prov
from .routes import ALL_ROLES, LEVELS, bp, require_company, settings_for

TIERS = sorted({tier for tier, _m in CITIES.values()})
DEMO_CONSENT = "Synthetic demo sample - not real data"


def _cohort_from_args(company) -> tuple[str, dict]:
    args = request.args
    role = args.get("role") or "Software Engineer"
    level = args.get("level") or "Senior"
    tier = args.get("tier") or (CITIES[args["city"]][0] if args.get("city") in CITIES else None) \
        or CITIES.get(company["hq_city"], ("Tier 1 SaaS Hub",))[0]
    stage = args.get("stage") or company["funding_stage"]
    if role not in ALL_ROLES or level not in LEVELS or tier not in TIERS or stage not in FUNDING_STAGES:
        abort(400, description="Unknown role, level, city tier or funding stage.")
    return streaming.cohort_key(role, level, tier, stage), {"role": role, "level": level, "tier": tier, "stage": stage}


@bp.route("/market")
@require("view_benchmark")
def market():
    company = require_company()
    settings = settings_for(company)
    key, sel = _cohort_from_args(company)
    snap = streaming.compute_snapshot(key, settings)
    history = db.query("SELECT id, computed_at, trigger, n, contributors, p25, p50, p75, suppressed, alert "
                       "FROM cohort_snapshots WHERE cohort_key = ? ORDER BY id DESC LIMIT 12", (key,))
    alerts = [db.loads(r["data"]) for r in db.query(
        "SELECT s.data FROM cohort_snapshots s JOIN (SELECT cohort_key, MAX(id) mid FROM cohort_snapshots "
        "GROUP BY cohort_key) m ON m.mid = s.id WHERE s.alert = 1 ORDER BY s.id DESC LIMIT 10")]
    busiest = db.query(
        "SELECT role, level, city_tier, funding_stage, COUNT(*) n, COUNT(DISTINCT contributor_key) contributors "
        "FROM benchmark_observations WHERE observed_month >= ? GROUP BY role, level, city_tier, funding_stage "
        "ORDER BY n DESC LIMIT 8", (streaming._month_floor(date.today(), int(settings["live_window_days"])),))
    demo_batch = db.query("SELECT * FROM network_import_batches WHERE consent_basis = ? AND removed_at IS NULL",
                          (DEMO_CONSENT,), one=True)
    return render_template("workflow/market.html", snap=snap, sel=sel, key=key, history=history, alerts=alerts,
                           busiest=busiest, roles=ALL_ROLES, levels=LEVELS, tiers=TIERS, stages=list(FUNDING_STAGES),
                           backend=streaming.pipeline.backend, company=company, demo_batch=demo_batch,
                           settings=settings)


@bp.route("/market/snapshot.json")
@require("view_benchmark")
def market_snapshot_json():
    company = require_company()
    key, _sel = _cohort_from_args(company)
    return jsonify(streaming.compute_snapshot(key, settings_for(company)))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@bp.route("/market/stream")
@require("view_benchmark")
def market_stream():
    """Sends the current snapshot, then every recompute of this cohort as it happens.
    Closes after max_seconds (default 5 minutes); EventSource reconnects on its own,
    which keeps a long-lived connection from pinning a worker forever."""
    company = require_company()
    key, _sel = _cohort_from_args(company)
    initial = streaming.compute_snapshot(key, settings_for(company))
    try:
        max_seconds = max(0.0, min(float(request.args.get("max_seconds", 300)), 900.0))
    except ValueError:
        max_seconds = 300.0
    hub = streaming.pipeline.hub
    q = hub.subscribe(key)

    def generate():
        try:
            yield "retry: 3000\n" + _sse("snapshot", initial)
            deadline = time.monotonic() + max_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = q.get(timeout=min(15.0, remaining))
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse("snapshot", msg)
        finally:
            hub.unsubscribe(key, q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# Demo network sample (so the live market has something to show in demo mode)
# --------------------------------------------------------------------------

def demo_network_rows(stage: str, today: date | None = None, seed: int = 11) -> list[dict]:
    """Synthetic partner offers across 8 pseudonymous companies and 5 months. In the
    Software Engineer / Senior / Tier 1 cohort candidates' asks rise 22% in the last
    two months while companies lift offers only 4%, so offers at the benchmark P50
    start getting declined - the pattern the clearing-price alert exists to catch.
    The other two cohorts are stable and mostly clear."""
    today = today or date.today()
    rng = random.Random(seed)
    cohorts = [("Software Engineer", "Senior", "Tier 1 SaaS Hub", 42.0, True),
               ("Account Executive", "Mid", "Tier 2 SaaS Hub", 18.0, False),
               ("Product Manager", "Mid", "Tier 1 SaaS Hub", 34.0, False)]
    rows = []
    for role, level, tier, base, shifts in cohorts:
        for months_ago in range(5):
            total = today.year * 12 + today.month - 1 - months_ago
            month = f"{total // 12:04d}-{total % 12 + 1:02d}"
            # Candidates' reservation price relative to the offers companies make.
            moved = shifts and months_ago <= 1
            reservation = base * (1.22 if moved else 1.0 if shifts else 0.97)
            offer_level = base * (1.04 if moved else 1.0)
            for _ in range(20):
                contributor = f"demo-partner-{rng.randrange(8)}"
                offer = round(offer_level * math.exp(rng.gauss(0, 0.10)), 2)
                p_accept = 1 / (1 + math.exp(-(offer / reservation - 1) * 25))
                accepted = rng.random() < p_accept
                counter = round(offer * rng.uniform(1.08, 1.18), 2) if rng.random() < 0.35 else None
                final = (round(min(counter, offer * 1.06), 2) if counter and accepted else offer) if accepted else None
                rec = {"contributor_key": hashlib.sha256(contributor.encode()).hexdigest()[:12], "role": role,
                       "level": level, "city_tier": tier, "funding_stage": stage, "employment_type": "Full-time",
                       "status": "accepted" if accepted else "declined", "observed_month": month,
                       "initial_cash_ctc": offer, "counter_cash_ctc": counter, "final_cash_ctc": final,
                       "decline_reason": None if accepted else rng.choice(
                           ["Compensation (cash)", "Accepted competing offer", "Compensation (cash)"])}
                rec["record_hash"] = hashlib.sha256(db.dumps([rec, rng.random()]).encode()).hexdigest()
                rows.append(rec)
    return rows


@bp.route("/market/demo-sample", methods=["POST"])
@require("manage_network_data")
def market_demo_sample():
    company = require_company()
    if company["mode"] != "demo":
        abort(400, description="The synthetic market sample can only be loaded in a demo workspace.")
    if db.query("SELECT id FROM network_import_batches WHERE consent_basis = ? AND removed_at IS NULL",
                (DEMO_CONSENT,), one=True):
        flash("The demo market sample is already loaded.", "error")
        return redirect(url_for("wf.market"))
    rows = demo_network_rows(company["funding_stage"])
    batch_id = db.execute(
        "INSERT INTO network_import_batches (imported_by, imported_at, filename, file_sha256, rows, contributors, "
        "consent_basis, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (current_user()["id"], db.now_iso(), "demo-market-sample", hashlib.sha256(db.now_iso().encode()).hexdigest(),
         len(rows), len({r["contributor_key"] for r in rows}), DEMO_CONSENT,
         "Synthetic offers for the live-market demo. Remove under Benchmark data."))
    cols = ["contributor_key", "role", "level", "city_tier", "funding_stage", "employment_type", "status",
            "observed_month", "initial_cash_ctc", "counter_cash_ctc", "final_cash_ctc", "decline_reason", "record_hash"]
    for r in rows:
        db.execute(f"INSERT INTO benchmark_observations (batch_id, source, consent_basis, {', '.join(cols)}) "
                   f"VALUES (?, 'partner_import', ?, {', '.join('?' for _ in cols)})",
                   (batch_id, DEMO_CONSENT, *[r[c] for c in cols]))
        streaming.queue_observation(r)
    audit("load_demo_market_sample", "network_import_batches", batch_id, {"rows": len(rows)}, company["id"])
    db.commit()
    flash(f"Loaded {len(rows)} synthetic partner offers (batch #{batch_id}). Remove them under Benchmark data.",
          "success")
    return redirect(url_for("wf.market"))


# --------------------------------------------------------------------------
# Integrations admin
# --------------------------------------------------------------------------

def _connections(company_id):
    conns = [dict(r) for r in db.query("SELECT * FROM integration_connections WHERE company_id = ? ORDER BY id",
                                       (company_id,))]
    counts = {}
    for r in db.query("SELECT connection_id, disposition, COUNT(*) n FROM offer_events WHERE company_id = ? "
                      "GROUP BY connection_id, disposition", (company_id,)):
        counts.setdefault(r["connection_id"], {})[r["disposition"]] = r["n"]
    for c in conns:
        c["counts"] = counts.get(c["id"], {})
        c["provider_obj"] = prov.PROVIDERS.get(c["provider"])
        c["field_map_pretty"] = json.dumps(db.loads(c["field_map"], {}), indent=2) if c["field_map"] != "{}" else ""
    return conns


def _render_integrations(company, **extra):
    events = db.query("SELECT e.*, c.name AS connection_name FROM offer_events e LEFT JOIN integration_connections c "
                      "ON c.id = e.connection_id WHERE e.company_id = ? ORDER BY e.id DESC LIMIT 30", (company["id"],))
    return render_template("workflow/integrations.html", company=company, connections=_connections(company["id"]),
                           providers=prov.PROVIDERS, events=events, sig_header=prov.GENERIC_SIG_HEADER,
                           token_header=prov.GENERIC_TOKEN_HEADER, **extra)


def _connection(company, connection_id):
    conn = db.query("SELECT * FROM integration_connections WHERE id = ? AND company_id = ?",
                    (connection_id, company["id"]), one=True)
    if conn is None:
        abort(404, description="Integration not found.")
    return conn


@bp.route("/integrations")
@require("manage_integrations")
def integrations_page():
    return _render_integrations(require_company())


@bp.route("/integrations/connections", methods=["POST"])
@require("manage_integrations")
def integration_create():
    company = require_company()
    key = request.form.get("provider")
    if key not in prov.PROVIDERS:
        abort(400, description="Unknown provider.")
    name = (request.form.get("name") or prov.PROVIDERS[key].name).strip()[:80]
    hook_key, secret = ingest.new_credentials()
    cid = db.execute("INSERT INTO integration_connections (company_id, provider, name, hook_key, secret, created_by, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (company["id"], key, name, hook_key, secret, current_user()["id"], db.now_iso()))
    audit("create_integration", "integration_connections", cid, {"provider": key, "name": name}, company["id"])
    db.commit()
    # The secret is shown once, on this response only (never flashed: the session cookie is signed, not encrypted).
    return _render_integrations(company, revealed={"id": cid, "secret": secret,
                                                   "url": url_for("api_v1.integration_webhook", hook_key=hook_key,
                                                                  _external=True)})


@bp.route("/integrations/connections/<int:connection_id>/rotate", methods=["POST"])
@require("manage_integrations")
def integration_rotate(connection_id):
    company = require_company()
    conn = _connection(company, connection_id)
    _hook, secret = ingest.new_credentials()
    db.execute("UPDATE integration_connections SET secret = ? WHERE id = ?", (secret, conn["id"]))
    audit("rotate_integration_secret", "integration_connections", conn["id"], {}, company["id"])
    db.commit()
    return _render_integrations(company, revealed={"id": conn["id"], "secret": secret,
                                                   "url": url_for("api_v1.integration_webhook",
                                                                  hook_key=conn["hook_key"], _external=True)})


@bp.route("/integrations/connections/<int:connection_id>/toggle", methods=["POST"])
@require("manage_integrations")
def integration_toggle(connection_id):
    company = require_company()
    conn = _connection(company, connection_id)
    db.execute("UPDATE integration_connections SET active = ? WHERE id = ?", (0 if conn["active"] else 1, conn["id"]))
    audit("toggle_integration", "integration_connections", conn["id"], {"active": not conn["active"]}, company["id"])
    db.commit()
    flash(f"{conn['name']} {'paused' if conn['active'] else 'resumed'}.", "success")
    return redirect(url_for("wf.integrations_page"))


def _parse_field_map(text: str) -> dict:
    if not text.strip():
        return {}
    try:
        fm = json.loads(text)
    except ValueError:
        raise ValueError("The field map must be valid JSON.")
    if not isinstance(fm, dict) or set(fm) - {"fields", "status_map", "roots"}:
        raise ValueError('The field map is an object with any of "fields", "status_map", "roots".')
    if not isinstance(fm.get("fields", {}), dict) or not isinstance(fm.get("status_map", {}), dict):
        raise ValueError('"fields" and "status_map" must be objects.')
    if "roots" in fm and not (isinstance(fm["roots"], list) and all(isinstance(r, str) for r in fm["roots"])):
        raise ValueError('"roots" must be a list of paths.')
    return fm


@bp.route("/integrations/connections/<int:connection_id>/field-map", methods=["POST"])
@require("manage_integrations")
def integration_field_map(connection_id):
    company = require_company()
    conn = _connection(company, connection_id)
    try:
        fm = _parse_field_map(request.form.get("field_map") or "")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("wf.integrations_page"))
    db.execute("UPDATE integration_connections SET field_map = ? WHERE id = ?", (db.dumps(fm), conn["id"]))
    audit("update_integration_field_map", "integration_connections", conn["id"], {"field_map": fm}, company["id"])
    db.commit()
    flash("Field map saved. It applies to the next delivery.", "success")
    return redirect(url_for("wf.integrations_page"))


@bp.route("/integrations/connections/<int:connection_id>/test", methods=["POST"])
@require("manage_integrations")
def integration_test(connection_id):
    """Builds a sample payload in the provider's shape, signs it with the connection
    secret and runs it through exactly the same verification and ingest path as a
    real delivery. The sample contains a fake candidate name to show it isn't stored."""
    company = require_company()
    conn = _connection(company, connection_id)
    status = request.form.get("status") or "accepted"
    if status not in ("sent", "accepted", "declined"):
        abort(400, description="Unknown test status.")
    try:
        amount = float(request.form.get("amount_lpa") or 42)
    except ValueError:
        amount = 42.0
    payload = prov.sample_payload(conn["provider"], status=status, amount_lpa=amount,
                                  title=request.form.get("title") or "Senior Software Engineer",
                                  city=request.form.get("city") or "Bangalore",
                                  record_id=f"test-{int(time.time() * 1000)}")
    p = prov.provider(conn["provider"])
    body, headers = prov.sign(p, conn["secret"], payload)
    result = ingest.ingest_delivery(conn, body, headers, remote_addr="test-event")
    result.pop("http_status", None)
    if result.get("errors"):
        flash("Test event rejected: " + "; ".join(e["error"] for e in result["errors"]), "error")
    else:
        flash("Test event processed: " + ", ".join(f"{k} {v}" for k, v in result.items()
                                                   if isinstance(v, int) and v and k != "processed") + ".", "success")
    return redirect(url_for("wf.integrations_page"))


@bp.route("/integrations/preview", methods=["POST"])
@require("manage_integrations")
def integration_preview():
    company = require_company()
    key = request.form.get("provider")
    if key not in prov.PROVIDERS:
        abort(400, description="Unknown provider.")
    text = (request.form.get("payload") or "")[:200_000]
    try:
        payload = json.loads(text)
        overrides = _parse_field_map(request.form.get("field_map") or "")
        results = ingest.preview(key, payload, overrides, company)
        error = None
    except ValueError as exc:
        results, error = None, str(exc) if "field map" in str(exc).lower() else "The sample payload isn't valid JSON."
    return _render_integrations(company, preview={"provider": key, "payload": text, "results": results,
                                                  "error": error, "field_map": request.form.get("field_map") or ""})


@bp.route("/integrations/sample/<provider_key>.json")
@require("manage_integrations")
def integration_sample(provider_key):
    if provider_key not in prov.PROVIDERS:
        abort(404, description="Unknown provider.")
    return jsonify(prov.sample_payload(provider_key))
