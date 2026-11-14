"""
Offer-workflow views, mounted at /app.

Flow: company profile + metrics -> requisition -> offer request (benchmark is
generated and stored) -> package scenarios with live checks -> submission
(immutable snapshot + frozen approval route) -> approvals -> briefs -> outcome.
"""
from __future__ import annotations

import csv
import hashlib
import io
import statistics
from datetime import date, datetime, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request,
                   session, url_for)

import saas_metrics as sm
from saas_domain import (CITIES, EDUCATION_PEDIGREE, FUNDING_STAGES, LEVEL_BANDS, PRICING_MODELS,
                         ROLE_ARCHETYPES, SKILL_CATEGORIES, TARGET_MARKETS, role_archetype_for)

from . import affordability as afford
from . import db
from . import pilot
from . import streaming
from .approvals import CONDITION_KINDS, DEFAULT_RULES, EXCEPTION_KINDS, approval_state, compute_route
from .auth import (APPROVER_ORDER, PERMISSIONS, ROLES, audit, can, csrf_token, current_user, demo_mode_enabled,
                   require, session_context, verify_csrf)
from .benchmark import DATA_SOURCE_LABEL, WORKSPACE_MODES, build_benchmark
from .integrity import action_hash, verify_all
from .briefs import candidate_summary_context, candidate_summary_pdf, internal_brief_pdf
from .checks import DISCLAIMER, OVERRIDE_REASONS, RULESET, PackageError, parse_package, run_checks
from .company import (ARR_BANDS, HEALTH_SCORE_FORMULA, METRIC_FIELDS, MetricsError, compute_health_profile,
                      metrics_csv_template, parse_metric_inputs, parse_metrics_csv)

bp = Blueprint("wf", __name__, url_prefix="/app")

LEVELS = [b[2] for b in LEVEL_BANDS]
ALL_ROLES = [r for roles in ROLE_ARCHETYPES.values() for r in roles]
SCENARIO_LABELS = ["A", "B", "C"]
EDITABLE_STATUSES = {"draft", "changes_requested", "rejected"}
OUTCOME_STATUSES = {
    "sent": "Sent, awaiting decision",
    "accepted": "Accepted",
    "declined": "Declined by candidate",
    "withdrawn": "Withdrawn",
}
DECLINE_REASONS = ["Compensation (cash)", "Compensation (ESOP)", "Accepted competing offer", "Counter-offer from current employer",
                   "Role / level fit", "Location / work model", "Timing / start date", "Personal reasons", "Other"]
STATUS_LABELS = {
    "draft": "Draft", "pending_approval": "Pending approval", "changes_requested": "Changes requested",
    "rejected": "Rejected", "approved": "Approved", **OUTCOME_STATUSES,
}
EMPLOYMENT_TYPES = ["Full-time", "Fixed-term contract"]

DEFAULT_SETTINGS = {
    "target_percentile": 50,
    "min_cohort_size": 10,
    "min_internal_parity_n": 3,
    "metrics_stale_days": 180,
    "benchmark_stale_days": 90,
    "override_tolerance_pct": 5,
    "internal_equity_tolerance_pct": 15,
    "one_time_cap_pct": 20,
    "fully_diluted_shares": 10_000_000,
    "esop_pool_available": 600_000,
    "fmv_per_share_inr": 250,
    "max_dilution_per_hire_pct": 0.25,
    "default_vesting_years": 4,
    "default_cliff_months": 12,
    "inr_per_usd": sm.INR_PER_USD,
    "share_outcomes": True,
    "health_positioning": True,
    "retention_days": 730,
    # V2: affordability policy
    "runway_floor_months": 12,
    "max_runway_impact_months": 1.0,
    "hiring_plan_tolerance_pct": 15,
    # V2: observed-offer benchmark (pilot / production)
    "min_contributors": 3,
    "pilot_lookback_months": 24,
    # V2: ESOP scenarios (illustrative)
    "strike_price_inr": 10.0,
    "esop_exit_conservative_x": 0.5,
    "esop_exit_base_x": 2.0,
    "esop_exit_upside_x": 5.0,
    "esop_future_dilution_pct": 30.0,
    "esop_tax_rate_pct": 30.0,
    # V3: live market (streaming percentiles + clearing price)
    "live_window_days": 90,
    "live_recent_days": 30,
    "live_recent_weight": 2.0,
    "clearing_alert_decline_pct": 50,
    "clearing_close_rate_pct": 80,
    # V3: HRIS payroll records only count as offers for recent joiners
    "payroll_new_joiner_days": 60,
}
# key -> (label, type, min, max)
SETTING_FIELDS = {
    "target_percentile": ("Target percentile for recommendations (25-75)", float, 25, 75),
    "min_cohort_size": ("Minimum peer-cohort size before suppressing", int, 3, 1000),
    "min_internal_parity_n": ("Minimum accepted offers before internal-parity comparison", int, 2, 1000),
    "metrics_stale_days": ("Company metrics are stale after (days)", int, 30, 1000),
    "benchmark_stale_days": ("Benchmark data is stale after (days)", int, 7, 1000),
    "override_tolerance_pct": ("Allowed gap from recommendation without reason code (%)", float, 0, 50),
    "internal_equity_tolerance_pct": ("Internal-equity tolerance vs accepted offers (%)", float, 0, 100),
    "one_time_cap_pct": ("One-time cash cap before exception (% of cash CTC)", float, 0, 200),
    "fully_diluted_shares": ("Fully diluted shares", int, 1, 10**12),
    "esop_pool_available": ("ESOP options still available in pool", int, 0, 10**12),
    "fmv_per_share_inr": ("Current FMV per share (Rs.)", float, 0, 10**7),
    "max_dilution_per_hire_pct": ("Max dilution per hire (% fully diluted)", float, 0, 100),
    "default_vesting_years": ("Default vesting (years)", float, 1, 10),
    "default_cliff_months": ("Default cliff (months)", int, 0, 24),
    "inr_per_usd": ("Rs. per USD (only for the ARR/employee benchmark)", float, 1, 1000),
    "retention_days": ("Keep candidate names for (days) after close", int, 30, 3650),
    "runway_floor_months": ("Runway floor - offers that take runway below this need Finance (months)", float, 0, 120),
    "max_runway_impact_months": ("Flag a single offer that cuts runway by more than (months)", float, 0, 24),
    "hiring_plan_tolerance_pct": ("Hiring-plan tolerance vs average budget per planned hire (%)", float, 0, 500),
    "min_contributors": ("Pilot/production: minimum contributing companies per cohort", int, 2, 1000),
    "pilot_lookback_months": ("Pilot/production: ignore observed offers older than (months)", int, 3, 120),
    "strike_price_inr": ("Default ESOP strike price (Rs./share)", float, 0, 10**7),
    "esop_exit_conservative_x": ("ESOP Conservative exit (x current FMV)", float, 0, 1000),
    "esop_exit_base_x": ("ESOP Base exit (x current FMV)", float, 0, 1000),
    "esop_exit_upside_x": ("ESOP Upside exit (x current FMV)", float, 0, 1000),
    "esop_future_dilution_pct": ("ESOP: dilution from future rounds before exit (%)", float, 0, 95),
    "esop_tax_rate_pct": ("ESOP: illustrative flat tax on gains (%)", float, 0, 60),
    "live_window_days": ("Live market: rolling window (days)", int, 30, 365),
    "live_recent_days": ("Live market: offers newer than this get extra weight (days)", int, 0, 180),
    "live_recent_weight": ("Live market: weight of those recent offers (x)", float, 1, 10),
    "clearing_alert_decline_pct": ("Market alert when P50 offers are declined at least (%)", float, 10, 100),
    "clearing_close_rate_pct": ("Close-rate price: acceptance probability to price for (%)", float, 50, 99),
    "payroll_new_joiner_days": ("HRIS payroll: count as an accepted offer if joined within (days)", int, 0, 365),
}

_cohorts = None
_predict_fn = None
_model_version = "unknown"


def configure(predict_fn, cohorts, model_version):
    global _predict_fn, _cohorts, _model_version
    _predict_fn, _cohorts, _model_version = predict_fn, cohorts, model_version


# --------------------------------------------------------------------------
# Request hooks & template helpers
# --------------------------------------------------------------------------

@bp.before_request
def _before():
    if not demo_mode_enabled():
        return render_template("workflow/error.html", code=503, message=(
            "The offer workflow is turned off on this server. It only has a demo sign-in (a user switcher), so it "
            "runs only when COMPINTEL_DEMO_MODE=true is set explicitly. Real SSO isn't built yet.")), 503
    verify_csrf()


@bp.app_context_processor
def _inject():
    if not (request.endpoint or "").startswith("wf.") and request.endpoint not in _SHELL_ENDPOINTS:
        return {}
    if not demo_mode_enabled():
        return {"wf_user": None, "wf_can": None, "wf_pending_count": 0}
    user = current_user()
    pending = 0
    if user is not None and user["role"] in APPROVER_ORDER:
        pending = len(_pending_for_role(user["role"]))
    return {
        "wf_user": user,
        "wf_users": db.query("SELECT * FROM users WHERE active = 1 ORDER BY id"),
        "wf_roles": ROLES,
        "wf_can": can,
        "wf_pending_count": pending,
        "csrf_token": csrf_token,
        "status_labels": STATUS_LABELS,
        "demo_source": DATA_SOURCE_LABEL,
    }


# Existing tool pages share the app shell, so they also get the sidebar context.
_SHELL_ENDPOINTS = {"predict_form", "predict", "compare_page", "saas_metrics_page", "compliance_check_page", "about_tech"}


@bp.app_template_filter("lpa")
def _fmt_lpa(value, digits=2):
    if value is None:
        return "-"
    return f"Rs.{value:,.{digits}f} LPA"


@bp.app_template_filter("inr")
def indian_grouping(value) -> str:
    """12,34,567 rather than 1,234,567 (lakh/crore grouping)."""
    n = int(round(float(value)))
    sign, digits = ("-" if n < 0 else ""), str(abs(n))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups) + "," + tail


def _fmt_inr(value):
    if value is None:
        return "-"
    return f"Rs.{indian_grouping(value)}"


@bp.app_template_filter("indian")
def _fmt_indian(value):
    return "-" if value is None else indian_grouping(value)


@bp.app_template_filter("fromjson")
def _fromjson(value):
    try:
        return db.loads(value, {})
    except ValueError:
        return {}


@bp.app_template_filter("dt")
def _fmt_dt(value):
    if not value:
        return "-"
    return str(value)[:16].replace("T", " ")


@bp.errorhandler(403)
@bp.errorhandler(400)
@bp.errorhandler(404)
def _error(exc):
    return render_template("workflow/error.html", code=exc.code, message=exc.description), exc.code


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------

def get_company():
    return db.query("SELECT * FROM companies ORDER BY id LIMIT 1", one=True)


def require_company():
    company = get_company()
    if company is None:
        abort(redirect(url_for("wf.company")))
    return company


def settings_for(company) -> dict:
    stored = db.loads(company["settings"], {}) if company is not None else {}
    return {**DEFAULT_SETTINGS, **stored}


def latest_snapshot(company_id):
    return db.query("SELECT * FROM metric_snapshots WHERE company_id = ? ORDER BY id DESC LIMIT 1",
                    (company_id,), one=True)


def stage_default_health(stage: str) -> float:
    return float(current_app.config["STAGE_DEFAULT_HEALTH"].get(stage, 50.0))


def current_profile(company):
    """Health profile from the latest metrics snapshot, or a provisional one from
    the company profile's ARR band + headcount if no metrics were ever entered."""
    snap = latest_snapshot(company["id"])
    settings = settings_for(company)
    if snap is not None:
        profile = db.loads(snap["derived"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["created_at"])).days
        return profile, snap, age
    provisional = compute_health_profile(
        {"arr_band": company["arr_band"], "headcount": company["employee_count"]},
        company["funding_stage"], stage_default_health(company["funding_stage"]), float(settings["inr_per_usd"]))
    return provisional, None, None


def band_for(company_id, role, level, city):
    return db.query("SELECT * FROM bands WHERE company_id = ? AND role = ? AND level = ? AND city_tier = ?",
                    (company_id, role, level, CITIES[city][0]), one=True)


def accepted_peer_stats(company, role, level, city, employment_type, exclude_request_id=None) -> dict:
    """Internal parity group, defined narrowly on purpose: accepted offers for the
    same role, level, city and employment type. Suppressed below the minimum count."""
    rows = db.query("SELECT final_cash_ctc FROM outcomes WHERE company_id = ? AND role = ? AND level = ? AND city = ? "
                    "AND employment_type = ? AND status = 'accepted' AND final_cash_ctc IS NOT NULL "
                    "AND (request_id IS NULL OR request_id != ?)",
                    (company["id"], role, level, city, employment_type, exclude_request_id or -1))
    values = [r["final_cash_ctc"] for r in rows]
    min_n = int(settings_for(company)["min_internal_parity_n"])
    return {
        "definition": f"{role} · {level} · {city} · {employment_type}",
        "n": len(values), "min_n": min_n, "suppressed": len(values) < min_n,
        "median": round(statistics.median(values), 2) if len(values) >= min_n else None,
    }


def offer_ctx(req) -> dict:
    return {k: req[k] for k in ("role", "level", "city", "years_experience", "skill_category",
                                "education_pedigree", "expected_ctc", "competing_offer", "employment_type")}


def generate_benchmark(company, req_fields: dict):
    """Demo workspaces use the synthetic model; pilot and production use observed,
    consented offers only (workflow/pilot.py). Either way the company's affordability
    position is frozen into the benchmark so runway checks are reproducible."""
    profile, snap, age = current_profile(company)
    settings = settings_for(company)
    affordability = afford.context_from_inputs(db.loads(snap["inputs"], {}) if snap else {})
    common = dict(
        company=company, profile=profile, offer=req_fields, settings=settings,
        band=band_for(company["id"], req_fields["role"], req_fields["level"], req_fields["city"]),
        accepted_peer=accepted_peer_stats(company, req_fields["role"], req_fields["level"], req_fields["city"],
                                          req_fields.get("employment_type", "Full-time")),
        metrics_age_days=age, mode=company["mode"], affordability_missing=affordability["missing_runway"],
    )
    if company["mode"] == "demo":
        bm = build_benchmark(_predict_fn, _cohorts, model_version=_model_version, **common)
    else:
        bm = pilot.build_observed_benchmark(**common)
    bm["affordability"] = affordability
    bm["metrics_snapshot_id"] = snap["id"] if snap else None
    bm["metrics_age_days"] = age
    return bm, snap


def get_request(request_id):
    company = get_company()
    req = db.query("SELECT * FROM offer_requests WHERE id = ? AND company_id = ?",
                   (request_id, company["id"] if company else -1), one=True)
    if req is None:
        abort(404, description="Offer not found.")
    user = current_user()
    if user["role"] == "exec" and req["status"] not in ("approved", *OUTCOME_STATUSES):
        abort(403, description="Read-only executives can view approved offers only.")
    return req


def market_for_offer(company, role, level, city) -> dict | None:
    """Live clearing-price summary for an offer's cohort, frozen into the checks
    (and so into the submission snapshot) when the offer is evaluated."""
    if city not in CITIES:
        return None
    snap = streaming.compute_snapshot(streaming.cohort_key_for_city(role, level, city, company["funding_stage"]),
                                      settings_for(company))
    c = snap["clearing"]
    return {"cohort": snap["label"], "window_days": snap["window_days"], "computed_at": snap["computed_at"],
            "p50": snap["p50"], "status": c["status"], "n_decided": c["n_decided"],
            "clearing_price_lpa": c["clearing_price_lpa"], "close_price_lpa": c["close_price_lpa"],
            "close_rate_pct": c["close_rate_pct"], "decline_at_p50_pct": c["decline_at_p50_pct"],
            "alert": c["alert"], "message": c.get("message")}


def evaluate(company, req, pkg, bm=None):
    bm = bm or load_bm(req["benchmark"])
    requisition = db.query("SELECT * FROM requisitions WHERE id = ?", (req["requisition_id"],), one=True) \
        if req["requisition_id"] else None
    return run_checks(pkg, bm, offer=offer_ctx(req), requisition=requisition, settings=settings_for(company),
                      metrics_age_days=bm.get("metrics_age_days"),
                      accepted_peer=accepted_peer_stats(company, req["role"], req["level"], req["city"],
                                                        req["employment_type"], req["id"]),
                      market=market_for_offer(company, req["role"], req["level"], req["city"]))


def latest_submission(request_id):
    return db.query("SELECT * FROM submissions WHERE request_id = ? ORDER BY revision DESC LIMIT 1",
                    (request_id,), one=True)


def submission_actions(submission_id):
    return db.query("SELECT a.*, u.name AS user_name FROM approval_actions a JOIN users u ON u.id = a.user_id "
                    "WHERE submission_id = ? ORDER BY a.id", (submission_id,))


def submission_state(sub):
    return approval_state(db.loads(sub["route"]), submission_actions(sub["id"]))


def _pending_for_role(role):
    out = []
    for req in db.query("SELECT * FROM offer_requests WHERE status = 'pending_approval' ORDER BY updated_at"):
        sub = latest_submission(req["id"])
        if sub is None:
            continue
        state = submission_state(sub)
        if state["state"] == "pending" and state["current_step"]["role"] == role:
            out.append((req, sub, state))
    return out


def notify(company_id, role, message, link):
    db.execute("INSERT INTO notifications (company_id, role, message, link, created_at) VALUES (?, ?, ?, ?, ?)",
               (company_id, role, message, link, db.now_iso()))


def default_package(bm, settings, req=None, requisition=None) -> dict:
    """Prefill. Uses the target when one is shown; otherwise (low/insufficient
    confidence) the candidate's expectation or the budget, so nothing pretends
    to be a recommendation."""
    target = bm["target"] if bm["display"]["show_target"] else None
    if target is None:
        target = (req["expected_ctc"] if req is not None and req["expected_ctc"] else None) \
            or (requisition["budget_max_lpa"] if requisition is not None and requisition["budget_max_lpa"] else None) \
            or (bm["display"]["lo"] if bm["display"].get("lo") else 10.0)
    variable = round(target * 0.10, 2)
    return {
        "fixed_pay": round(target - variable, 2), "variable_pay": variable, "cash_ctc": round(target, 2),
        "esop_options": bm["esop"].get("options_mid", 0), "vesting_years": float(settings["default_vesting_years"]),
        "cliff_months": int(settings["default_cliff_months"]), "strike_price_inr": float(settings["strike_price_inr"]),
        "joining_bonus": 0.0, "relocation": 0.0,
        "notice_buyout": 0.0, "other_one_time": 0.0, "override_reason": "", "override_note": "",
        "other_one_time_note": "", "notes": "", "low_confidence_ack": False,
    }


def _float_or_none(value, label, lo=0, hi=10000):
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except ValueError:
        raise ValueError(f"{label} must be a number.")
    if not (lo <= v <= hi):
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}.")
    return v


def load_bm(text_or_dict) -> dict:
    """Benchmarks stored before confidence-driven display existed are treated as
    full-display demo benchmarks, which is how they were shown at the time."""
    bm = db.loads(text_or_dict) if isinstance(text_or_dict, str) else text_or_dict
    if "display" not in bm:
        from .benchmark import display_for
        bm["display"] = display_for("High", bm.get("p25"), bm.get("p75"))
        bm["display"].update(mode="full", lo=bm.get("p25"), hi=bm.get("p75"))
    bm.setdefault("data_mode", "demo")
    bm.setdefault("positioning", None)  # benchmarks from before health positioning existed
    bm.setdefault("affordability", None)  # benchmarks from before runway impact existed
    if "accepted_peer" not in bm:
        bm["accepted_peer"] = None
    return bm


def contributor_key(company_id) -> str:
    secret = current_app.secret_key or ""
    secret = secret if isinstance(secret, str) else secret.decode("latin-1")
    return hashlib.sha256(f"{secret}:{company_id}".encode()).hexdigest()[:12]


def record_observation(company, outcome_id):
    """Anonymised copy for the give-to-get data network - only with consent. Queues
    the cohort for the live recompute (workflow/streaming.py) once the request commits."""
    if not settings_for(company)["share_outcomes"]:
        return False
    o = db.query("SELECT * FROM outcomes WHERE id = ?", (outcome_id,), one=True)
    if o["city"] not in CITIES:
        return False
    previous = db.query("SELECT * FROM benchmark_observations WHERE outcome_id = ?", (outcome_id,), one=True)
    if o["status"] not in ("accepted", "declined"):
        # e.g. an integration moved an accepted offer to withdrawn: it no longer
        # describes a decided offer, so stop it feeding benchmarks.
        if previous is not None:
            db.execute("DELETE FROM benchmark_observations WHERE outcome_id = ?", (outcome_id,))
            streaming.queue_observation(previous)
        return False
    profile, _snap, _age = current_profile(company)
    db.execute("DELETE FROM benchmark_observations WHERE outcome_id = ?", (outcome_id,))
    streaming.pipeline.queue(streaming.cohort_key_for_city(o["role"], o["level"], o["city"], company["funding_stage"]))
    if previous is not None:
        streaming.queue_observation(previous)
    # Company context is reduced to bands; no names, no company identity, city reduced to its tier.
    db.execute(
        "INSERT INTO benchmark_observations (outcome_id, source, consent_basis, contributor_key, role, level, city_tier, "
        "funding_stage, employment_type, employee_band, arr_band, ndr_band, health_band, status, initial_cash_ctc, "
        "initial_esop_options, counter_cash_ctc, counter_esop_options, final_cash_ctc, final_esop_options, "
        "decline_reason, competing_offer, observed_month) "
        "VALUES (?, 'tenant_outcome', 'Workspace data-sharing setting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        (outcome_id, contributor_key(company["id"]), o["role"], o["level"], CITIES[o["city"]][0],
         company["funding_stage"], o["employment_type"], pilot.employee_band(company["employee_count"]),
         company["arr_band"], pilot.ndr_band(profile.get("derived", {}).get("ndr_pct")), profile["health_band"],
         o["status"], o["initial_cash_ctc"], o["initial_esop_options"], o["counter_cash_ctc"],
         o["counter_esop_options"], o["final_cash_ctc"], o["final_esop_options"], o["decline_reason"],
         o["competing_offer"], (o["decision_date"] or db.now_iso())[:7]))
    return True


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

@bp.route("/switch-user", methods=["POST"])
def switch_user():
    user = db.query("SELECT * FROM users WHERE id = ? AND active = 1", (request.form.get("user_id", type=int),), one=True)
    if user is None:
        abort(400, description="Unknown user.")
    session["user_id"] = user["id"]
    from flask import g
    g.pop("compintel_user", None)
    audit("switch_user", "user", user["id"], {"role": user["role"]})
    db.commit()
    nxt = request.form.get("next") or ""
    safe = nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt
    return redirect(nxt if safe else url_for("wf.dashboard"))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@bp.route("/")
def dashboard():
    company = get_company()
    if company is None:
        return redirect(url_for("wf.company"))
    user = current_user()
    cid = company["id"]
    onboarding = onboarding_state(user["id"])
    db.commit()
    activation = activation_steps(user, company)
    status_filter = "" if user["role"] != "exec" else \
        "AND status IN ('approved','sent','accepted','declined','withdrawn')"
    recent = db.query(f"SELECT * FROM offer_requests WHERE company_id = ? {status_filter} "
                      f"ORDER BY updated_at DESC LIMIT 8", (cid,))
    notes = db.query("SELECT * FROM notifications WHERE company_id = ? AND role = ? ORDER BY id DESC LIMIT 6",
                     (cid, user["role"]))
    queue = _pending_for_role(user["role"]) if user["role"] in APPROVER_ORDER else []
    return render_template("workflow/dashboard.html", company=company, activation=activation, recent=recent,
                           notes=notes, queue=queue, kpis=_kpis(cid), profile=current_profile(company)[0],
                           onboarding=onboarding, ttv=time_to_first_benchmark(onboarding),
                           ttv_target=FIRST_BENCHMARK_TARGET_MINUTES, metrics_request=open_metrics_request(cid))


# --------------------------------------------------------------------------
# Onboarding: role-specific, recruiter-first, instrumented
# --------------------------------------------------------------------------

FIRST_BENCHMARK_TARGET_MINUTES = 10


def onboarding_state(user_id) -> dict:
    """The user's first-run record, created the first time they're seen."""
    db.execute("INSERT OR IGNORE INTO user_onboarding (user_id, first_seen_at) VALUES (?, ?)", (user_id, db.now_iso()))
    return dict(db.query("SELECT * FROM user_onboarding WHERE user_id = ?", (user_id,), one=True))


def mark_first_benchmark(user_id):
    onboarding_state(user_id)
    db.execute("UPDATE user_onboarding SET first_benchmark_at = ? WHERE user_id = ? AND first_benchmark_at IS NULL",
               (db.now_iso(), user_id))


def time_to_first_benchmark(onboarding) -> float | None:
    """Minutes from the user's first visit to their first benchmark."""
    if not onboarding.get("first_benchmark_at"):
        return None
    delta = datetime.fromisoformat(onboarding["first_benchmark_at"]) - datetime.fromisoformat(onboarding["first_seen_at"])
    return round(max(delta.total_seconds(), 0) / 60, 1)


def open_metrics_request(company_id):
    return db.query("SELECT m.*, u.name AS requester FROM metrics_requests m JOIN users u ON u.id = m.requested_by "
                    "WHERE m.company_id = ? AND m.fulfilled_at IS NULL ORDER BY m.id DESC LIMIT 1",
                    (company_id,), one=True)


def _count(sql, params) -> int:
    return db.query(sql, params, one=True)["c"]


def activation_steps(user, company) -> list[dict]:
    """Checklist for the signed-in user's role. A recruiter starts with past offers
    and a benchmark (no metrics needed) and asks Finance for metrics on the side;
    Finance and Admin start with the company data only they have."""
    cid, uid, role = company["id"], user["id"], user["role"]
    has_metrics = _count("SELECT COUNT(*) c FROM metric_snapshots WHERE company_id = ?", (cid,)) > 0
    metrics_asked = _count("SELECT COUNT(*) c FROM metrics_requests WHERE company_id = ?", (cid,)) > 0
    open_requests = _count("SELECT COUNT(*) c FROM metrics_requests WHERE company_id = ? AND fulfilled_at IS NULL",
                           (cid,))

    def step(label, done, link, hint="", action=None):
        return {"label": label, "done": bool(done), "link": link, "hint": hint, "action": action}

    if role == "recruiter":
        return [
            step("Import your past offers", _count("SELECT COUNT(*) c FROM outcomes WHERE company_id = ? AND external = 1",
                                                   (cid,)),
                 url_for("wf.outcome_external"), "Paste or upload a CSV of recent offers and whether they were accepted. "
                 "Seeds internal parity and your acceptance stats."),
            step("Get your first benchmark", _count("SELECT COUNT(*) c FROM offer_requests WHERE created_by = ?", (uid,)),
                 url_for("wf.offer_new"), f"Works now, without company metrics. Target: under "
                 f"{FIRST_BENCHMARK_TARGET_MINUTES} minutes."),
            step("Ask Finance for company metrics", has_metrics or metrics_asked, url_for("wf.company"),
                 "Metrics only fine-tune where you aim in the range. One click sends Finance a request.",
                 action=None if (has_metrics or metrics_asked) else "request_metrics"),
            step("Submit your first offer", _count("SELECT COUNT(*) c FROM submissions WHERE submitted_by = ?", (uid,)),
                 url_for("wf.offers")),
            step("Record your first outcome", _count("SELECT COUNT(*) c FROM outcomes WHERE created_by = ? AND "
                                                     "request_id IS NOT NULL AND status != 'sent'", (uid,)),
                 url_for("wf.offers"), "Accepted or declined - this is what calibrates the benchmark."),
        ]
    if role == "finance":
        return [
            step("Enter company metrics", has_metrics, url_for("wf.company"),
                 "ARR, NDR, churn, CAC, margin. Form or CSV; blanks are fine."),
            step("Answer metrics requests", open_requests == 0, url_for("wf.company"),
                 f"{open_requests} open request(s) from recruiters." if open_requests else ""),
            step("Check ESOP pool and FMV", True, url_for("wf.settings_page"), "Used for grant ranges and dilution checks."),
            step("Review offers waiting for Finance", not _pending_for_role("finance"), url_for("wf.approvals")),
        ]
    if role in ("admin", "people_ops"):
        return [
            step("Company profile", True, url_for("wf.company")),
            step("Company metrics (or ask Finance)", has_metrics or metrics_asked, url_for("wf.company")),
            step("Job architecture bands", _count("SELECT COUNT(*) c FROM bands WHERE company_id = ?", (cid,)),
                 url_for("wf.bands")),
            step("First requisition", _count("SELECT COUNT(*) c FROM requisitions WHERE company_id = ?", (cid,)),
                 url_for("wf.requisitions")),
            step("Team's first benchmark", _count("SELECT COUNT(*) c FROM offer_requests WHERE company_id = ?", (cid,)),
                 url_for("wf.offers")),
        ]
    if role == "hiring_manager":
        return [
            step("Open a requisition", _count("SELECT COUNT(*) c FROM requisitions WHERE company_id = ?", (cid,)),
                 url_for("wf.requisitions")),
            step("Review offers waiting for you", not _pending_for_role("hiring_manager"), url_for("wf.approvals")),
        ]
    return []


@bp.route("/onboarding/dismiss", methods=["POST"])
def onboarding_dismiss():
    user = current_user()
    onboarding_state(user["id"])
    db.execute("UPDATE user_onboarding SET dismissed_at = ? WHERE user_id = ?", (db.now_iso(), user["id"]))
    db.commit()
    return redirect(url_for("wf.dashboard"))


@bp.route("/company/request-metrics", methods=["POST"])
@require("request_metrics")
def request_metrics():
    """One click from a recruiter: Finance gets a notification, the recruiter keeps working."""
    company = require_company()
    nxt = request.form.get("next") or url_for("wf.dashboard")
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("wf.dashboard")
    existing = open_metrics_request(company["id"])
    if existing is not None:
        flash(f"Finance was already asked by {existing['requester']} on {existing['requested_at'][:10]}.", "success")
        return redirect(nxt)
    user = current_user()
    note = (request.form.get("note") or "").strip()[:300] or None
    rid = db.execute("INSERT INTO metrics_requests (company_id, requested_by, requested_at, note) VALUES (?, ?, ?, ?)",
                     (company["id"], user["id"], db.now_iso(), note))
    notify(company["id"], "finance", f"{user['name']} asked for company metrics (ARR, NDR, churn, CAC, margin)"
           + (f": {note}" if note else "."), url_for("wf.company"))
    audit("request_metrics", "metrics_request", rid, {"note": note}, company["id"])
    db.commit()
    flash("Request sent to Finance. Keep going - benchmarks work without metrics; refresh them once metrics arrive.",
          "success")
    return redirect(nxt)


def _kpis(cid):
    rows = db.query("SELECT * FROM outcomes WHERE company_id = ? AND status IN ('accepted','declined','withdrawn')", (cid,))
    decided = [r for r in rows if r["status"] in ("accepted", "declined")]
    accepted = [r for r in rows if r["status"] == "accepted"]
    # Coverage is only defined for accepted offers that had a recommended range; outside
    # offers imported without one would otherwise count as misses and read as "0%".
    with_rec = [r for r in accepted if r["rec_p25"] is not None and r["final_cash_ctc"] is not None]
    in_range = [r for r in with_rec if r["rec_p25"] <= r["final_cash_ctc"] <= r["rec_p75"]]
    in_app = db.query("SELECT COUNT(*) c FROM offer_requests WHERE company_id = ? AND status NOT IN "
                      "('draft','pending_approval','changes_requested','rejected')", (cid,), one=True)["c"]
    with_outcome = db.query("SELECT COUNT(*) c FROM offer_requests WHERE company_id = ? AND status IN "
                            "('accepted','declined','withdrawn')", (cid,), one=True)["c"]
    durations = []
    for sub in db.query("SELECT s.*, r.created_at AS req_created FROM submissions s JOIN offer_requests r "
                        "ON r.id = s.request_id WHERE r.company_id = ?", (cid,)):
        state = submission_state(sub)
        if state["state"] == "approved" and state["done"]:
            last = max(a["ts"] for a in state["done"].values())
            durations.append((datetime.fromisoformat(last) - datetime.fromisoformat(sub["req_created"])).total_seconds() / 3600)
    return {
        "north_star": len([r for r in in_range if r["request_id"] is not None]),
        "acceptance_rate": round(len(accepted) / len(decided) * 100) if decided else None,
        "in_range_pct": round(len(in_range) / len(with_rec) * 100) if with_rec else None,
        "in_range_n": len(with_rec),
        "outcome_coverage": round(with_outcome / in_app * 100) if in_app else None,
        "median_hours_to_approval": round(statistics.median(durations), 1) if durations else None,
        "decided": len(decided),
    }


# --------------------------------------------------------------------------
# Company profile & metrics
# --------------------------------------------------------------------------

def _profile_from_form(form):
    errors = []
    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Company name is required.")
    stage = form.get("funding_stage")
    if stage not in FUNDING_STAGES:
        errors.append("Pick a funding stage.")
    market = form.get("target_market")
    if market not in TARGET_MARKETS:
        errors.append("Pick a target market.")
    pricing = form.get("pricing_model")
    if pricing not in PRICING_MODELS:
        errors.append("Pick a pricing model.")
    try:
        employees = int(form.get("employee_count") or 0)
        if employees < 1:
            raise ValueError
    except ValueError:
        errors.append("Employee count must be a whole number of at least 1.")
        employees = None
    arr_band = form.get("arr_band") or None
    if arr_band and arr_band not in ARR_BANDS:
        errors.append("Unrecognised ARR band.")
    locations = [c for c in form.getlist("hiring_locations") if c in CITIES]
    hq = form.get("hq_city") or None
    if hq and hq not in CITIES:
        errors.append("Unrecognised HQ city.")
    return errors, {
        "name": name, "domain": (form.get("domain") or "").strip().lower() or None,
        "sector": (form.get("sector") or "B2B SaaS").strip(), "hq_city": hq, "funding_stage": stage,
        "target_market": market, "pricing_model": pricing, "employee_count": employees, "arr_band": arr_band,
        "hiring_locations": db.dumps(locations), "work_model": form.get("work_model") or "Hybrid",
    }


@bp.route("/company", methods=["GET"])
def company():
    company = get_company()
    history = []
    profile = snap = age = None
    if company is not None:
        profile, snap, age = current_profile(company)
        history = db.query("SELECT m.*, u.name AS user_name FROM metric_snapshots m LEFT JOIN users u "
                           "ON u.id = m.created_by WHERE company_id = ? ORDER BY m.id DESC", (company["id"],))
    return render_template(
        "workflow/company.html", company=company, profile=profile, snap=snap, age=age, history=history,
        snap_inputs=db.loads(snap["inputs"], {}) if snap else {},
        cities=CITIES, funding_stages=FUNDING_STAGES, target_markets=TARGET_MARKETS, pricing_models=PRICING_MODELS,
        arr_bands=ARR_BANDS, metric_fields=METRIC_FIELDS, formula=HEALTH_SCORE_FORMULA,
        locations=db.loads(company["hiring_locations"], []) if company else [],
        settings=settings_for(company) if company else DEFAULT_SETTINGS,
        metrics_request=open_metrics_request(company["id"]) if company else None,
    )


@bp.route("/company/profile", methods=["POST"])
@require("edit_company_profile")
def company_profile_save():
    errors, values = _profile_from_form(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("wf.company"))
    company = get_company()
    now = db.now_iso()
    if company is None:
        cid = db.execute(
            "INSERT INTO companies (name, domain, sector, hq_city, funding_stage, target_market, pricing_model, "
            "employee_count, arr_band, hiring_locations, work_model, settings, created_at, updated_at) "
            "VALUES (:name, :domain, :sector, :hq_city, :funding_stage, :target_market, :pricing_model, "
            ":employee_count, :arr_band, :hiring_locations, :work_model, '{}', :now, :now)", {**values, "now": now})
        for name, kind, value, role in DEFAULT_RULES:
            db.execute("INSERT INTO approval_rules (company_id, name, condition_kind, condition_value, approver_role) "
                       "VALUES (?, ?, ?, ?, ?)", (cid, name, kind, value, role))
        audit("create_company", "company", cid, values, cid)
        flash("Workspace created. Add company metrics next - or skip and get a provisional benchmark now.", "success")
    else:
        cid = company["id"]
        db.execute("UPDATE companies SET name=:name, domain=:domain, sector=:sector, hq_city=:hq_city, "
                   "funding_stage=:funding_stage, target_market=:target_market, pricing_model=:pricing_model, "
                   "employee_count=:employee_count, arr_band=:arr_band, hiring_locations=:hiring_locations, "
                   "work_model=:work_model, updated_at=:now WHERE id=:id", {**values, "now": now, "id": cid})
        before = {k: company[k] for k in values}
        audit("update_company", "company", cid,
              {"changed": {k: [before[k], v] for k, v in values.items() if before[k] != v}}, cid)
        flash("Company profile saved.", "success")
    db.commit()
    return redirect(url_for("wf.company"))


def _save_metrics(company, inputs, source):
    settings = settings_for(company)
    profile = compute_health_profile(inputs, company["funding_stage"], stage_default_health(company["funding_stage"]),
                                     float(settings["inr_per_usd"]))
    sid = db.execute(
        "INSERT INTO metric_snapshots (company_id, created_at, created_by, source, inputs, derived, health_score, "
        "health_band, health_score_version, completeness) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (company["id"], db.now_iso(), current_user()["id"], source, db.dumps(inputs), db.dumps(profile),
         profile["health_score"], profile["health_band"], profile["version"], profile["completeness"]))
    if inputs.get("headcount"):
        db.execute("UPDATE companies SET employee_count = ?, updated_at = ? WHERE id = ?",
                   (int(inputs["headcount"]), db.now_iso(), company["id"]))
    audit("record_metrics", "metric_snapshot", sid,
          {"source": source, "health_score": profile["health_score"], "completeness": profile["completeness"]},
          company["id"])
    if open_metrics_request(company["id"]) is not None:
        db.execute("UPDATE metrics_requests SET fulfilled_at = ?, fulfilled_snapshot_id = ? "
                   "WHERE company_id = ? AND fulfilled_at IS NULL", (db.now_iso(), sid, company["id"]))
        notify(company["id"], "recruiter", f"Company metrics are in (health {profile['health_score']:.0f}/100). "
               f"Refresh draft offers to update their target.", url_for("wf.offers"))
    db.commit()
    return profile


@bp.route("/company/metrics", methods=["POST"])
@require("edit_metrics")
def company_metrics_save():
    company = require_company()
    try:
        inputs = parse_metric_inputs(request.form)
    except MetricsError as exc:
        flash(str(exc), "error")
        return redirect(url_for("wf.company"))
    profile = _save_metrics(company, inputs, "manual")
    flash(f"Metrics saved as a new snapshot. Health score {profile['health_score']:.0f}/100 "
          f"({profile['health_band']}), {round(profile['coverage'] * 100)}% from real metrics.", "success")
    return redirect(url_for("wf.company"))


@bp.route("/company/metrics-template.csv")
def company_metrics_template():
    return Response(metrics_csv_template(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=compintel-metrics-template.csv"})


@bp.route("/company/metrics-upload", methods=["POST"])
@require("edit_metrics")
def company_metrics_upload():
    company = require_company()
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file to upload.", "error")
        return redirect(url_for("wf.company"))
    try:
        text = file.read(200_000).decode("utf-8-sig")
        inputs = parse_metrics_csv(text)
    except (UnicodeDecodeError, MetricsError) as exc:
        flash(f"Couldn't read that CSV: {exc}", "error")
        return redirect(url_for("wf.company"))
    profile = _save_metrics(company, inputs, "csv")
    flash(f"CSV imported as a new snapshot. Health score {profile['health_score']:.0f}/100.", "success")
    return redirect(url_for("wf.company"))


@bp.route("/company/demo", methods=["POST"])
@require("edit_company_profile")
def load_demo():
    if get_company() is not None:
        flash("A workspace already exists.", "error")
        return redirect(url_for("wf.company"))
    from .seed import seed_demo_workspace
    seed_demo_workspace(current_user(), _cohorts)
    flash("Demo workspace loaded: a sample Series A company, metrics, bands, requisitions and past outcomes.", "success")
    return redirect(url_for("wf.dashboard"))


# --------------------------------------------------------------------------
# Job architecture (bands)
# --------------------------------------------------------------------------

@bp.route("/bands", methods=["GET", "POST"])
def bands():
    company = require_company()
    if request.method == "POST":
        if not can("manage_bands"):
            abort(403, description="Only People Ops and Admin can edit bands.")
        action = request.form.get("action")
        if action == "seed":
            n = 0
            for row in _cohorts.band_seed(company["funding_stage"], company["target_market"]):
                mid = round(row["cash_ctc_lpa"], 1)
                cur = db.execute(
                    "INSERT OR IGNORE INTO bands (company_id, role, level, city_tier, min_lpa, mid_lpa, max_lpa, source, "
                    "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, 'seeded from demo dataset', ?, ?)",
                    (company["id"], row["role"], row["level"], row["saas_hub_tier"], round(mid * 0.8, 1), mid,
                     round(mid * 1.2, 1), db.now_iso(), current_user()["id"]))
                n += 1 if cur else 0
            audit("seed_bands", "bands", None, {"rows": n}, company["id"])
            flash("Missing bands seeded from the demo dataset medians (min 80%, max 120% of midpoint).", "success")
        elif action == "delete":
            bid = request.form.get("band_id", type=int)
            band = db.query("SELECT * FROM bands WHERE id = ? AND company_id = ?", (bid, company["id"]), one=True)
            if band:
                db.execute("DELETE FROM bands WHERE id = ?", (bid,))
                audit("delete_band", "band", bid, dict(band), company["id"])
                flash("Band removed.", "success")
        else:
            try:
                role, level, tier = request.form["role"], request.form["level"], request.form["city_tier"]
                if role not in ALL_ROLES or level not in LEVELS or tier not in {t for t, _m in CITIES.values()}:
                    raise ValueError("Pick a role, level and city tier.")
                lo, mid, hi = (float(request.form[k]) for k in ("min_lpa", "mid_lpa", "max_lpa"))
                if not (0 < lo <= mid <= hi):
                    raise ValueError("Band must satisfy 0 < min <= midpoint <= max.")
            except (KeyError, ValueError) as exc:
                flash(str(exc) if str(exc) else "All band fields are required.", "error")
                return redirect(url_for("wf.bands"))
            db.execute(
                "INSERT INTO bands (company_id, role, level, city_tier, min_lpa, mid_lpa, max_lpa, source, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?) ON CONFLICT(company_id, role, level, city_tier) DO UPDATE SET "
                "min_lpa=excluded.min_lpa, mid_lpa=excluded.mid_lpa, max_lpa=excluded.max_lpa, source='manual', "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (company["id"], role, level, tier, lo, mid, hi, db.now_iso(), current_user()["id"]))
            audit("upsert_band", "band", None, {"role": role, "level": level, "tier": tier, "min": lo, "mid": mid, "max": hi},
                  company["id"])
            flash(f"Band saved for {role} / {level} / {tier}.", "success")
        db.commit()
        return redirect(url_for("wf.bands", role=request.form.get("filter_role", "")))
    role_filter = request.args.get("role", "")
    sql = "SELECT * FROM bands WHERE company_id = ?"
    params = [company["id"]]
    if role_filter:
        sql += " AND role = ?"
        params.append(role_filter)
    rows = db.query(sql + " ORDER BY role, CASE level WHEN 'Junior' THEN 0 WHEN 'Mid' THEN 1 WHEN 'Senior' THEN 2 ELSE 3 END, city_tier", params)
    return render_template("workflow/bands.html", rows=rows, roles=ROLE_ARCHETYPES, levels=LEVELS,
                           tiers=sorted({t for t, _m in CITIES.values()}), role_filter=role_filter)


# --------------------------------------------------------------------------
# Requisitions
# --------------------------------------------------------------------------

@bp.route("/requisitions", methods=["GET", "POST"])
def requisitions():
    company = require_company()
    if request.method == "POST":
        if not can("manage_requisitions"):
            abort(403, description="This role can't create requisitions.")
        f = request.form
        try:
            if f.get("role") not in ALL_ROLES or f.get("level") not in LEVELS or f.get("city") not in CITIES:
                raise ValueError("Pick a role, level and city.")
            budget = _float_or_none(f.get("budget_max_lpa"), "Budget", 0.5, 1000)
            openings = int(f.get("openings") or 1)
            if openings < 1:
                raise ValueError("Openings must be at least 1.")
            emp = f.get("employment_type") or "Full-time"
            if emp not in EMPLOYMENT_TYPES:
                raise ValueError("Pick an employment type.")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("wf.requisitions"))
        hm = f.get("hiring_manager_id", type=int)
        rid = db.execute(
            "INSERT INTO requisitions (company_id, role, level, city, budget_max_lpa, openings, hiring_manager_id, "
            "notes, created_by, created_at, employment_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company["id"], f["role"], f["level"], f["city"], budget, openings, hm, (f.get("notes") or "")[:500],
             current_user()["id"], db.now_iso(), emp))
        audit("create_requisition", "requisition", rid, {"role": f["role"], "level": f["level"], "city": f["city"],
                                                         "budget": budget}, company["id"])
        db.commit()
        flash("Requisition created.", "success")
        return redirect(url_for("wf.requisitions"))
    rows = db.query("SELECT q.*, u.name AS hm_name, (SELECT COUNT(*) FROM offer_requests o WHERE o.requisition_id = q.id) "
                    "AS offer_count FROM requisitions q LEFT JOIN users u ON u.id = q.hiring_manager_id "
                    "WHERE q.company_id = ? ORDER BY q.id DESC", (company["id"],))
    hms = db.query("SELECT * FROM users WHERE role = 'hiring_manager' AND active = 1")
    locations = db.loads(company["hiring_locations"], []) or list(CITIES)
    return render_template("workflow/requisitions.html", rows=rows, roles=ROLE_ARCHETYPES, levels=LEVELS,
                           cities=CITIES, locations=locations, hms=hms, employment_types=EMPLOYMENT_TYPES)


# --------------------------------------------------------------------------
# Offers
# --------------------------------------------------------------------------

@bp.route("/offers")
@require("view_offers")
def offers():
    company = require_company()
    user = current_user()
    status = request.args.get("status", "")
    sql = "SELECT * FROM offer_requests WHERE company_id = ?"
    params = [company["id"]]
    if user["role"] == "exec":
        sql += " AND status IN ('approved','sent','accepted','declined','withdrawn')"
    if status:
        sql += " AND status = ?"
        params.append(status)
    rows = db.query(sql + " ORDER BY updated_at DESC", params)
    return render_template("workflow/offers.html", rows=rows, status=status,
                           benchmarks={r["id"]: load_bm(r["benchmark"]) for r in rows})


@bp.route("/offers/new", methods=["GET", "POST"])
@require("create_offer")
def offer_new():
    company = require_company()
    reqs = db.query("SELECT * FROM requisitions WHERE company_id = ? AND status = 'open' ORDER BY id DESC", (company["id"],))
    selected = request.values.get("requisition_id", type=int)
    requisition = db.query("SELECT * FROM requisitions WHERE id = ? AND company_id = ?", (selected, company["id"]), one=True) \
        if selected else None
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        f = request.form
        try:
            role = requisition["role"] if requisition else f.get("role")
            level = requisition["level"] if requisition else f.get("level")
            city = requisition["city"] if requisition else f.get("city")
            emp = requisition["employment_type"] if requisition else (f.get("employment_type") or "Full-time")
            if role not in ALL_ROLES or level not in LEVELS or city not in CITIES:
                raise ValueError("Pick a requisition, or a role, level and city.")
            if emp not in EMPLOYMENT_TYPES:
                raise ValueError("Pick an employment type.")
            years = _float_or_none(f.get("years_experience"), "Years of experience", 0, 45)
            if years is None:
                raise ValueError("Years of experience is required.")
            if f.get("skill_category") not in SKILL_CATEGORIES:
                raise ValueError("Pick a skill category.")
            if f.get("education_pedigree") not in EDUCATION_PEDIGREE:
                raise ValueError("Pick an education background.")
            fields = {
                "role": role, "level": level, "city": city, "years_experience": years,
                "skill_category": f["skill_category"], "education_pedigree": f["education_pedigree"],
                "current_ctc": _float_or_none(f.get("current_ctc"), "Current CTC"),
                "expected_ctc": _float_or_none(f.get("expected_ctc"), "Expected CTC"),
                "competing_offer": _float_or_none(f.get("competing_offer"), "Competing offer"),
                "employment_type": emp,
            }
            start = f.get("start_date") or None
            if start:
                date.fromisoformat(start)
        except ValueError as exc:
            flash(str(exc) if "Invalid isoformat" not in str(exc) else "Start date is invalid.", "error")
            return render_template("workflow/offer_new.html", reqs=reqs, requisition=requisition, form=form,
                                   **_offer_form_options())
        bm, snap = generate_benchmark(company, fields)
        import secrets
        pseudo = "CAND-" + secrets.token_hex(4).upper()
        now = db.now_iso()
        rid = db.execute(
            "INSERT INTO offer_requests (company_id, requisition_id, candidate_name, candidate_pseudo_id, role, level, city, "
            "years_experience, skill_category, education_pedigree, current_ctc, expected_ctc, competing_offer, start_date, "
            "status, metrics_snapshot_id, benchmark, created_by, created_at, updated_at, employment_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
            (company["id"], requisition["id"] if requisition else None, (f.get("candidate_name") or "").strip()[:120] or None,
             pseudo, role, level, city, years, fields["skill_category"], fields["education_pedigree"],
             fields["current_ctc"], fields["expected_ctc"], fields["competing_offer"], start,
             snap["id"] if snap else None, db.dumps(bm), current_user()["id"], now, now, emp))
        db.execute("INSERT INTO packages (request_id, label, data, updated_at, updated_by) VALUES (?, 'A', ?, ?, ?)",
                   (rid, db.dumps(default_package(bm, settings_for(company), fields, requisition)), now,
                    current_user()["id"]))
        audit("create_offer_request", "offer_request", rid,
              {"pseudo_id": pseudo, "role": role, "level": level, "city": city, "p50": bm["p50"], "target": bm["target"],
               "confidence": bm["confidence"]["level"], "data_mode": bm["data_mode"],
               "cohort_hash": bm["cohort"].get("cohort_hash"), "model_version": bm["model_version"]}, company["id"])
        mark_first_benchmark(current_user()["id"])
        db.commit()
        return redirect(url_for("wf.offer_detail", request_id=rid))
    return render_template("workflow/offer_new.html", reqs=reqs, requisition=requisition, form=form, **_offer_form_options())


def _offer_form_options():
    return dict(roles=ROLE_ARCHETYPES, levels=LEVELS, cities=CITIES, skills=SKILL_CATEGORIES,
                pedigrees=EDUCATION_PEDIGREE, employment_types=EMPLOYMENT_TYPES)


@bp.route("/offers/<int:request_id>")
@require("view_offers")
def offer_detail(request_id):
    company = require_company()
    req = get_request(request_id)
    bm = load_bm(req["benchmark"])
    settings = settings_for(company)
    packages = {p["label"]: db.loads(p["data"]) for p in
                db.query("SELECT * FROM packages WHERE request_id = ? ORDER BY label", (request_id,))}
    label = request.args.get("scenario") or (next(iter(packages)) if packages else "A")
    if label not in SCENARIO_LABELS:
        label = "A"
    subs = db.query("SELECT s.*, u.name AS user_name FROM submissions s LEFT JOIN users u ON u.id = s.submitted_by "
                    "WHERE request_id = ? ORDER BY revision DESC", (request_id,))
    latest = subs[0] if subs else None
    editable = req["status"] in EDITABLE_STATUSES and can("create_offer")
    requisition = db.query("SELECT * FROM requisitions WHERE id = ?", (req["requisition_id"],), one=True) \
        if req["requisition_id"] else None
    if req["status"] not in EDITABLE_STATUSES and latest is not None:
        # Locked: show exactly what was submitted, checked against the rules of the day.
        frozen = db.loads(latest["snapshot"])
        pkg, evaluation, bm, label = frozen["package"], frozen["evaluation"], load_bm(frozen["benchmark"]), latest["package_label"]
        route_preview = None
    else:
        pkg = packages.get(label) or default_package(bm, settings, req, requisition)
        evaluation = evaluate(company, req, pkg, bm)
        route_preview = preview_route(company, req, pkg, evaluation)
    state = submission_state(latest) if latest else None
    actions = submission_actions(latest["id"]) if latest else []
    route = db.loads(latest["route"]) if latest else []
    outcome = db.query("SELECT * FROM outcomes WHERE request_id = ?", (request_id,), one=True)
    user = current_user()
    role_turn = bool(state and state["state"] == "pending" and state["current_step"]["role"] == user["role"])
    own_submission = bool(latest and latest["submitted_by"] == user["id"])
    can_decide = role_turn and not own_submission
    return render_template(
        "workflow/offer.html", req=req, bm=bm, pkg=pkg, label=label, packages=packages, ev=evaluation,
        subs=subs, latest=latest, state=state, actions=actions, route=route, outcome=outcome,
        editable=editable, route_preview=route_preview, blocked_own=role_turn and own_submission,
        show_benchmark=can("view_benchmark"), can_decide=can_decide, settings=settings, requisition=requisition,
        override_reasons=OVERRIDE_REASONS, scenario_labels=SCENARIO_LABELS, ruleset=RULESET, disclaimer=DISCLAIMER,
        exception_kinds=EXCEPTION_KINDS,
    )


@bp.route("/offers/<int:request_id>/evaluate", methods=["POST"])
@require("view_benchmark")
def offer_evaluate(request_id):
    """Live checks while the recruiter edits - nothing is saved."""
    company = require_company()
    req = get_request(request_id)
    try:
        pkg = parse_package(request.form, settings_for(company))
    except PackageError as exc:
        return render_template("workflow/_checks.html", ev=None, error=str(exc), route_preview=None)
    ev = evaluate(company, req, pkg)
    return render_template("workflow/_checks.html", ev=ev, error=None, route_preview=preview_route(company, req, pkg, ev))


def preview_route(company, req, pkg, ev):
    rules = db.query("SELECT * FROM approval_rules WHERE company_id = ? ORDER BY id", (company["id"],))
    return compute_route(rules, cash_ctc=pkg["cash_ctc"], level=req["level"], role_family=role_archetype_for(req["role"]),
                         city=req["city"], exceptions=set(ev["exceptions"]))


@bp.route("/offers/<int:request_id>/package", methods=["POST"])
@require("create_offer")
def offer_package_save(request_id):
    company = require_company()
    req = get_request(request_id)
    if req["status"] not in EDITABLE_STATUSES:
        abort(400, description="This offer is locked while it's in approval or after approval.")
    label = request.form.get("label", "A")
    if label not in SCENARIO_LABELS:
        abort(400, description="Unknown scenario.")
    settings = settings_for(company)
    try:
        pkg = parse_package(request.form, settings)
    except PackageError as exc:
        flash(str(exc), "error")
        return redirect(url_for("wf.offer_detail", request_id=request_id, scenario=label))
    now = db.now_iso()
    db.execute("INSERT INTO packages (request_id, label, data, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
               "ON CONFLICT(request_id, label) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at, "
               "updated_by=excluded.updated_by", (request_id, label, db.dumps(pkg), now, current_user()["id"]))
    db.execute("UPDATE offer_requests SET updated_at = ? WHERE id = ?", (now, request_id))
    audit("save_package", "offer_request", request_id, {"scenario": label, "cash_ctc": pkg["cash_ctc"],
                                                         "esop_options": pkg["esop_options"]}, company["id"])

    if request.form.get("action") != "submit":
        db.commit()
        flash(f"Scenario {label} saved.", "success")
        return redirect(url_for("wf.offer_detail", request_id=request_id, scenario=label))

    bm = load_bm(req["benchmark"])
    ev = evaluate(company, req, pkg, bm)
    if ev["blocked"]:
        db.commit()
        flash("Can't submit yet - fix the blocking checks first.", "error")
        return redirect(url_for("wf.offer_detail", request_id=request_id, scenario=label))
    rules = db.query("SELECT * FROM approval_rules WHERE company_id = ? ORDER BY id", (company["id"],))
    route = compute_route(rules, cash_ctc=pkg["cash_ctc"], level=req["level"], role_family=role_archetype_for(req["role"]),
                          city=req["city"], exceptions=set(ev["exceptions"]))
    if not route:
        db.commit()
        flash("No approval rule matched, so this offer has no approver. Ask an admin to add an 'Always' rule.", "error")
        return redirect(url_for("wf.offer_detail", request_id=request_id, scenario=label))
    prev = latest_submission(request_id)
    revision = (prev["revision"] + 1) if prev else 1
    snapshot = {
        "request": {k: req[k] for k in req.keys() if k not in ("candidate_name", "benchmark")},
        "company": {k: company[k] for k in ("name", "funding_stage", "target_market", "pricing_model", "employee_count")},
        "benchmark": bm, "package": pkg, "evaluation": ev, "policy": settings, "route": route,
        "scenario": label, "app_model_version": _model_version,
    }
    blob = db.dumps(snapshot)
    snapshot_hash = hashlib.sha256(blob.encode()).hexdigest()
    ctx = session_context()
    sid = db.execute("INSERT INTO submissions (request_id, revision, package_label, submitted_by, submitted_at, snapshot, "
                     "snapshot_hash, route, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (request_id, revision, label, current_user()["id"], now, blob, snapshot_hash, db.dumps(route),
                      db.dumps(ctx)))
    db.execute("UPDATE offer_requests SET status = 'pending_approval', updated_at = ? WHERE id = ?", (now, request_id))
    # The chained audit entry pins every version that produced this offer.
    audit("submit_offer", "submission", sid, {
        "request_id": request_id, "revision": revision, "scenario": label,
        "route": [s["role"] for s in route], "exceptions": ev["exceptions"],
        "snapshot_hash": snapshot_hash, "model_version": bm["model_version"],
        "health_score_version": bm["health_score_version"],
        "ruleset": f"{ev['ruleset']['id']} v{ev['ruleset']['version']}",
        "cohort_hash": bm["cohort"].get("cohort_hash"), "dataset_sha": bm["cohort"].get("dataset_sha"),
        "data_mode": bm["data_mode"],
    }, company["id"])
    notify(company["id"], route[0]["role"], f"Offer {req['candidate_pseudo_id']} ({req['role']}) needs your approval.",
           url_for("wf.offer_detail", request_id=request_id))
    db.commit()
    flash(f"Submitted revision {revision} for approval: " + " -> ".join(s["role_label"] for s in route) + ".", "success")
    return redirect(url_for("wf.offer_detail", request_id=request_id))


@bp.route("/offers/<int:request_id>/rebenchmark", methods=["POST"])
@require("create_offer")
def offer_rebenchmark(request_id):
    company = require_company()
    req = get_request(request_id)
    if req["status"] not in EDITABLE_STATUSES:
        abort(400, description="Benchmark is frozen while the offer is in or past approval.")
    bm, snap = generate_benchmark(company, offer_ctx(req))
    db.execute("UPDATE offer_requests SET benchmark = ?, metrics_snapshot_id = ?, updated_at = ? WHERE id = ?",
               (db.dumps(bm), snap["id"] if snap else None, db.now_iso(), request_id))
    audit("refresh_benchmark", "offer_request", request_id, {"p50": bm["p50"], "target": bm["target"]}, company["id"])
    db.commit()
    flash("Benchmark refreshed with the latest company metrics and bands.", "success")
    return redirect(url_for("wf.offer_detail", request_id=request_id))


@bp.route("/offers/<int:request_id>/scenarios")
@require("view_benchmark")
def offer_scenarios(request_id):
    company = require_company()
    req = get_request(request_id)
    bm = load_bm(req["benchmark"])
    rows = db.query("SELECT * FROM packages WHERE request_id = ? ORDER BY label", (request_id,))
    scenarios = []
    for p in rows:
        pkg = db.loads(p["data"])
        scenarios.append({"label": p["label"], "pkg": pkg, "ev": evaluate(company, req, pkg, bm), "updated_at": p["updated_at"]})
    return render_template("workflow/scenarios.html", req=req, bm=bm, scenarios=scenarios)


def _insert_action(sub, step, user, action, comment, ts=None):
    row = {"submission_id": sub["id"], "step": step, "approver_role": user["role"], "user_id": user["id"],
           "action": action, "comment": comment, "ts": ts or db.now_iso()}
    row["content_hash"] = action_hash(row, sub["snapshot_hash"])
    db.execute("INSERT INTO approval_actions (submission_id, step, approver_role, user_id, action, comment, ts, "
               "content_hash) VALUES (:submission_id, :step, :approver_role, :user_id, :action, :comment, :ts, "
               ":content_hash)", row)
    return row["content_hash"]


@bp.route("/offers/<int:request_id>/decision", methods=["POST"])
@require("view_offers")
def offer_decision(request_id):
    company = require_company()
    req = get_request(request_id)
    sub = latest_submission(request_id)
    if sub is None:
        abort(400, description="Nothing has been submitted for this offer.")
    action = request.form.get("action")
    comment = (request.form.get("comment") or "").strip()[:1000]
    user = current_user()
    state = submission_state(sub)
    if action == "comment":
        if not can("view_benchmark"):
            abort(403, description="Read-only executives can't comment on offers.")
        if not comment:
            flash("Write a comment first.", "error")
            return redirect(url_for("wf.offer_detail", request_id=request_id))
        h = _insert_action(sub, None, user, "comment", comment)
        audit("comment", "submission", sub["id"], {"comment": comment, "action_hash": h}, company["id"])
        db.commit()
        return redirect(url_for("wf.offer_detail", request_id=request_id))
    if action not in ("approve", "reject", "request_changes"):
        abort(400, description="Unknown action.")
    if state["state"] != "pending" or state["current_step"]["role"] != user["role"]:
        audit("decision_denied", "submission", sub["id"], {"action": action}, company["id"])
        db.commit()
        abort(403, description="It isn't this role's turn to decide on this offer.")
    if sub["submitted_by"] == user["id"]:
        # Separation of duties: holding the right role isn't enough if you submitted it.
        audit("decision_denied", "submission", sub["id"], {"action": action, "reason": "own_submission"}, company["id"])
        db.commit()
        abort(403, description="You submitted this offer, so someone else with this role has to decide on it.")
    if action in ("reject", "request_changes") and not comment:
        flash("Add a comment explaining the rejection or the changes needed.", "error")
        return redirect(url_for("wf.offer_detail", request_id=request_id))
    step = state["current_step"]
    now = db.now_iso()
    h = _insert_action(sub, step["step"], user, action, comment or None, now)
    new_state = submission_state(sub)
    status = {"approved": "approved", "pending": "pending_approval", "rejected": "rejected",
              "changes_requested": "changes_requested"}[new_state["state"]]
    db.execute("UPDATE offer_requests SET status = ?, updated_at = ? WHERE id = ?", (status, now, request_id))
    audit(action, "submission", sub["id"], {"step": step["step"], "role": user["role"], "comment": comment,
                                            "resulting_status": status, "action_hash": h,
                                            "snapshot_hash": sub["snapshot_hash"]}, company["id"])
    link = url_for("wf.offer_detail", request_id=request_id)
    if new_state["state"] == "pending":
        notify(company["id"], new_state["current_step"]["role"],
               f"Offer {req['candidate_pseudo_id']} ({req['role']}) needs your approval.", link)
    else:
        notify(company["id"], "recruiter", f"Offer {req['candidate_pseudo_id']} is {STATUS_LABELS[status].lower()}.", link)
    db.commit()
    flash(f"Recorded: {action.replace('_', ' ')}.", "success")
    return redirect(link)


@bp.route("/offers/<int:request_id>/submissions/<int:revision>")
@require("view_benchmark")
def submission_view(request_id, revision):
    company = require_company()
    req = get_request(request_id)
    sub = db.query("SELECT * FROM submissions WHERE request_id = ? AND revision = ?", (request_id, revision), one=True)
    if sub is None:
        abort(404, description="Submission not found.")
    snapshot = db.loads(sub["snapshot"])
    verification = None
    if request.args.get("verify"):
        verification = _verify_submission(company, snapshot, sub)
        audit("verify_snapshot", "submission", sub["id"], {"result": verification["ok"]}, company["id"])
        db.commit()
    return render_template("workflow/submission.html", req=req, sub=sub, snap=snapshot, verification=verification,
                           state=submission_state(sub), actions=submission_actions(sub["id"]))


def _verify_submission(company, snapshot, sub):
    """Re-runs the stored inputs through the model and checks the hash, to prove
    the submission can be reproduced exactly (OB-08)."""
    bm = snapshot["benchmark"]
    blob_ok = hashlib.sha256(sub["snapshot"].encode()).hexdigest() == sub["snapshot_hash"]
    same_model = bm["model_version"] == _model_version
    if bm.get("p50") is None:  # no model was run (insufficient data / non-demo workspace)
        return {"ok": blob_ok, "hash_ok": blob_ok, "same_model": same_model, "reproduced": None, "diffs": {},
                "rerun": None, "no_model": True}
    if bm.get("cohort", {}).get("engine") == pilot.ENGINE_VERSION:
        # Observed-offer benchmark: re-derive the range from the exact stored rows.
        rep = pilot.reproduce(bm["cohort"])
        return {"ok": blob_ok and rep["ok"], "hash_ok": blob_ok, "same_model": True, "observed": True,
                "reproduced": rep.get("reproduced"), "rows_hash_ok": rep.get("rows_hash_ok"),
                "rerun": rep.get("rerun"), "reason": rep.get("reason"), "diffs": {}}
    inputs = {k: bm["inputs"][k] for k in ("role", "years_experience", "city", "funding_stage", "target_market",
                                           "pricing_model", "skill_category", "education_pedigree",
                                           "health_score_override")}
    rerun = _predict_fn(inputs, apply_signals=False)
    diffs = {q: round(abs(rerun[q] - bm[q]), 4) for q in ("p25", "p50", "p75")}
    reproduced = all(d < 0.01 for d in diffs.values())
    return {"ok": blob_ok and (reproduced or not same_model), "hash_ok": blob_ok, "same_model": same_model,
            "reproduced": reproduced, "diffs": diffs,
            "rerun": {q: round(rerun[q], 2) for q in ("p25", "p50", "p75")}}


@bp.route("/offers/<int:request_id>/brief.pdf")
@require("export_benchmark")
def offer_brief(request_id):
    company = require_company()
    req = get_request(request_id)
    sub = latest_submission(request_id)
    if sub is None:
        abort(400, description="Submit the offer before generating an approval brief.")
    state = submission_state(sub)
    pdf = internal_brief_pdf(company=company, request_row=req, submission=sub, snapshot=db.loads(sub["snapshot"]),
                             route=db.loads(sub["route"]), actions=submission_actions(sub["id"]), state=state)
    audit("export_internal_brief", "submission", sub["id"], {}, company["id"])
    db.commit()
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=offer-brief-{req['candidate_pseudo_id']}-r{sub['revision']}.pdf"})


def _approved_package(req):
    sub = latest_submission(req["id"])
    if sub is None or submission_state(sub)["state"] != "approved":
        abort(400, description="The candidate summary is available once the offer is fully approved.")
    snap = db.loads(sub["snapshot"])
    return sub, snap["package"], snap["evaluation"]["structure"]


def _ctc_ack(sub):
    return db.query("SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id "
                    "WHERE a.action = 'acknowledge_ctc_warnings' AND a.entity = 'submission' AND a.entity_id = ? "
                    "ORDER BY a.id LIMIT 1", (sub["id"],), one=True)


@bp.route("/offers/<int:request_id>/candidate-summary", methods=["GET", "POST"])
@require("record_outcome")
def candidate_summary(request_id):
    """Candidate-facing export. If the approved package carries CTC warnings, a
    named person must acknowledge them first; the acknowledgment is audited."""
    company = require_company()
    req = get_request(request_id)
    sub, pkg, structure = _approved_package(req)
    snap = db.loads(sub["snapshot"])
    warnings = snap["evaluation"].get("ctc_warnings") or [
        c for c in snap["evaluation"]["checks"] if c["category"] in ("CTC structure", "Statutory wage") and c["status"] == "warn"]
    ack = _ctc_ack(sub) if warnings else None
    if request.method == "POST":
        if not warnings:
            return redirect(url_for("wf.candidate_summary", request_id=request_id))
        if request.form.get("confirm") != "on":
            flash("Tick the box to confirm you've reviewed the CTC warnings.", "error")
        else:
            audit("acknowledge_ctc_warnings", "submission", sub["id"],
                  {"warnings": [w["title"] for w in warnings], "ruleset": snap["evaluation"]["ruleset"]["version"]},
                  company["id"])
            db.commit()
        return redirect(url_for("wf.candidate_summary", request_id=request_id))
    if warnings and ack is None:
        return render_template("workflow/candidate_ack.html", req=req, warnings=warnings,
                               ruleset=snap["evaluation"]["ruleset"], disclaimer=DISCLAIMER)
    bm = load_bm(snap["benchmark"])
    ctx = candidate_summary_context(company=company, request_row=req, package=pkg, structure=structure,
                                    settings=snap["policy"])
    ctx["demo"] = bm["data_mode"] == "demo"
    audit("view_candidate_summary", "submission", sub["id"], {"format": request.args.get("format", "html")}, company["id"])
    db.commit()
    if request.args.get("format") == "pdf":
        return Response(candidate_summary_pdf(ctx), mimetype="application/pdf", headers={
            "Content-Disposition": f"attachment; filename=offer-summary-{req['candidate_pseudo_id']}.pdf"})
    return render_template("workflow/candidate_summary.html", c=ctx, req=req, ack=ack)


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

def _parse_outcome(form):
    status = form.get("status")
    if status not in OUTCOME_STATUSES:
        raise ValueError("Pick an outcome status.")
    out = {"status": status}
    for key in ("sent_date", "verbal_acceptance_date", "decision_date"):
        v = form.get(key) or None
        if v:
            date.fromisoformat(v)
        out[key] = v
    out["final_cash_ctc"] = _float_or_none(form.get("final_cash_ctc"), "Final cash CTC")
    opts = form.get("final_esop_options")
    out["final_esop_options"] = int(float(opts)) if opts not in (None, "") else None
    # Offer-outcome telemetry (PRD 9.3): the opening offer and the candidate's counter, if any.
    out["initial_cash_ctc"] = _float_or_none(form.get("initial_cash_ctc"), "Initial cash CTC")
    out["counter_cash_ctc"] = _float_or_none(form.get("counter_cash_ctc"), "Candidate counter (cash)")
    for key, label in (("initial_esop_options", "Initial ESOP options"), ("counter_esop_options", "Counter ESOP options")):
        v = _float_or_none(form.get(key), label, 0, 10**12)
        out[key] = int(v) if v is not None else None
    out["competing_offer"] = _float_or_none(form.get("competing_offer"), "Competing offer")
    reason = form.get("decline_reason") or None
    if reason and reason not in DECLINE_REASONS:
        raise ValueError("Unrecognised decline reason.")
    out["decline_reason"] = reason
    out["notes"] = (form.get("notes") or "").strip()[:1000] or None
    if status == "accepted" and out["final_cash_ctc"] is None:
        raise ValueError("Final cash CTC is required for an accepted offer.")
    return out


@bp.route("/offers/<int:request_id>/outcome", methods=["GET", "POST"])
@require("record_outcome")
def offer_outcome(request_id):
    company = require_company()
    req = get_request(request_id)
    if req["status"] not in ("approved", *OUTCOME_STATUSES):
        abort(400, description="Record an outcome once the offer is approved.")
    sub, pkg, _structure = _approved_package(req)
    bm = load_bm(db.loads(sub["snapshot"])["benchmark"])
    existing = db.query("SELECT * FROM outcomes WHERE request_id = ?", (request_id,), one=True)
    if request.method == "POST":
        try:
            o = _parse_outcome(request.form)
        except ValueError as exc:
            flash(str(exc) if "isoformat" not in str(exc) else "One of the dates is invalid.", "error")
            return redirect(url_for("wf.offer_outcome", request_id=request_id))
        # The opening offer is the approved package, not something typed in afterwards.
        o["initial_cash_ctc"], o["initial_esop_options"] = pkg["cash_ctc"], pkg["esop_options"]
        o["employment_type"] = req["employment_type"]
        now = db.now_iso()
        if existing:
            db.execute("UPDATE outcomes SET status=:status, sent_date=:sent_date, verbal_acceptance_date=:verbal_acceptance_date, "
                       "decision_date=:decision_date, final_cash_ctc=:final_cash_ctc, final_esop_options=:final_esop_options, "
                       "competing_offer=:competing_offer, decline_reason=:decline_reason, notes=:notes, "
                       "initial_cash_ctc=:initial_cash_ctc, initial_esop_options=:initial_esop_options, "
                       "counter_cash_ctc=:counter_cash_ctc, counter_esop_options=:counter_esop_options, updated_at=:now "
                       "WHERE id=:id", {**o, "now": now, "id": existing["id"]})
            oid = existing["id"]
        else:
            oid = db.execute(
                "INSERT INTO outcomes (company_id, request_id, external, role, level, city, status, sent_date, "
                "verbal_acceptance_date, decision_date, final_cash_ctc, final_esop_options, competing_offer, decline_reason, "
                "rec_p25, rec_p50, rec_p75, notes, created_by, created_at, updated_at, employment_type, health_band, "
                "initial_cash_ctc, initial_esop_options, counter_cash_ctc, counter_esop_options) "
                "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company["id"], request_id, req["role"], req["level"], req["city"], o["status"], o["sent_date"],
                 o["verbal_acceptance_date"], o["decision_date"], o["final_cash_ctc"], o["final_esop_options"],
                 # Calibration is measured against the model's own P25-P75, not the widened display range.
                 o["competing_offer"], o["decline_reason"], bm["p25"], bm["p50"], bm["p75"],
                 o["notes"], current_user()["id"], now, now, req["employment_type"], bm["health"]["band"],
                 o["initial_cash_ctc"], o["initial_esop_options"], o["counter_cash_ctc"], o["counter_esop_options"]))
        db.execute("UPDATE offer_requests SET status = ?, updated_at = ? WHERE id = ?", (o["status"], now, request_id))
        shared = record_observation(company, oid)
        negotiated = (o["counter_cash_ctc"] is not None or
                      (o["final_cash_ctc"] is not None and abs(o["final_cash_ctc"] - pkg["cash_ctc"]) > 0.005))
        audit("record_outcome", "outcome", oid, {**o, "negotiated": negotiated, "shared": shared}, company["id"])
        db.commit()
        flash("Outcome recorded." + (" An anonymised copy was added to the benchmark network." if shared else ""), "success")
        return redirect(url_for("wf.offer_detail", request_id=request_id))
    return render_template("workflow/outcome.html", req=req, pkg=pkg, existing=existing, statuses=OUTCOME_STATUSES,
                           reasons=DECLINE_REASONS, external=False, **_offer_form_options())


@bp.route("/outcomes/new", methods=["GET", "POST"])
@require("record_outcome")
def outcome_external():
    """Outcome for an offer made outside CompIntel."""
    company = require_company()
    if request.method == "POST":
        f = request.form
        try:
            if f.get("role") not in ALL_ROLES or f.get("level") not in LEVELS or f.get("city") not in CITIES:
                raise ValueError("Pick a role, level and city.")
            o = _parse_outcome(f)
            o["employment_type"] = _employment_type(f)
        except ValueError as exc:
            flash(str(exc) if "isoformat" not in str(exc) else "One of the dates is invalid.", "error")
            return redirect(url_for("wf.outcome_external"))
        oid = _insert_external_outcome(company, f["role"], f["level"], f["city"], o)
        shared = record_observation(company, oid)
        audit("record_external_outcome", "outcome", oid, {**o, "shared": shared}, company["id"])
        db.commit()
        flash("External outcome recorded.", "success")
        return redirect(url_for("wf.insights"))
    return render_template("workflow/outcome.html", req=None, pkg=None, existing=None, statuses=OUTCOME_STATUSES,
                           reasons=DECLINE_REASONS, external=True, **_offer_form_options())


def _employment_type(form):
    emp = form.get("employment_type") or "Full-time"
    if emp not in EMPLOYMENT_TYPES:
        raise ValueError("Unrecognised employment type.")
    return emp


def _insert_external_outcome(company, role, level, city, o):
    now = db.now_iso()
    profile, _snap, _age = current_profile(company)
    return db.execute(
        "INSERT INTO outcomes (company_id, request_id, external, role, level, city, status, sent_date, verbal_acceptance_date, "
        "decision_date, final_cash_ctc, final_esop_options, competing_offer, decline_reason, notes, created_by, created_at, "
        "updated_at, employment_type, health_band, initial_cash_ctc, initial_esop_options, counter_cash_ctc, "
        "counter_esop_options) VALUES (?, NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (company["id"], role, level, city, o["status"], o["sent_date"], o["verbal_acceptance_date"], o["decision_date"],
         o["final_cash_ctc"], o["final_esop_options"], o["competing_offer"], o["decline_reason"], o["notes"],
         current_user()["id"], now, now, o.get("employment_type", "Full-time"), profile["health_band"],
         o.get("initial_cash_ctc"), o.get("initial_esop_options"), o.get("counter_cash_ctc"),
         o.get("counter_esop_options")))


# The four telemetry columns are last so older 12-column exports and pasted rows still import.
OUTCOME_CSV_COLUMNS = ["role", "level", "city", "employment_type", "status", "sent_date", "decision_date",
                       "final_cash_ctc", "final_esop_options", "competing_offer", "decline_reason", "notes",
                       "initial_cash_ctc", "initial_esop_options", "counter_cash_ctc", "counter_esop_options"]


@bp.route("/outcomes/template.csv")
def outcome_template():
    return Response(",".join(OUTCOME_CSV_COLUMNS) +
                    "\nSoftware Engineer,Mid,Bengaluru,Full-time,accepted,2026-09-01,2026-09-05,22.5,8000,,,,21,8000,24,\n",
                    mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=compintel-outcomes-template.csv"})


@bp.route("/outcomes/import", methods=["POST"])
@require("record_outcome")
def outcome_import():
    company = require_company()
    file = request.files.get("file")
    pasted = (request.form.get("csv_text") or "").strip()
    if (not file or not file.filename) and not pasted:
        flash("Choose a CSV file or paste CSV rows.", "error")
        return redirect(url_for("wf.outcome_external"))
    try:
        if file and file.filename:
            text = file.read(1_000_000).decode("utf-8-sig")
        else:
            text = pasted[:1_000_000]
            if not text.lower().startswith("role,"):  # pasted rows without the header line
                text = ",".join(OUTCOME_CSV_COLUMNS) + "\n" + text
        reader = csv.DictReader(io.StringIO(text))
        parsed = []
        for i, row in enumerate(reader, start=2):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if row.get("role") not in ALL_ROLES or row.get("level") not in LEVELS or row.get("city") not in CITIES:
                raise ValueError(f"row {i}: unknown role, level or city")
            try:
                o = _parse_outcome(row)
                o["employment_type"] = _employment_type(row)
                parsed.append((row, o))
            except ValueError as exc:
                raise ValueError(f"row {i}: {exc}")
    except (UnicodeDecodeError, ValueError) as exc:
        flash(f"Nothing imported - {exc}.", "error")
        return redirect(url_for("wf.outcome_external"))
    shared = 0
    for row, o in parsed:
        oid = _insert_external_outcome(company, row["role"], row["level"], row["city"], o)
        shared += record_observation(company, oid)
    audit("import_outcomes", "outcomes", None, {"rows": len(parsed), "shared": shared}, company["id"])
    db.commit()
    flash(f"Imported {len(parsed)} outcomes ({shared} shared anonymously).", "success")
    return redirect(url_for("wf.insights"))


@bp.route("/offers.csv")
@require("export_benchmark")
def offers_export():
    company = require_company()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["candidate_pseudo_id", "role", "level", "city", "status", "p25", "p50", "p75", "target", "confidence",
                "submitted_cash_ctc", "final_cash_ctc", "outcome", "created_at"])
    for r in db.query("SELECT * FROM offer_requests WHERE company_id = ? ORDER BY id", (company["id"],)):
        bm = load_bm(r["benchmark"])
        sub = latest_submission(r["id"])
        cash = db.loads(sub["snapshot"])["package"]["cash_ctc"] if sub else None
        o = db.query("SELECT * FROM outcomes WHERE request_id = ?", (r["id"],), one=True)
        w.writerow([r["candidate_pseudo_id"], r["role"], r["level"], r["city"], r["status"], bm["p25"], bm["p50"], bm["p75"],
                    bm["target"], bm["confidence"]["level"], cash, o["final_cash_ctc"] if o else None,
                    o["status"] if o else None, r["created_at"]])
    audit("export_offers_csv", "offers", None, {}, company["id"])
    db.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=compintel-offers.csv"})


# --------------------------------------------------------------------------
# Approvals queue & insights
# --------------------------------------------------------------------------

@bp.route("/approvals")
def approvals():
    require_company()
    user = current_user()
    mine = _pending_for_role(user["role"]) if user["role"] in APPROVER_ORDER else []
    all_pending = []
    for role in APPROVER_ORDER:
        for req, sub, state in _pending_for_role(role):
            all_pending.append((req, sub, state))
    return render_template("workflow/approvals.html", mine=mine, all_pending=all_pending)


@bp.route("/insights")
def insights():
    company = require_company()
    rows = db.query("SELECT * FROM outcomes WHERE company_id = ?", (company["id"],))
    dim = request.args.get("by", "role")
    if dim not in ("role", "level", "city", "health_band", "employment_type", "segment"):
        dim = "role"
    groups = {}
    for r in rows:
        key = r[dim] if dim != "segment" else ("In-app offers" if not r["external"] else "External offers")
        key = key or "Not recorded"
        g = groups.setdefault(key, {"total": 0, "accepted": 0, "declined": 0, "withdrawn": 0, "sent": 0,
                                    "in_range": 0, "with_rec": 0, "final": []})
        g["total"] += 1
        g[r["status"]] += 1
        if r["status"] == "accepted" and r["final_cash_ctc"] is not None:
            g["final"].append(r["final_cash_ctc"])
            if r["rec_p25"] is not None:
                g["with_rec"] += 1
                g["in_range"] += 1 if r["rec_p25"] <= r["final_cash_ctc"] <= r["rec_p75"] else 0
    table = []
    for key, g in sorted(groups.items()):
        decided = g["accepted"] + g["declined"]
        table.append({"key": key, **g, "acceptance": round(g["accepted"] / decided * 100) if decided else None,
                      "coverage": round(g["in_range"] / g["with_rec"] * 100) if g["with_rec"] else None,
                      "median_final": round(statistics.median(g["final"]), 2) if g["final"] else None})
    reasons = {}
    for r in rows:
        if r["decline_reason"]:
            reasons[r["decline_reason"]] = reasons.get(r["decline_reason"], 0) + 1
    contributed = db.query("SELECT COUNT(*) c FROM benchmark_observations", one=True)["c"]
    kpis = _kpis(company["id"])
    settings = settings_for(company)
    return render_template("workflow/insights.html", table=table, dim=dim, kpis=kpis,
                           reasons=sorted(reasons.items(), key=lambda kv: -kv[1]), contributed=contributed,
                           share=settings["share_outcomes"], telemetry=_telemetry(rows),
                           readiness=_pilot_readiness(kpis, settings), mode=company["mode"])


def _telemetry(rows) -> dict:
    """Negotiation signals from the company's own outcomes (PRD 9.3)."""
    decided = [r for r in rows if r["status"] in ("accepted", "declined", "withdrawn")]
    declined = [r for r in rows if r["status"] == "declined"]
    countered = [r for r in decided if r["counter_cash_ctc"] is not None]
    uplifts = [(r["final_cash_ctc"] / r["initial_cash_ctc"] - 1) * 100 for r in rows
               if r["status"] == "accepted" and r["final_cash_ctc"] and r["initial_cash_ctc"]]
    counter_gaps = [(r["counter_cash_ctc"] / r["initial_cash_ctc"] - 1) * 100 for r in countered
                    if r["initial_cash_ctc"]]
    return {
        "decided": len(decided),
        "counter_rate": round(len(countered) / len(decided) * 100) if decided else None,
        "countered": len(countered),
        "median_counter_ask_pct": round(statistics.median(counter_gaps), 1) if counter_gaps else None,
        "median_uplift_pct": round(statistics.median(uplifts), 1) if uplifts else None,
        "uplift_n": len(uplifts),
        "decline_reason_capture": (round(sum(1 for r in declined if r["decline_reason"]) / len(declined) * 100)
                                   if declined else None),
        "with_initial": sum(1 for r in rows if r["initial_cash_ctc"] is not None),
    }


PILOT_TARGETS = {"records": 500, "records_stretch": 1000, "contributors": 10, "contributors_stretch": 20,
                 "outcome_capture": 80, "coverage_min_n": 30, "coverage_lo": 40, "coverage_hi": 60,
                 "decline_reason_capture": 80}


def _pilot_readiness(kpis, settings) -> dict:
    """PRD 18, Phase 1 exit criteria, measured from the benchmark network and this workspace."""
    net = pilot.network_stats(settings)
    t = PILOT_TARGETS
    items = [
        {"label": "Verified offer records in the network", "value": net["records"],
         "target": f"{PILOT_TARGETS['records']}-{PILOT_TARGETS['records_stretch']}",
         "met": net["records"] >= PILOT_TARGETS["records"]},
        {"label": "Contributing companies", "value": net["contributors"],
         "target": f"{PILOT_TARGETS['contributors']}-{PILOT_TARGETS['contributors_stretch']}",
         "met": net["contributors"] >= PILOT_TARGETS["contributors"]},
        {"label": "Offers with a recorded outcome (this workspace)",
         "value": f"{kpis['outcome_coverage']}%" if kpis["outcome_coverage"] is not None else "-",
         "target": f"{PILOT_TARGETS['outcome_capture']}%",
         "met": (kpis["outcome_coverage"] or 0) >= PILOT_TARGETS["outcome_capture"]},
        # A calibrated P25-P75 range holds about half of accepted offers, and a percentage
        # from a handful of offers isn't a measurement yet - so both are required.
        {"label": "P25-P75 coverage of accepted in-app offers (calibrated is ~50%)",
         "value": (f"{kpis['in_range_pct']}% of {kpis['in_range_n']}" if kpis["in_range_pct"] is not None
                   else "no accepted offers with a range yet"),
         "target": f"{t['coverage_lo']}-{t['coverage_hi']}% on {t['coverage_min_n']}+",
         "met": (kpis["in_range_pct"] is not None and kpis["in_range_n"] >= t["coverage_min_n"]
                 and t["coverage_lo"] <= kpis["in_range_pct"] <= t["coverage_hi"])},
        {"label": "Decline reasons captured (benchmark network)",
         "value": (f"{net['decline_reason_capture']}% of {net['declined']} declines" if net["decline_reason_capture"]
                   is not None else "no declines yet"),
         "target": f"{t['decline_reason_capture']}%",
         "met": (net["decline_reason_capture"] or 0) >= t["decline_reason_capture"]},
    ]
    return {"criteria": items, "network": net}


# --------------------------------------------------------------------------
# Benchmark network: design-partner data for pilot / production benchmarks
# --------------------------------------------------------------------------

@bp.route("/network")
@require("view_benchmark")
def network_page():
    company = require_company()
    settings = settings_for(company)
    batches = db.query("SELECT b.*, u.name AS user_name FROM network_import_batches b LEFT JOIN users u "
                       "ON u.id = b.imported_by ORDER BY b.id DESC")
    by_source = db.query("SELECT source, status, COUNT(*) n FROM benchmark_observations GROUP BY source, status "
                         "ORDER BY source, status")
    coverage = db.query("SELECT role, level, COUNT(*) n, COUNT(DISTINCT contributor_key) contributors "
                        "FROM benchmark_observations WHERE status = 'accepted' AND final_cash_ctc IS NOT NULL "
                        "GROUP BY role, level ORDER BY n DESC LIMIT 25")
    return render_template("workflow/network.html", company=company, settings=settings, batches=batches,
                           by_source=by_source, coverage=coverage, stats=pilot.network_stats(settings),
                           consent_bases=pilot.CONSENT_BASES, columns=pilot.PARTNER_CSV_COLUMNS,
                           ndr_bands=pilot.NDR_BANDS)


@bp.route("/network/template.csv")
@require("view_benchmark")
def network_template():
    return Response(pilot.partner_template(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=compintel-partner-offers-template.csv"})


@bp.route("/network/import", methods=["POST"])
@require("manage_network_data")
def network_import():
    """Admin imports anonymised offer records from design partners. Requires an
    explicit consent attestation; company refs are hashed, never stored."""
    company = require_company()
    basis = request.form.get("consent_basis")
    if basis not in pilot.CONSENT_BASES or request.form.get("consent_attested") != "on":
        flash("Choose the consent basis and confirm the attestation before importing.", "error")
        return redirect(url_for("wf.network_page"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file.", "error")
        return redirect(url_for("wf.network_page"))
    raw = file.read(5_000_000)
    sha = hashlib.sha256(raw).hexdigest()
    if db.query("SELECT id FROM network_import_batches WHERE file_sha256 = ?", (sha,), one=True):
        flash("This exact file has already been imported.", "error")
        return redirect(url_for("wf.network_page"))
    secret = current_app.secret_key
    secret = secret if isinstance(secret, str) else secret.decode("latin-1")
    try:
        records = pilot.parse_partner_csv(raw.decode("utf-8-sig"), secret)
    except (UnicodeDecodeError, pilot.ImportError_) as exc:
        flash(f"Nothing imported - {exc}.", "error")
        return redirect(url_for("wf.network_page"))
    existing = {r["record_hash"] for r in db.query("SELECT record_hash FROM benchmark_observations "
                                                    "WHERE record_hash IS NOT NULL")}
    fresh = [r for r in records if r["record_hash"] not in existing]
    fresh = list({r["record_hash"]: r for r in fresh}.values())  # duplicates inside the file too
    user = current_user()
    batch_id = db.execute(
        "INSERT INTO network_import_batches (imported_by, imported_at, filename, file_sha256, rows, contributors, "
        "consent_basis, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], db.now_iso(), file.filename[:200], sha, len(fresh), len({r["contributor_key"] for r in fresh}),
         pilot.CONSENT_BASES[basis], (request.form.get("note") or "").strip()[:300] or None))
    cols = ["contributor_key", "role", "level", "city_tier", "funding_stage", "employment_type", "employee_band",
            "arr_band", "ndr_band", "status", "observed_month", "initial_cash_ctc", "variable_pct",
            "initial_esop_options", "counter_cash_ctc", "counter_esop_options", "final_cash_ctc",
            "final_esop_options", "decline_reason", "competing_offer", "record_hash"]
    for r in fresh:
        db.execute(f"INSERT INTO benchmark_observations (batch_id, source, consent_basis, {', '.join(cols)}) "
                   f"VALUES (?, 'partner_import', ?, {', '.join('?' for _ in cols)})",
                   (batch_id, pilot.CONSENT_BASES[basis], *[r[c] for c in cols]))
        streaming.queue_observation(r)  # coalesced: one recompute per cohort, after commit
    audit("import_network_batch", "network_import_batches", batch_id,
          {"rows": len(fresh), "skipped_duplicates": len(records) - len(fresh), "file_sha256": sha,
           "consent_basis": basis}, company["id"])
    db.commit()
    flash(f"Imported {len(fresh)} partner offer records"
          + (f" ({len(records) - len(fresh)} duplicates skipped)" if len(records) != len(fresh) else "") + ".",
          "success")
    return redirect(url_for("wf.network_page"))


@bp.route("/network/batches/<int:batch_id>/remove", methods=["POST"])
@require("manage_network_data")
def network_batch_remove(batch_id):
    """Deletion workflow (PRD 11.4): withdraws a partner's batch from all future
    benchmarks. Submissions that used those rows keep their frozen snapshot."""
    company = require_company()
    batch = db.query("SELECT * FROM network_import_batches WHERE id = ?", (batch_id,), one=True)
    if batch is None or batch["removed_at"]:
        abort(404, description="Batch not found or already removed.")
    n = db.query("SELECT COUNT(*) c FROM benchmark_observations WHERE batch_id = ?", (batch_id,), one=True)["c"]
    for r in db.query("SELECT DISTINCT role, level, city_tier, funding_stage FROM benchmark_observations "
                      "WHERE batch_id = ?", (batch_id,)):
        streaming.queue_observation(r)
    db.execute("DELETE FROM benchmark_observations WHERE batch_id = ?", (batch_id,))
    db.execute("UPDATE network_import_batches SET removed_at = ?, removed_by = ? WHERE id = ?",
               (db.now_iso(), current_user()["id"], batch_id))
    audit("remove_network_batch", "network_import_batches", batch_id, {"rows_removed": n}, company["id"])
    db.commit()
    flash(f"Removed {n} records from batch #{batch_id}. Future benchmarks no longer use them.", "success")
    return redirect(url_for("wf.network_page"))


# --------------------------------------------------------------------------
# Settings, users, retention
# --------------------------------------------------------------------------

@bp.route("/settings")
def settings_page():
    company = require_company()
    rules = db.query("SELECT * FROM approval_rules WHERE company_id = ? ORDER BY id", (company["id"],))
    users = db.query("SELECT * FROM users ORDER BY id")
    return render_template("workflow/settings.html", settings=settings_for(company), fields=SETTING_FIELDS, rules=rules,
                           users=users, condition_kinds=CONDITION_KINDS, exception_kinds=EXCEPTION_KINDS,
                           approver_roles=APPROVER_ORDER, ruleset=RULESET, disclaimer=DISCLAIMER,
                           modes=WORKSPACE_MODES, company=company,
                           usable_cohorts=pilot.usable_cohorts(settings_for(company)))


@bp.route("/settings/mode", methods=["POST"])
@require("manage_users")
def settings_mode():
    """Workspace data mode. Leaving demo mode switches the synthetic model off for
    every new benchmark; benchmarks already made stay labelled as demo."""
    company = require_company()
    mode = request.form.get("mode")
    if mode not in WORKSPACE_MODES:
        abort(400, description="Unknown workspace mode.")
    usable = pilot.usable_cohorts(settings_for(company))
    if mode != "demo" and company["mode"] == "demo" and usable == 0 and request.form.get("confirm_no_data") != "on":
        flash("No cohort in the benchmark network clears the privacy thresholds yet, so in pilot or production mode "
              "every offer would be entered manually with no market range. Tick the confirmation to switch anyway.",
              "error")
        return redirect(url_for("wf.settings_page"))
    if mode != company["mode"]:
        db.execute("UPDATE companies SET mode = ?, updated_at = ? WHERE id = ?", (mode, db.now_iso(), company["id"]))
        audit("change_workspace_mode", "company", company["id"], {"from": company["mode"], "to": mode}, company["id"])
        db.commit()
        flash(f"Workspace is now in {mode} mode. " + ("New benchmarks use the synthetic demo model."
              if mode == "demo" else "New benchmarks won't use the synthetic model; packages are entered manually."),
              "success")
    return redirect(url_for("wf.settings_page"))


@bp.route("/settings/policy", methods=["POST"])
@require("manage_policy")
def settings_policy():
    company = require_company()
    current = settings_for(company)
    new = dict(current)
    try:
        for key, (label, typ, lo, hi) in SETTING_FIELDS.items():
            raw = request.form.get(key)
            if raw in (None, ""):
                continue
            value = typ(float(raw))
            if not (lo <= value <= hi):
                raise ValueError(f"{label} must be between {lo:g} and {hi:g}.")
            new[key] = value
    except ValueError as exc:
        flash(str(exc) if "between" in str(exc) else "Policy values must be numbers.", "error")
        return redirect(url_for("wf.settings_page"))
    if can("manage_retention"):
        new["share_outcomes"] = request.form.get("share_outcomes") == "on"
    if request.form.get("health_positioning_field"):  # only when the form actually showed the checkbox
        new["health_positioning"] = request.form.get("health_positioning") == "on"
    changed = {k: [current[k], v] for k, v in new.items() if current.get(k) != v}
    db.execute("UPDATE companies SET settings = ?, updated_at = ? WHERE id = ?", (db.dumps(new), db.now_iso(), company["id"]))
    audit("update_policy", "company", company["id"], {"changed": changed}, company["id"])
    db.commit()
    flash("Policy saved. In-flight and past submissions keep the policy they were checked against.", "success")
    return redirect(url_for("wf.settings_page"))


@bp.route("/settings/rules", methods=["POST"])
@require("manage_policy")
def settings_rules():
    company = require_company()
    action = request.form.get("action")
    if action == "toggle":
        rid = request.form.get("rule_id", type=int)
        rule = db.query("SELECT * FROM approval_rules WHERE id = ? AND company_id = ?", (rid, company["id"]), one=True)
        if rule:
            db.execute("UPDATE approval_rules SET active = ? WHERE id = ?", (0 if rule["active"] else 1, rid))
            audit("toggle_approval_rule", "approval_rule", rid, {"active": not rule["active"]}, company["id"])
    else:
        name = (request.form.get("name") or "").strip()
        kind = request.form.get("condition_kind")
        value = (request.form.get("condition_value") or "").strip()
        role = request.form.get("approver_role")
        if not name or kind not in CONDITION_KINDS or role not in APPROVER_ORDER:
            flash("Rule needs a name, a condition and an approver role.", "error")
            return redirect(url_for("wf.settings_page"))
        if kind == "exception" and value not in EXCEPTION_KINDS:
            flash("For an exception rule, the value must be one of: " + ", ".join(EXCEPTION_KINDS), "error")
            return redirect(url_for("wf.settings_page"))
        if kind == "cash_above":
            try:
                float(value)
            except ValueError:
                flash("Cash threshold must be a number (LPA).", "error")
                return redirect(url_for("wf.settings_page"))
        rid = db.execute("INSERT INTO approval_rules (company_id, name, condition_kind, condition_value, approver_role) "
                         "VALUES (?, ?, ?, ?, ?)", (company["id"], name[:80], kind, value[:200], role))
        audit("add_approval_rule", "approval_rule", rid, {"name": name, "kind": kind, "value": value, "role": role},
              company["id"])
    db.commit()
    flash("Approval rules updated. Existing submissions keep the route they were given.", "success")
    return redirect(url_for("wf.settings_page"))


@bp.route("/settings/users", methods=["POST"])
@require("manage_users")
def settings_users():
    company = require_company()
    action = request.form.get("action")
    if action == "role":
        uid = request.form.get("user_id", type=int)
        role = request.form.get("role")
        if role not in ROLES:
            abort(400, description="Unknown role.")
        if uid == current_user()["id"] and role != "admin":
            flash("You can't remove your own admin role.", "error")
            return redirect(url_for("wf.settings_page"))
        db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
        audit("change_user_role", "user", uid, {"role": role}, company["id"])
    else:
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        role = request.form.get("role")
        domain = company["domain"]
        if not name or "@" not in email or role not in ROLES:
            flash("A new user needs a name, an email and a role.", "error")
            return redirect(url_for("wf.settings_page"))
        if domain and not email.endswith("@" + domain) and not email.endswith("@demo.compintel"):
            flash(f"Invite only work emails on the company domain (@{domain}).", "error")
            return redirect(url_for("wf.settings_page"))
        if db.query("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
            flash("That email already has an account.", "error")
            return redirect(url_for("wf.settings_page"))
        uid = db.execute("INSERT INTO users (name, email, role) VALUES (?, ?, ?)", (name[:80], email, role))
        audit("invite_user", "user", uid, {"email": email, "role": role}, company["id"])
    db.commit()
    flash("Users updated.", "success")
    return redirect(url_for("wf.settings_page"))


@bp.route("/settings/purge", methods=["POST"])
@require("manage_retention")
def settings_purge():
    """Removes candidate names from closed offers past the retention period.
    Snapshots never contained names, so history stays intact and pseudonymous."""
    company = require_company()
    days = int(settings_for(company)["retention_days"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.query("SELECT id FROM offer_requests WHERE company_id = ? AND candidate_name IS NOT NULL AND "
                    "status IN ('accepted','declined','withdrawn','rejected') AND updated_at < ?", (company["id"], cutoff))
    for r in rows:
        db.execute("UPDATE offer_requests SET candidate_name = NULL WHERE id = ?", (r["id"],))
    audit("purge_candidate_names", "offer_requests", None, {"rows": len(rows), "older_than_days": days}, company["id"])
    db.commit()
    flash(f"Removed candidate names from {len(rows)} closed offers older than {days} days.", "success")
    return redirect(url_for("wf.settings_page"))


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------

def _audit_rows():
    start = request.args.get("from") or (date.today() - timedelta(days=30)).isoformat()
    end = request.args.get("to") or date.today().isoformat()
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError:
        abort(400, description="Dates must be YYYY-MM-DD.")
    rows = db.query("SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id "
                    "WHERE substr(a.ts, 1, 10) BETWEEN ? AND ? ORDER BY a.id DESC", (start, end))
    return rows, start, end


@bp.route("/audit")
@require("view_audit")
def audit_page():
    rows, start, end = _audit_rows()
    integrity = None
    if request.args.get("verify"):
        integrity = verify_all(db.get_db())
        audit("verify_integrity", "audit_log", None, {"ok": integrity["ok"], "entries": integrity["audit_entries"],
                                                     "problems": integrity["problems"][:20]})
        db.commit()
    return render_template("workflow/audit.html", rows=rows[:500], total=len(rows), start=start, end=end,
                           integrity=integrity)


@bp.route("/audit.csv")
@require("view_audit")
def audit_export():
    rows, start, end = _audit_rows()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "ts", "company_id", "user_id", "user", "role", "action", "entity", "entity_id", "detail",
                "context", "prev_hash", "entry_hash"])
    for r in rows:
        w.writerow([r["id"], r["ts"], r["company_id"], r["user_id"], r["user_name"], r["user_role"], r["action"],
                    r["entity"], r["entity_id"], r["detail"], r["context"], r["prev_hash"], r["entry_hash"]])
    audit("export_audit_log", "audit_log", None, {"from": start, "to": end, "rows": len(rows)})
    db.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=compintel-audit-{start}-to-{end}.csv"})


# --------------------------------------------------------------------------
# Public-site leads and launch metrics (Website & Brand PRD 14, 17)
# --------------------------------------------------------------------------

@bp.route("/pilot-applications")
@require("view_audit")
def pilot_applications():
    from marketing import launch_metrics  # the site module imports this package; avoid a cycle at load time

    rows = db.query("SELECT * FROM pilot_applications ORDER BY id DESC LIMIT 200")
    audit("view_pilot_applications", "pilot_applications", None, {"rows": len(rows)})
    db.commit()
    return render_template("workflow/pilot_applications.html", rows=rows, metrics=launch_metrics())
