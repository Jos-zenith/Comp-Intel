"""
Approval routing and state.

Rules are evaluated once, at submission, and the resulting route is frozen into
the submission row - so editing the rules later never changes what an
in-flight or historical offer needed. Steps run in APPROVER_ORDER, one role per
step, and an offer only reaches "approved" once every step has an approve
action from a user holding that step's role.
"""
from __future__ import annotations

from .auth import APPROVER_ORDER, ROLES

CONDITION_KINDS = {
    "always": "Always",
    "cash_above": "Cash CTC above (LPA)",
    "level_in": "Level is one of (comma-separated)",
    "role_family": "Role family is (Product / GTM)",
    "location_in": "City is one of (comma-separated)",
    "exception": "Check raised exception",
}

EXCEPTION_KINDS = {
    "market_high": "Above P75 market rate",
    "band": "Above internal band",
    "equity": "Above recent accepted offers",
    "budget": "Over budget / below runway floor / high one-time cash",
    "esop": "ESOP dilution over cap",
    "override": "Recommendation overridden",
}

DEFAULT_RULES = [
    ("Hiring manager sign-off", "always", "", "hiring_manager"),
    ("Senior and lead hires", "level_in", "Senior,Lead / Principal", "people_ops"),
    ("Outside internal band", "exception", "band", "people_ops"),
    ("Above accepted-offer parity", "exception", "equity", "people_ops"),
    ("Above P75", "exception", "market_high", "finance"),
    ("Over budget", "exception", "budget", "finance"),
    ("High cash offers", "cash_above", "40", "finance"),
    ("ESOP over dilution cap", "exception", "esop", "admin"),
]


def _split(value):
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


def rule_matches(rule, *, cash_ctc, level, role_family, city, exceptions) -> bool:
    kind, value = rule["condition_kind"], rule["condition_value"]
    if kind == "always":
        return True
    if kind == "cash_above":
        try:
            return cash_ctc > float(value)
        except (TypeError, ValueError):
            return False
    if kind == "level_in":
        return level.lower() in _split(value)
    if kind == "role_family":
        return (role_family or "").lower() == (value or "").strip().lower()
    if kind == "location_in":
        return city.lower() in _split(value)
    if kind == "exception":
        return value in exceptions
    return False


def compute_route(rules, **ctx) -> list[dict]:
    reasons: dict[str, list[str]] = {}
    for rule in rules:
        if rule["active"] and rule_matches(rule, **ctx):
            reasons.setdefault(rule["approver_role"], []).append(rule["name"])
    route = []
    for role in APPROVER_ORDER:
        if role in reasons:
            route.append({"step": len(route) + 1, "role": role, "role_label": ROLES[role], "reasons": reasons[role]})
    return route


def approval_state(route: list[dict], actions) -> dict:
    """Derives where a submission is from its frozen route + its action log.
    actions: rows with step, approver_role, action, ts (oldest first)."""
    done = {}
    for a in actions:
        if a["action"] in ("reject", "request_changes"):
            return {"state": "rejected" if a["action"] == "reject" else "changes_requested",
                    "current_step": None, "done": done, "final_action": dict(a)}
        if a["action"] == "approve" and a["step"] is not None:
            done[a["step"]] = dict(a)
    for step in route:
        if step["step"] not in done:
            return {"state": "pending", "current_step": step, "done": done, "final_action": None}
    return {"state": "approved", "current_step": None, "done": done, "final_action": None}
