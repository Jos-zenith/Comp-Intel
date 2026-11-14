"""
HRIS / ATS adapters: payload -> canonical intermediate records -> OfferEvent.

Each provider is declarative:
  root        where the record(s) live in the payload (first path that resolves;
              a list there means several records in one delivery)
  fields      canonical key -> candidate dotted paths (first non-empty wins)
  status_map  provider status -> sent | accepted | declined | withdrawn | ignored
  signature   how the delivery is authenticated (see verify())
  flatten     optional pre-step (Lever's `fields` array, Greenhouse custom fields)

IMPORTANT - read before relying on a mapping. Field paths and signature schemes are
written from each vendor's public documentation as understood when this was built.
None has been exercised against a live account or sandbox. Keka, Darwinbox,
greytHR and Zoho People don't publish a standard signed-webhook format for
compensation data at all; for those, CompIntel expects you to forward records
(from their APIs, a scheduled export, or a workflow webhook) signed with the
connection secret. That's why every connection can override any path or status
in Settings, and why /app/integrations has a dry-run preview: paste a real payload,
see exactly what it maps to, before turning a connection on.

Unified aggregators (Merge.dev, Finch) mean one integration instead of dozens.
Their payloads are mapped the same way; note that Merge's ATS Offer model carries
no compensation, so use its HRIS Employment model, and Finch amounts are in
minor units (paise), handled by amount_scale.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

GENERIC_SIG_HEADER = "X-CompIntel-Signature"   # "sha256=<hex HMAC of raw body>"
GENERIC_TOKEN_HEADER = "X-CompIntel-Token"     # fallback for tools that can only send a static header
MAX_SKEW_SECONDS = 300


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    category: str            # hris | ats | unified
    kind: str                # payroll | offer
    signature: str           # generic | greenhouse | merge | finch (all cover the full body)
    default_currency: str | None
    roots: tuple
    fields: dict
    status_map: dict = field(default_factory=dict)
    flatten: str | None = None
    amount_scale: float = 1.0
    note: str = ""


def _p(*paths):
    return list(paths)


PROVIDERS: dict[str, Provider] = {}


def _register(p: Provider):
    PROVIDERS[p.key] = p


# ---------------------------------------------------------------- Indian HRIS (active payroll)

_HRIS_STATUS = {"active": "active", "": "active", None: "active"}

_register(Provider(
    key="keka", name="Keka HR", category="hris", kind="payroll", signature="generic", default_currency="INR",
    roots=("data", ""),
    fields={"id": _p("employeeNumber", "employeeId", "id"), "title": _p("jobTitle.title", "jobTitle", "designation"),
            "level": _p("band", "grade", "jobLevel"), "location": _p("location.name", "workLocation", "location"),
            "fixed_amount": _p("salary.ctc", "annualCtc", "ctc"), "variable_amount": _p("salary.variablePay", "variablePay"),
            "timestamp": _p("updatedAt", "modifiedOn", "effectiveDate"), "joined_on": _p("joiningDate", "dateOfJoining"),
            "status": _p("employmentStatus")},
    status_map=_HRIS_STATUS, note="Forward employee salary records from Keka's API, signed with the connection secret."))

_register(Provider(
    key="darwinbox", name="Darwinbox", category="hris", kind="payroll", signature="generic", default_currency="INR",
    roots=("data", "employee", ""),
    fields={"id": _p("employee_id", "employee_no", "id"), "title": _p("designation", "designation_name"),
            "level": _p("grade", "band"), "location": _p("office_location", "location", "city"),
            "fixed_amount": _p("annual_ctc", "ctc"), "variable_amount": _p("variable_pay", "annual_variable"),
            "timestamp": _p("last_modified", "updated_on"), "joined_on": _p("date_of_joining", "doj"),
            "status": _p("employee_status")},
    status_map=_HRIS_STATUS, note="Darwinbox APIs are enabled per tenant; forward salary records signed with the secret."))

_register(Provider(
    key="greythr", name="greytHR", category="hris", kind="payroll", signature="generic", default_currency="INR",
    roots=("data", ""),
    fields={"id": _p("employeeNo", "employeeId", "id"), "title": _p("designation"), "level": _p("grade"),
            "location": _p("location", "branch"), "fixed_amount": _p("annualCTC", "ctc"),
            "variable_amount": _p("variablePay"), "timestamp": _p("lastModified", "updatedOn"),
            "joined_on": _p("dateOfJoin", "doj"), "status": _p("status")},
    status_map=_HRIS_STATUS, note="Export from greytHR's API or a scheduled report; sign with the connection secret."))

_register(Provider(
    key="zoho_people", name="Zoho People", category="hris", kind="payroll", signature="generic",
    default_currency="INR", roots=("response.result", "data", ""),
    fields={"id": _p("EmployeeID", "Employee_ID", "recordId"), "title": _p("Designation", "Title"),
            "level": _p("Grade", "Band"), "location": _p("Work_location", "LocationName", "Location"),
            "fixed_amount": _p("Annual_CTC", "CTC"), "variable_amount": _p("Variable_Pay"),
            "timestamp": _p("ModifiedTime", "Modified_Time"), "joined_on": _p("Dateofjoining", "Date_of_joining"),
            "status": _p("Employeestatus")},
    status_map=_HRIS_STATUS,
    note="Zoho workflow webhooks can only send a static header: use X-CompIntel-Token, or relay and sign."))

# ---------------------------------------------------------------- ATS (live offer letters)

_register(Provider(
    key="greenhouse", name="Greenhouse", category="ats", kind="offer", signature="greenhouse", default_currency=None,
    roots=("payload.offer", "payload"), flatten="greenhouse_custom_fields",
    fields={"id": _p("id"), "status": _p("status"),
            "title": _p("job.name", "cf.job_title", "cf.title"), "level": _p("cf.level", "cf.grade"),
            "location": _p("cf.location", "job.offices.0.location.name", "job.offices.0.name"),
            "fixed_amount": _p("cf.salary.amount", "cf.salary", "cf.base_salary.amount", "cf.fixed_ctc"),
            "currency": _p("cf.salary.unit", "cf.base_salary.unit", "cf.currency"),
            "variable_amount": _p("cf.variable_pay.amount", "cf.variable_pay", "cf.bonus.amount", "cf.bonus"),
            "esop_options": _p("cf.options", "cf.stock_options", "cf.esop"),
            "counter_amount": _p("cf.candidate_counter.amount", "cf.candidate_counter"),
            "decline_reason": _p("cf.decline_reason"),
            "timestamp": _p("resolved_at", "sent_at", "updated_at", "created_at")},
    status_map={"unresolved": "sent", "accepted": "accepted", "rejected": "declined", "deprecated": "withdrawn"},
    note="Webhook events offer_created / offer_updated, signed with the 'Signature: sha256 <hex>' header. "
         "Salary, level and location are custom fields: check their keys in the preview."))

_register(Provider(
    key="lever", name="Lever", category="ats", kind="offer", signature="generic", default_currency=None,
    roots=("data.offer", "data", ""), flatten="lever_fields",
    fields={"id": _p("id"), "status": _p("status"), "title": _p("f.job_title", "f.title", "posting.text"),
            "level": _p("f.level", "f.job_level"), "location": _p("f.location", "f.office"),
            "fixed_amount": _p("f.salary_amount", "f.compensation_amount"), "currency": _p("f.salary_currency"),
            "pay_period": _p("f.salary_interval"), "variable_amount": _p("f.bonus_amount", "f.variable_compensation"),
            "esop_options": _p("f.equity_amount", "f.stock_options"),
            "timestamp": _p("signedAt", "sentAt", "approvedAt", "createdAt")},
    status_map={"draft": "ignored", "approval-sent": "ignored", "approved": "ignored", "sent": "sent",
                "sent-manually": "sent", "opened": "sent", "denied": "declined", "signed": "accepted"},
    note="Lever's webhooks don't carry offer amounts, and their native signature covers only token + triggeredAt, "
         "not the body - so it can't vouch for the data. Have your forwarder fetch /opportunities/:id/offers and "
         "send the offer under data.offer, signed over the full body with this connection's secret."))

_register(Provider(
    key="freshteam", name="Freshteam", category="ats", kind="offer", signature="generic", default_currency="INR",
    roots=("offer", "data", ""),
    fields={"id": _p("id"), "status": _p("status"), "title": _p("job.title", "job_title", "designation"),
            "level": _p("level", "grade"), "location": _p("branch.city", "location", "job.branch.city"),
            "fixed_amount": _p("offered_salary", "ctc", "salary"), "currency": _p("currency"),
            "variable_amount": _p("variable_pay", "bonus"), "esop_options": _p("stock_options"),
            "timestamp": _p("updated_at", "created_at")},
    status_map={"offered": "sent", "sent": "sent", "pending": "sent", "accepted": "accepted", "declined": "declined",
                "rejected": "declined", "withdrawn": "withdrawn", "draft": "ignored"},
    note="Forward offers from Freshteam's API or workflow automator, signed with the connection secret."))

# ---------------------------------------------------------------- Unified APIs (one integration, many HRIS)

_register(Provider(
    key="merge", name="Merge.dev (unified HRIS)", category="unified", kind="payroll", signature="merge",
    default_currency=None, roots=("data", ""),
    fields={"id": _p("id", "remote_id"), "title": _p("job_title"), "level": _p("job_level", "pay_group.name"),
            "location": _p("work_location.city", "employee.work_location.city"),
            "fixed_amount": _p("pay_rate"), "pay_period": _p("pay_period"), "currency": _p("pay_currency"),
            "timestamp": _p("effective_date", "modified_at"), "joined_on": _p("employee.start_date", "start_date"),
            "status": _p("employment_status")},
    status_map={"ACTIVE": "active", "active": "active", "": "active", None: "active"},
    note="Subscribe to Employment.added / Employment.changed with work_location expanded. Merge's ATS Offer "
         "model has status only (no amounts), so offer prices come from the HRIS side."))

_register(Provider(
    key="finch", name="Finch (unified employment)", category="unified", kind="payroll", signature="finch",
    default_currency=None, roots=("data", ""), amount_scale=0.01,
    fields={"id": _p("id", "individual_id"), "title": _p("title"), "level": _p("class_code", "level"),
            "location": _p("location.city"), "fixed_amount": _p("income.amount"), "pay_period": _p("income.unit"),
            "currency": _p("income.currency"), "timestamp": _p("income.effective_date", "updated_at"),
            "joined_on": _p("start_date"), "status": _p("is_active")},
    status_map={True: "active", False: "ignored", "true": "active", "false": "ignored", None: "active"},
    note="Finch webhooks announce changes; fetch /employer/employment and forward the record. Amounts are in "
         "minor units (paise) - amount_scale 0.01."))


def provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise KeyError(f"Unknown provider {key!r}")


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------

def _hex_hmac(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(p: Provider, secret: str, body: bytes, headers, now: float | None = None) -> None:
    """Raises SignatureError unless the delivery is authentic. `headers` is any
    case-insensitive mapping (Flask's request.headers)."""
    now = now or time.time()
    scheme = p.signature
    if scheme == "generic":
        sent = headers.get(GENERIC_SIG_HEADER, "")
        if sent:
            expected = "sha256=" + _hex_hmac(secret, body)
            if hmac.compare_digest(sent.strip(), expected):
                return
            raise SignatureError("X-CompIntel-Signature doesn't match the body")
        token = headers.get(GENERIC_TOKEN_HEADER, "")
        if token and hmac.compare_digest(token, secret):
            return
        raise SignatureError("missing X-CompIntel-Signature (or X-CompIntel-Token)")
    if scheme == "greenhouse":
        sent = headers.get("Signature", "")
        expected = "sha256 " + _hex_hmac(secret, body)
        if sent and hmac.compare_digest(sent.strip(), expected):
            return
        raise SignatureError("Greenhouse 'Signature' header doesn't match")
    if scheme == "merge":
        sent = headers.get("X-Merge-Webhook-Signature", "")
        digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        expected = base64.urlsafe_b64encode(digest).decode()
        if sent and (hmac.compare_digest(sent.strip(), expected) or
                     hmac.compare_digest(sent.strip().rstrip("="), expected.rstrip("="))):
            return
        raise SignatureError("X-Merge-Webhook-Signature doesn't match")
    if scheme == "finch":
        msg_id, ts, sent = (headers.get("finch-event-id", ""), headers.get("finch-timestamp", ""),
                            headers.get("finch-signature", ""))
        if not (msg_id and ts and sent):
            raise SignatureError("missing finch-event-id / finch-timestamp / finch-signature")
        try:
            sent_at = float(ts)
        except ValueError:
            raise SignatureError("finch-timestamp isn't a number")
        if abs(now - sent_at) > MAX_SKEW_SECONDS:
            raise SignatureError("finch-timestamp is too old (possible replay)")
        key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        try:
            key_bytes = base64.b64decode(key)
        except (ValueError, TypeError):
            key_bytes = key.encode()
        expected = base64.b64encode(hmac.new(key_bytes, f"{msg_id}.{ts}.".encode() + body,
                                             hashlib.sha256).digest()).decode()
        for part in sent.split():
            version, _, value = part.partition(",")
            if version == "v1" and hmac.compare_digest(value, expected):
                return
        raise SignatureError("finch-signature doesn't match")
    raise SignatureError(f"unknown signature scheme {scheme!r}")


def sign(p: Provider, secret: str, payload: dict, now: float | None = None) -> tuple[bytes, dict]:
    """Builds a correctly signed delivery for `payload` - used by 'Send test event'
    and the tests, so the test path exercises the same verification as production."""
    now = now or time.time()
    body = json.dumps(payload).encode()
    if p.signature == "generic":
        return body, {GENERIC_SIG_HEADER: "sha256=" + _hex_hmac(secret, body)}
    if p.signature == "greenhouse":
        return body, {"Signature": "sha256 " + _hex_hmac(secret, body)}
    if p.signature == "merge":
        digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        return body, {"X-Merge-Webhook-Signature": base64.urlsafe_b64encode(digest).decode()}
    if p.signature == "finch":
        key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        try:
            key_bytes = base64.b64decode(key)
        except (ValueError, TypeError):
            key_bytes = key.encode()
        msg_id, ts = "evt_test", str(int(now))
        sig = base64.b64encode(hmac.new(key_bytes, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
        return body, {"finch-event-id": msg_id, "finch-timestamp": ts, "finch-signature": f"v1,{sig}"}
    raise SignatureError(f"unknown signature scheme {p.signature!r}")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def get_path(obj, path: str):
    if path == "":
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _unwrap(value):
    """Custom-field wrappers like {"value": ...} / {"name": ..., "value": ...}."""
    while isinstance(value, dict) and "value" in value and len(value) <= 4 and "amount" not in value:
        value = value["value"]
    return value


def _flatten(kind: str | None, rec: dict) -> dict:
    rec = dict(rec)
    if kind == "lever_fields" and isinstance(rec.get("fields"), list):
        rec["f"] = {f.get("identifier") or f.get("text"): f.get("value") for f in rec["fields"] if isinstance(f, dict)}
    elif kind == "greenhouse_custom_fields":
        cf = rec.get("custom_fields") or {}
        if isinstance(cf, list):  # keyed_custom_fields style list
            cf = {c.get("name_key") or c.get("name"): c.get("value") for c in cf if isinstance(c, dict)}
        rec["cf"] = {k: _unwrap(v) for k, v in cf.items()}
        if isinstance(rec.get("keyed_custom_fields"), dict):
            rec["cf"].update({k: _unwrap(v) for k, v in rec["keyed_custom_fields"].items()})
    return rec


def map_status(status_map: dict, value):
    """Provider status -> our status; None when unmapped (normalize rejects it)."""
    if isinstance(value, (dict, list)):
        return None
    if value in status_map:
        return status_map[value]
    if isinstance(value, str):
        for candidate in (value.strip().lower(), value.strip()):
            if candidate in status_map:
                return status_map[candidate]
    return None


def merged_fields(p: Provider, overrides: dict | None) -> tuple[tuple, dict, dict]:
    """A connection's field-map override replaces paths per key; status_map entries
    are added on top; roots replace the provider's roots."""
    overrides = overrides or {}
    fields = {k: list(v) for k, v in p.fields.items()}
    for k, v in (overrides.get("fields") or {}).items():
        fields[k] = [v] if isinstance(v, str) else list(v)
    status_map = {**p.status_map, **(overrides.get("status_map") or {})}
    roots = tuple(overrides["roots"]) if overrides.get("roots") else p.roots
    return roots, fields, status_map


def extract(p: Provider, payload, overrides: dict | None = None) -> list[dict]:
    """Payload -> list of canonical intermediate dicts (see schema.normalize)."""
    roots, fields, status_map = merged_fields(p, overrides)
    found = None
    for root in roots:
        node = get_path(payload, root)
        if node not in (None, {}, []):
            found = node
            break
    if found is None:
        return []
    records = found if isinstance(found, list) else [found]
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec = _flatten(p.flatten, rec)
        raw = {}
        for key, paths in fields.items():
            for path in paths:
                value = _unwrap(get_path(rec, path))
                if value not in (None, ""):
                    raw[key] = value
                    break
        if isinstance(raw.get("fixed_amount"), dict):  # {"amount": .., "unit"/"currency": ..}
            money = raw["fixed_amount"]
            raw["fixed_amount"] = money.get("amount")
            raw.setdefault("currency", money.get("unit") or money.get("currency"))
        raw["status_raw"] = raw.get("status")
        raw["status"] = map_status(status_map, raw["status_raw"])
        raw["amount_scale"] = p.amount_scale
        out.append(raw)
    return out


# --------------------------------------------------------------------------
# Sample payloads (for "Send test event" and the tests)
# --------------------------------------------------------------------------

def sample_payload(key: str, *, status: str = "accepted", amount_lpa: float = 42.0, title="Senior Software Engineer",
                   city="Bangalore", record_id="rec-1001", joined_days_ago: int = 10) -> dict:
    from datetime import date, timedelta
    rupees = int(round(amount_lpa * 1e5))
    today = date.today()
    joined = (today - timedelta(days=joined_days_ago)).isoformat()
    if key == "greenhouse":
        gh = {"accepted": "accepted", "declined": "rejected", "sent": "unresolved", "withdrawn": "deprecated"}[status]
        return {"action": "offer_updated", "payload": {"offer": {
            "id": record_id, "status": gh, "updated_at": f"{today.isoformat()}T10:00:00Z",
            "job": {"name": title, "offices": [{"location": {"name": city}}]},
            "custom_fields": {"salary": {"name": "Salary", "type": "currency",
                                         "value": {"amount": str(rupees), "unit": "INR"}},
                              "options": {"value": 9000}},
            "candidate": {"first_name": "Should", "last_name": "NotBeStored", "email": "candidate@example.com"}}}}
    if key == "lever":
        lv = {"accepted": "signed", "declined": "denied", "sent": "sent", "withdrawn": "denied"}[status]
        return {"event": "offerUpdated", "data": {"offer": {
            "id": record_id, "status": lv, "createdAt": int(time.time() * 1000),
            "fields": [{"identifier": "job_title", "value": title}, {"identifier": "location", "value": city},
                       {"identifier": "salary_amount", "value": rupees}, {"identifier": "salary_currency", "value": "INR"},
                       {"identifier": "salary_interval", "value": "per-year-salary"},
                       {"identifier": "candidate_name", "value": "Should Not Be Stored"}]}}}
    if key == "freshteam":
        return {"offer": {"id": record_id, "status": status, "job": {"title": title}, "location": city,
                          "offered_salary": rupees, "currency": "INR", "updated_at": today.isoformat(),
                          "candidate": {"name": "Should Not Be Stored"}}}
    if key == "merge":
        return {"hook": {"event": "Employment.changed"}, "linked_account": {"integration": "Keka"},
                "data": {"id": record_id, "job_title": title, "pay_rate": rupees, "pay_period": "YEAR",
                         "pay_currency": "INR", "effective_date": today.isoformat(),
                         "employee": {"start_date": joined, "work_location": {"city": city},
                                      "first_name": "Should", "last_name": "NotBeStored"},
                         "employment_status": "ACTIVE"}}
    if key == "finch":
        return {"data": {"id": record_id, "title": title, "location": {"city": city},
                         "income": {"unit": "yearly", "amount": rupees * 100, "currency": "inr",
                                    "effective_date": today.isoformat()},
                         "start_date": joined, "is_active": True, "first_name": "Should"}}
    # Indian HRIS: generic signed forwarders with the provider's field names
    base = {
        "keka": {"employeeNumber": record_id, "jobTitle": {"title": title}, "location": {"name": city},
                 "salary": {"ctc": rupees}, "updatedAt": today.isoformat(), "joiningDate": joined,
                 "displayName": "Should Not Be Stored"},
        "darwinbox": {"employee_id": record_id, "designation": title, "office_location": city, "annual_ctc": rupees,
                      "last_modified": today.isoformat(), "date_of_joining": joined, "full_name": "Should Not Be Stored"},
        "greythr": {"employeeNo": record_id, "designation": title, "location": city, "annualCTC": rupees,
                    "lastModified": today.isoformat(), "dateOfJoin": joined, "name": "Should Not Be Stored"},
        "zoho_people": {"EmployeeID": record_id, "Designation": title, "Work_location": city, "Annual_CTC": rupees,
                        "ModifiedTime": today.isoformat(), "Dateofjoining": joined, "FirstName": "Should"},
    }
    if key not in base:
        raise KeyError(key)
    return {"data": base[key]}
