"""
Webhook / forwarded-record ingestion: verify -> extract -> normalise -> log -> upsert outcome.

  * Authenticity first: nothing is parsed until the provider's signature checks out.
    Failed signatures are not written to the database (that would let anyone fill
    it); the connection remembers the last error for the admin page.
  * Idempotent: a redelivered webhook (same body) is acknowledged and skipped.
    A later event for the same provider record (offer sent -> accepted) updates
    the same outcome via its source_ref instead of creating a second one, and a
    late "sent" never downgrades an accepted/declined outcome.
  * PII-free: raw payloads are never stored, only their SHA-256 and the
    normalised OfferEvent fields.
  * Consent-gated sharing is unchanged: the outcome goes through
    routes.record_observation(), which only copies it to the benchmark network
    when the workspace shares outcomes, and which queues the cohort for the
    streaming recompute.
"""
from __future__ import annotations

import hashlib
import json
import secrets

from flask import current_app

from .. import db
from ..auth import system_audit
from . import providers as prov
from .schema import NormalizationError, normalize

TERMINAL = ("accepted", "declined", "withdrawn")


def new_credentials() -> tuple[str, str]:
    """(hook_key for the URL, secret for signatures)."""
    return secrets.token_urlsafe(18), "whsec_" + secrets.token_urlsafe(32)


def source_ref(connection_id: int, provider_key: str, record_id) -> str:
    secret = current_app.secret_key or ""
    secret = secret if isinstance(secret, str) else secret.decode("latin-1")
    return hashlib.sha256(f"{secret}:{connection_id}:{provider_key}:{record_id}".encode()).hexdigest()[:24]


def _settings(company) -> dict:
    from ..routes import settings_for
    return settings_for(company)


def preview(provider_key: str, payload, overrides: dict | None, company) -> list[dict]:
    """Dry run for the admin page: what would each record become? Writes nothing."""
    p = prov.provider(provider_key)
    out = []
    raws = prov.extract(p, payload, overrides)
    if not raws:
        return [{"index": 0, "ok": False, "error": f"No record found at any of: {', '.join(repr(r) for r in p.roots)}"}]
    settings = _settings(company)
    for i, raw in enumerate(raws):
        shown = {k: v for k, v in raw.items() if k not in ("amount_scale",)}
        if raw.get("status") == "ignored":
            out.append({"index": i, "ok": True, "ignored": True, "raw": shown,
                        "note": f"Status {raw.get('status_raw')!r} isn't an offer decision yet; it would be skipped."})
            continue
        try:
            ev = normalize(raw, provider=p.key, kind=p.kind, default_currency=p.default_currency,
                           source_ref="(preview)", company_stage=company["funding_stage"],
                           new_joiner_days=int(settings.get("payroll_new_joiner_days", 60)))
            out.append({"index": i, "ok": True, "event": ev.as_dict(), "raw": shown})
        except NormalizationError as exc:
            out.append({"index": i, "ok": False, "error": str(exc), "raw": shown})
    return out


def _log(conn_row, company_id, sha, idx, disposition, ev=None, error=None, outcome_id=None) -> int:
    fields = dict(connection_id=conn_row["id"], company_id=company_id, provider=conn_row["provider"],
                  received_at=db.now_iso(), payload_sha256=sha, record_index=idx, disposition=disposition,
                  error=error, outcome_id=outcome_id)
    if ev is not None:
        fields.update(source_ref=ev.source_ref, schema_version=ev.schema_version, kind=ev.kind, status=ev.status,
                      role=ev.role, level=ev.level, city=ev.city, city_tier=ev.city_tier,
                      fixed_ctc_lpa=ev.fixed_ctc_lpa, variable_ctc_lpa=ev.variable_ctc_lpa,
                      cash_ctc_lpa=ev.cash_ctc_lpa, esop_grant=ev.esop_grant, company_stage=ev.company_stage,
                      counter_cash_ctc_lpa=ev.counter_cash_ctc_lpa, occurred_at=ev.timestamp)
    cols = ", ".join(fields)
    marks = ", ".join(f":{k}" for k in fields)
    return db.execute(f"INSERT INTO offer_events ({cols}) VALUES ({marks})", fields)


def _decline_reason(text):
    from ..routes import DECLINE_REASONS
    if not text:
        return None
    for reason in DECLINE_REASONS:
        if reason.lower() == text.lower():
            return reason
    return "Other"


def apply_event(company, conn_row, ev) -> tuple[str, int | None]:
    """Upsert the workspace outcome for this provider record. Returns (disposition, outcome_id)."""
    from ..routes import current_profile, record_observation

    if ev.status == "active":
        return "incumbent_stored", None
    existing = db.query("SELECT * FROM outcomes WHERE company_id = ? AND source_ref = ?",
                        (company["id"], ev.source_ref), one=True)
    if existing is not None and existing["status"] in TERMINAL and ev.status == "sent":
        return "stale", existing["id"]
    day = ev.timestamp[:10]
    cash = ev.cash_ctc_lpa
    initial = (existing["initial_cash_ctc"] if existing is not None and existing["initial_cash_ctc"] is not None
               else cash)
    values = {
        "status": ev.status,
        "sent_date": (existing["sent_date"] if existing is not None and existing["sent_date"] else
                      (day if ev.status == "sent" else None)),
        "decision_date": day if ev.status in TERMINAL else None,
        "final_cash_ctc": cash if ev.status == "accepted" else None,
        "final_esop_options": ev.esop_grant if ev.status == "accepted" else None,
        "initial_cash_ctc": initial,
        "initial_esop_options": (existing["initial_esop_options"] if existing is not None
                                 and existing["initial_esop_options"] is not None else ev.esop_grant),
        "counter_cash_ctc": ev.counter_cash_ctc_lpa if ev.counter_cash_ctc_lpa is not None else
        (existing["counter_cash_ctc"] if existing is not None else None),
        "decline_reason": _decline_reason(ev.decline_reason) if ev.status == "declined" else None,
        "now": db.now_iso(),
    }
    if existing is not None:
        db.execute("UPDATE outcomes SET status=:status, sent_date=:sent_date, decision_date=:decision_date, "
                   "final_cash_ctc=:final_cash_ctc, final_esop_options=:final_esop_options, "
                   "initial_cash_ctc=:initial_cash_ctc, initial_esop_options=:initial_esop_options, "
                   "counter_cash_ctc=:counter_cash_ctc, decline_reason=:decline_reason, updated_at=:now WHERE id=:id",
                   {**values, "id": existing["id"]})
        oid, disposition = existing["id"], "updated"
    else:
        profile, _snap, _age = current_profile(company)
        oid = db.execute(
            "INSERT INTO outcomes (company_id, request_id, external, role, level, city, status, sent_date, decision_date, "
            "final_cash_ctc, final_esop_options, decline_reason, notes, created_by, created_at, updated_at, "
            "employment_type, health_band, initial_cash_ctc, initial_esop_options, counter_cash_ctc, source_ref) "
            "VALUES (?, NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'Full-time', ?, ?, ?, ?, ?)",
            (company["id"], ev.role, ev.level, ev.city, ev.status, values["sent_date"], values["decision_date"],
             values["final_cash_ctc"], values["final_esop_options"], values["decline_reason"],
             f"From {prov.provider(ev.provider).name} (integration #{conn_row['id']})", values["now"], values["now"],
             profile["health_band"], values["initial_cash_ctc"], values["initial_esop_options"],
             values["counter_cash_ctc"], ev.source_ref))
        disposition = "recorded"
    record_observation(company, oid)
    return disposition, oid


def ingest_delivery(conn_row, body: bytes, headers, *, remote_addr: str = "", skip_signature: bool = False) -> dict:
    """Processes one delivery. Returns a JSON-able result including `http_status`."""
    p = prov.provider(conn_row["provider"])
    company = db.query("SELECT * FROM companies WHERE id = ?", (conn_row["company_id"],), one=True)
    if company is None:
        return {"http_status": 409, "error": "The workspace this connection belongs to no longer exists."}
    sha = hashlib.sha256(body).hexdigest()
    if not skip_signature:
        try:
            prov.verify(p, conn_row["secret"], body, headers)
        except prov.SignatureError as exc:
            db.execute("UPDATE integration_connections SET last_error = ?, last_error_at = ? WHERE id = ?",
                       (f"Signature rejected: {exc}", db.now_iso(), conn_row["id"]))
            db.commit()
            return {"http_status": 401, "error": f"Signature rejected: {exc}"}
    if db.query("SELECT id FROM offer_events WHERE connection_id = ? AND payload_sha256 = ? LIMIT 1",
                (conn_row["id"], sha), one=True):
        return {"http_status": 200, "duplicate": True, "message": "Already processed this exact delivery."}
    try:
        payload = json.loads(body)
    except ValueError:
        _log(conn_row, company["id"], sha, 0, "rejected", error="Body isn't valid JSON.")
        db.commit()
        return {"http_status": 400, "error": "Body isn't valid JSON."}

    overrides = db.loads(conn_row["field_map"], {})
    settings = _settings(company)
    results = {"recorded": 0, "updated": 0, "incumbent_stored": 0, "ignored": 0, "stale": 0, "rejected": 0}
    errors = []
    raws = prov.extract(p, payload, overrides)
    if not raws:
        msg = f"No record found at any of: {', '.join(repr(r) for r in prov.merged_fields(p, overrides)[0])}"
        _log(conn_row, company["id"], sha, 0, "rejected", error=msg)
        results["rejected"] += 1
        errors.append({"index": 0, "error": msg})
    for i, raw in enumerate(raws):
        if raw.get("status") == "ignored":
            _log(conn_row, company["id"], sha, i, "ignored",
                 error=f"Status {raw.get('status_raw')!r} isn't an offer decision")
            results["ignored"] += 1
            continue
        try:
            ev = normalize(raw, provider=p.key, kind=p.kind, default_currency=p.default_currency,
                           source_ref=source_ref(conn_row["id"], p.key, raw.get("id") or f"{sha}:{i}"),
                           company_stage=company["funding_stage"],
                           new_joiner_days=int(settings.get("payroll_new_joiner_days", 60)))
        except NormalizationError as exc:
            _log(conn_row, company["id"], sha, i, "rejected", error=str(exc))
            results["rejected"] += 1
            errors.append({"index": i, "error": str(exc)})
            continue
        disposition, oid = apply_event(company, conn_row, ev)
        _log(conn_row, company["id"], sha, i, disposition, ev=ev, outcome_id=oid)
        results[disposition] += 1
    db.execute("UPDATE integration_connections SET last_event_at = ? WHERE id = ?", (db.now_iso(), conn_row["id"]))
    system_audit("integration_delivery", "integration_connections", conn_row["id"],
                 {"provider": p.key, "payload_sha256": sha, **results}, company["id"],
                 {"source": "webhook", "ip": remote_addr})
    db.commit()
    return {"http_status": 200, "processed": len(raws), **results, "errors": errors}
