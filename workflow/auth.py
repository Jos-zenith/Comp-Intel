"""
Demo identity, role-based permissions, CSRF and audit logging.

There is no real login in the demo: a sidebar switcher picks one of the seeded
demo users and stores their id in the signed session. Every permission check
and audit entry still runs against that user, so the role model is exercised
exactly as it would be behind SSO.
"""
from __future__ import annotations

import hmac
import secrets
from functools import wraps

from flask import abort, g, request, session

from . import db

ROLES = {
    "recruiter": "Recruiter",
    "hiring_manager": "Hiring manager",
    "people_ops": "People Ops / HR",
    "finance": "Finance",
    "admin": "Admin",
    "exec": "Read-only executive",
}

# Approval steps always run in this order, whatever order the rules list them in.
APPROVER_ORDER = ["hiring_manager", "people_ops", "finance", "admin"]

PERMISSIONS = {
    "view_offers": {"recruiter", "hiring_manager", "people_ops", "finance", "admin", "exec"},
    "view_benchmark": {"recruiter", "hiring_manager", "people_ops", "finance", "admin"},
    "create_offer": {"recruiter", "people_ops", "admin"},
    "record_outcome": {"recruiter", "people_ops", "admin"},
    "export_benchmark": {"recruiter", "people_ops", "finance", "admin"},
    "manage_requisitions": {"recruiter", "hiring_manager", "people_ops", "admin"},
    "edit_company_profile": {"admin"},
    "edit_metrics": {"admin", "finance", "people_ops"},
    "request_metrics": {"recruiter", "hiring_manager"},
    "manage_bands": {"people_ops", "admin"},
    "manage_policy": {"people_ops", "admin"},
    "manage_users": {"admin"},
    "view_audit": {"admin"},
    "manage_retention": {"admin"},
    "manage_network_data": {"admin"},  # import / remove consented design-partner offer records
    "manage_integrations": {"admin"},  # HRIS/ATS connections, their secrets and delivery log
}


def current_user():
    if "compintel_user" in g:
        return g.compintel_user
    user = None
    uid = session.get("user_id")
    if uid is not None:
        user = db.query("SELECT * FROM users WHERE id = ? AND active = 1", (uid,), one=True)
    if user is None:
        user = db.query("SELECT * FROM users WHERE role = 'recruiter' AND active = 1 ORDER BY id", one=True)
        if user is not None:
            session["user_id"] = user["id"]
    g.compintel_user = user
    return user


def can(permission: str) -> bool:
    user = current_user()
    return user is not None and user["role"] in PERMISSIONS.get(permission, set())


def require(permission: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not can(permission):
                user = current_user()
                audit("permission_denied", "route", None, {"permission": permission, "path": request.path})
                db.commit()
                abort(403, description=f"{ROLES.get(user['role'], 'This role') if user else 'Anonymous'} "
                                       f"can't do this ({permission.replace('_', ' ')}).")
            return view(*args, **kwargs)
        return wrapped
    return decorator


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf_token"] = token
    return token


def verify_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not hmac.compare_digest(sent, session.get("csrf_token", "")):
        abort(400, description="Form expired or invalid (CSRF check failed). Reload the page and try again.")


def demo_mode_enabled() -> bool:
    """The user switcher stands in for real sign-in, so the workflow only runs
    when demo mode is explicitly on (COMPINTEL_DEMO_MODE=true, or `python main.py`
    for local development). A deployed server without it gets no /app at all."""
    from flask import current_app
    return bool(current_app.config.get("COMPINTEL_DEMO_MODE"))


def session_context() -> dict:
    """Who/where for the audit trail: client IP, user agent and a random
    per-browser-session id (not the cookie itself)."""
    if "sid" not in session:
        session["sid"] = secrets.token_hex(8)
    try:
        ctx = {"ip": request.remote_addr or "", "user_agent": (request.user_agent.string or "")[:160],
               "session": session["sid"]}
        if request.headers.get("X-Forwarded-For"):  # client-supplied; recorded, not trusted
            ctx["forwarded_for"] = request.headers["X-Forwarded-For"][:120]
        return ctx
    except RuntimeError:  # outside a request (e.g. seeding from a script)
        return {}


def system_audit(action: str, entity: str, entity_id, detail: dict | None, company_id, context: dict):
    """Audit entry for machine actors (HRIS/ATS webhooks): no user, no session, an
    explicit context instead. Same hash chain as audit()."""
    from .integrity import GENESIS, audit_entry_hash

    last = db.query("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1", one=True)
    row = {
        "ts": db.now_iso(), "company_id": company_id, "user_id": None, "user_role": "integration",
        "action": action, "entity": entity, "entity_id": entity_id, "detail": db.dumps(detail or {}),
        "context": db.dumps(context), "prev_hash": last["entry_hash"] if last else GENESIS,
    }
    row["entry_hash"] = audit_entry_hash(row["prev_hash"], row)
    db.execute(
        "INSERT INTO audit_log (ts, company_id, user_id, user_role, action, entity, entity_id, detail, context, "
        "prev_hash, entry_hash) VALUES (:ts, :company_id, :user_id, :user_role, :action, :entity, :entity_id, "
        ":detail, :context, :prev_hash, :entry_hash)", row)
    return row["entry_hash"]


def audit(action: str, entity: str, entity_id, detail: dict | None = None, company_id=None):
    """Append-only, hash-chained audit entry. Caller commits as part of its own
    transaction, so the entry and the change it records land together."""
    from .integrity import GENESIS, audit_entry_hash

    user = current_user()
    last = db.query("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1", one=True)
    row = {
        "ts": db.now_iso(), "company_id": company_id, "user_id": user["id"] if user else None,
        "user_role": user["role"] if user else None, "action": action, "entity": entity, "entity_id": entity_id,
        "detail": db.dumps(detail or {}), "context": db.dumps(session_context()),
        "prev_hash": last["entry_hash"] if last else GENESIS,
    }
    row["entry_hash"] = audit_entry_hash(row["prev_hash"], row)
    db.execute(
        "INSERT INTO audit_log (ts, company_id, user_id, user_role, action, entity, entity_id, detail, context, "
        "prev_hash, entry_hash) VALUES (:ts, :company_id, :user_id, :user_role, :action, :entity, :entity_id, "
        ":detail, :context, :prev_hash, :entry_hash)", row)
    return row["entry_hash"]
