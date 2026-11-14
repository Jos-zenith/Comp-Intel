"""
Machine-facing API, mounted at /api/v1. No session, no CSRF: each endpoint either
holds no state (CTC validation) or authenticates the caller itself (webhooks are
signed per connection).

  POST /api/v1/ctc/validate                  stateless CTC structure + statutory checks
  GET  /api/v1/ctc/rulesets                  the versioned rulesets the engine knows
  POST /api/v1/integrations/hooks/<hook_key> HRIS/ATS deliveries (see workflow/integrations)
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import ctc_engine
from saas_domain import normalize_city

from . import db
from .auth import demo_mode_enabled
from .integrations import ingest

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

MAX_WEBHOOK_BYTES = 1_000_000


@bp.route("/ctc/validate", methods=["POST"])
def ctc_validate():
    """Validate a cash CTC (and optional variable / explicit components) against the
    ruleset in force - fast enough to call on every slider move. JSON body; form
    fields are accepted too, with components as components.basic_lpa etc."""
    payload = request.get_json(silent=True)
    if payload is None:
        form = request.form.to_dict()
        components = {k.split(".", 1)[1]: v for k, v in form.items() if k.startswith("components.") and v != ""}
        payload = {k: v for k, v in form.items() if not k.startswith("components.")}
        if components:
            payload["components"] = components
    if not isinstance(payload, dict):
        return jsonify(error="Send a JSON object."), 400
    try:
        return jsonify(ctc_engine.validate(payload, normalize_city=normalize_city))
    except ctc_engine.ValidationInputError as exc:
        return jsonify(error=str(exc)), 400
    except ctc_engine.RulesetError as exc:
        return jsonify(error=str(exc)), 422


@bp.route("/ctc/rulesets")
def ctc_rulesets():
    current = ctc_engine.active()
    return jsonify(active=current.meta(), rulesets=[
        {**r.meta(), "effective_to": r.effective_to.isoformat() if r.effective_to else None,
         "rules": [{"id": x["id"], "type": x["type"], "category": x["category"], "on_fail": x["on_fail"],
                    "statute": bool(x.get("statute")), "params": x.get("params", {})} for x in r.rules],
         "structure": r.structure}
        for r in ctc_engine.load()])


@bp.route("/integrations/hooks/<hook_key>", methods=["POST"])
def integration_webhook(hook_key):
    # Connections and the outcomes they write live in the workflow, which only runs
    # in demo mode until real sign-in exists; the receiver follows the same switch.
    if not demo_mode_enabled():
        return jsonify(error="The offer workflow (and its integrations) is turned off on this server."), 503
    if (request.content_length or 0) > MAX_WEBHOOK_BYTES:
        return jsonify(error="Payload too large."), 413
    conn = db.query("SELECT * FROM integration_connections WHERE hook_key = ?", (hook_key,), one=True)
    if conn is None or not conn["active"]:
        return jsonify(error="Unknown or inactive integration."), 404
    body = request.get_data(cache=False)
    if len(body) > MAX_WEBHOOK_BYTES:
        return jsonify(error="Payload too large."), 413
    result = ingest.ingest_delivery(conn, body, request.headers, remote_addr=request.remote_addr or "")
    status = result.pop("http_status")
    return jsonify(result), status
