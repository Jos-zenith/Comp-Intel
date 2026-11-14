"""
Public website: marketing pages, the sample offer benchmark, the India CTC and
ESOP calculators, and the pilot application (Website, Brand & First-Run PRD v1.0).

The primary conversion is "Generate a sample offer benchmark", not sign-up. The
sample asks four questions (role, level, city, funding stage), runs the same
demo model the workflow uses, and leads with the trade-off: what moving from
the recommended package to an aggressive one costs in cash and runway. Every
number here comes from the synthetic demo model and is labelled as such.

Nothing on these pages needs an account. The only things stored are pilot
applications (which the applicant submits on purpose) and anonymous funnel
events with no IP address, user agent or free text.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from flask import Blueprint, flash, redirect, render_template, request, url_for

import saas_metrics as sm
from saas_domain import CITIES, FUNDING_STAGES, LEVEL_BANDS, ROLE_ARCHETYPES
from workflow import affordability as afford
from workflow import db
from workflow import esop as esop_mod
from workflow.auth import csrf_token, demo_mode_enabled, verify_csrf
from workflow.checks import DISCLAIMER, RULESET, structure_ctc
from workflow.take_home import TAX_RULESET, estimate_take_home

bp = Blueprint("mk", __name__)

LEVELS = [b[2] for b in LEVEL_BANDS]
# Representative experience per level, so a visitor picks a level rather than typing years.
LEVEL_YEARS = {"Junior": 1.0, "Mid": 3.5, "Senior": 7.0, "Lead / Principal": 11.0}
ROLES = ROLE_ARCHETYPES

# An illustrative company for the sample, so runway impact shows before anyone enters
# their own numbers: Rs.30 Cr cash, Rs.1.6 Cr monthly burn (~19 months), Rs.4 Cr for 10 hires.
SAMPLE_CONTEXT = {"cash_balance_cr": 30.0, "monthly_burn_lakh": 160.0, "hiring_budget_cr": 4.0, "planned_hires": 10}
SAMPLE_FMV, SAMPLE_FDS = 250.0, 10_000_000
# Cash-versus-equity tilt per scenario: the conservative package carries more ESOP.
SCENARIOS = [("Conservative", 35.0, 1.25), ("Recommended", None, 1.0), ("Aggressive", 75.0, 0.75)]

PERSONAS = ["Founder / CEO", "Finance / CFO", "Head of People / HRBP", "Recruiter / TA", "Compensation analyst", "Other"]
EMPLOYEE_BANDS = ["Under 30", "30-99", "100-199", "200-500", "500+"]
HIRES_PER_YEAR = ["Under 5", "5-10", "11-25", "26-50", "50+"]
FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com", "icloud.com",
             "rediffmail.com", "proton.me", "protonmail.com", "aol.com"}
EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
EVENTS = {"sample_benchmark", "ctc_calculator", "esop_calculator", "pilot_application"}

_predict_fn = None


def register_site(app, predict_fn):
    global _predict_fn
    _predict_fn = predict_fn
    app.register_blueprint(bp)

    @app.errorhandler(404)
    def _not_found(_exc):
        return render_template("site/404.html"), 404


@bp.app_context_processor
def _site_context():
    return {"workflow_enabled": demo_mode_enabled(), "site_csrf_token": csrf_token}


@bp.before_request
def _csrf():
    verify_csrf()


def track(event: str, detail: dict | None = None):
    """Anonymous funnel event: no IP, no user agent, no free text."""
    if event not in EVENTS:
        return
    try:
        db.execute("INSERT INTO site_events (ts, event, detail) VALUES (?, ?, ?)",
                   (db.now_iso(), event, db.dumps(detail or {})))
        db.commit()
    except Exception:  # analytics must never break a page
        pass


# --------------------------------------------------------------------------
# Sample benchmark
# --------------------------------------------------------------------------

def approx_percentile(cash: float, p25: float, p50: float, p75: float) -> int:
    """Where a cash figure sits in the market distribution. Exact at the quartiles:
    linear between them (the inverse of sm.interpolate_percentile), and outside them
    a lognormal tail fitted to that side's half-range, since model ranges are often
    skewed. Approximate by construction; shown as 'about P__'."""
    if min(p25, p50, p75, cash) <= 0 or not (p25 <= p50 <= p75) or p75 == p25:
        return 50
    q = 0.6745  # z-score of the quartiles
    if p25 <= cash <= p50:
        pct = 25 + 25 * (cash - p25) / (p50 - p25) if p50 > p25 else 50
    elif p50 < cash <= p75:
        pct = 50 + 25 * (cash - p50) / (p75 - p50)
    else:
        if cash < p25:
            sigma = (math.log(p50) - math.log(p25)) / q if p50 > p25 else (math.log(p75) - math.log(p25)) / (2 * q)
            z = -q + (math.log(cash) - math.log(p25)) / sigma
        else:
            sigma = (math.log(p75) - math.log(p50)) / q if p75 > p50 else (math.log(p75) - math.log(p25)) / (2 * q)
            z = q + (math.log(cash) - math.log(p75)) / sigma
        pct = 50 * (1 + math.erf(z / math.sqrt(2)))
    return int(round(max(1, min(99, pct))))


def _validate(role, level, city, stage):
    all_roles = {r for roles in ROLES.values() for r in roles}
    if role not in all_roles or level not in LEVELS or city not in CITIES or stage not in FUNDING_STAGES:
        raise ValueError("Pick a role, level, city and funding stage from the lists.")


def _context_from_args(args) -> tuple[dict, bool]:
    """Optional company context from the query string; falls back to the sample company."""
    ctx = dict(SAMPLE_CONTEXT)
    custom = False
    for key, lo, hi in (("cash_balance_cr", 0, 100000), ("monthly_burn_lakh", 0, 1_000_000),
                        ("hiring_budget_cr", 0, 10000), ("planned_hires", 1, 100000)):
        raw = (args.get(key) or "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                raise ValueError("Company numbers must be numbers.")
            if not (lo <= value <= hi):
                raise ValueError("One of the company numbers is out of range.")
            ctx[key] = value
            custom = True
    return ctx, custom


def sample_benchmark(role: str, level: str, city: str, stage: str, context: dict | None = None) -> dict:
    _validate(role, level, city, stage)
    context = context or SAMPLE_CONTEXT
    result = _predict_fn({
        "role": role, "years_experience": LEVEL_YEARS[level], "city": city, "funding_stage": stage,
        "target_market": "Domestic", "skill_category": "Core", "education_pedigree": "Recognised university",
        "pricing_model": "Subscription",
    }, apply_signals=False)
    p25, p50, p75 = result["p25"], result["p50"], result["p75"]
    ctx = afford.context_from_inputs(context)
    pos = result["positioning"]
    scenarios = []
    for name, pct, esop_tilt in SCENARIOS:
        cash = result["target"] if pct is None else sm.interpolate_percentile(p25, p50, p75, pct)
        grant_lakh = result["esop_annualized_lpa"] * 4 * esop_tilt
        options = int(round(grant_lakh * 1e5 / SAMPLE_FMV, -1))
        impact = afford.offer_impact(ctx, cash)
        scenarios.append({
            "name": name, "cash": round(cash, 1), "percentile": approx_percentile(cash, p25, p50, p75),
            "esop_options": options, "esop_pct": round(options / SAMPLE_FDS * 100, 3),
            "annual_cost_lakh": impact["annual_cost_lakh"], "monthly_burn_lakh": impact["monthly_burn_increase_lakh"],
            "runway_after": impact["runway_after"], "runway_delta": impact["runway_delta_months"],
            "plan_variance_pct": impact["plan_variance_pct"],
        })
    # The aha moment compares the two ends of the decision (Conservative vs Aggressive), and
    # pairs it with what the recommended hire costs in runway on its own. Numbers stay honest:
    # for a company with a large burn the marginal runway difference can be small.
    cons, rec, agg = scenarios
    aha = {
        "from_cash": cons["cash"], "to_cash": agg["cash"],
        "cost_delta_lakh": round(agg["annual_cost_lakh"] - cons["annual_cost_lakh"], 1),
        "runway_delta": (round(agg["runway_after"] - cons["runway_after"], 2)
                         if agg["runway_after"] is not None and cons["runway_after"] is not None else None),
        "from_pct": cons["percentile"], "to_pct": agg["percentile"],
        "hire_runway_delta": rec["runway_delta"],
    }
    return {
        "role": role, "level": level, "city": city, "stage": stage,
        "p25": round(p25, 1), "p50": round(p50, 1), "p75": round(p75, 1),
        "target": round(result["target"], 1), "target_percentile": pos["target_percentile"],
        "health_score": round(result["health_score"]), "health_band": result["health_band"],
        "stance": pos["stance"], "percentile_shift": pos["percentile_shift"],
        "scenarios": scenarios, "aha": aha, "context": ctx,
        "drivers": [c for c in result["contributions"][:3]],
    }


@lru_cache(maxsize=1)
def hero_sample() -> dict | None:
    """The live offer-decision card on the homepage. Deterministic, so cached."""
    try:
        return sample_benchmark("Software Engineer", "Senior", "Bengaluru", "Series A")
    except Exception:
        return None


def render_home():
    return render_template("site/home.html", s=hero_sample(), roles=ROLES, levels=LEVELS, cities=list(CITIES),
                           stages=list(FUNDING_STAGES))


@bp.route("/sample-benchmark")
def sample():
    args = request.args
    form = {"role": args.get("role", ""), "level": args.get("level", ""), "city": args.get("city", ""),
            "stage": args.get("stage", "")}
    result, error, custom = None, None, False
    if all(form.values()):
        try:
            ctx, custom = _context_from_args(args)
            result = sample_benchmark(form["role"], form["level"], form["city"], form["stage"], ctx)
            result["custom"] = custom
            track("sample_benchmark", {**form, "custom_context": custom})
        except ValueError as exc:
            error = str(exc)
    elif any(form.values()):
        error = "Pick a role, level, city and funding stage."
    return render_template("site/sample.html", form=form, s=result, error=error, custom=custom, roles=ROLES,
                           levels=LEVELS, cities=list(CITIES), stages=list(FUNDING_STAGES), args=args)


# --------------------------------------------------------------------------
# Calculators
# --------------------------------------------------------------------------

@bp.route("/india-ctc")
def india_ctc():
    args = request.args
    result, error = None, None
    form = {"cash_ctc": args.get("cash_ctc", "24"), "variable": args.get("variable", "2.4"),
            "city": args.get("city", "Bengaluru")}
    if "cash_ctc" in args:
        try:
            cash, variable = float(form["cash_ctc"]), float(form["variable"] or 0)
            if not (1 <= cash <= 1000) or not (0 <= variable < cash) or form["city"] not in CITIES:
                raise ValueError
            structure = structure_ctc(cash, variable, form["city"])
            if not structure["fits"]:
                error = (f"Variable pay doesn't fit: after Basic (50% of CTC), employer EPF and gratuity, at most "
                         f"Rs.{structure['max_variable_lpa']:.2f} LPA is left for variable pay.")
            else:
                result = {"structure": structure, "take_home": estimate_take_home(structure)}
                track("ctc_calculator", {"city_tier": CITIES[form["city"]][0]})
        except ValueError:
            error = "Enter a cash CTC between 1 and 1,000 LPA and a variable amount smaller than it."
    return render_template("site/india_ctc.html", form=form, r=result, error=error, cities=list(CITIES),
                           ruleset=RULESET, tax=TAX_RULESET, disclaimer=DISCLAIMER)


@bp.route("/esop-scenarios")
def esop_page():
    args = request.args
    defaults = {"mode": "options", "options": "8000", "grant_pct": "0.08", "fmv": "250", "strike": "10",
                "fds": "10000000", "conservative": "0.5", "base": "2", "upside": "5", "dilution": "30", "tax": "30"}
    form = {k: args.get(k, v) for k, v in defaults.items()}
    result, error = None, None
    try:
        fds = float(form["fds"])
        if form["mode"] == "pct":
            options = int(round(float(form["grant_pct"]) / 100 * fds))
        else:
            options = int(float(form["options"]))
        settings = {"fmv_per_share_inr": float(form["fmv"]), "fully_diluted_shares": fds,
                    "esop_exit_conservative_x": float(form["conservative"]), "esop_exit_base_x": float(form["base"]),
                    "esop_exit_upside_x": float(form["upside"]), "esop_future_dilution_pct": float(form["dilution"]),
                    "esop_tax_rate_pct": float(form["tax"])}
        if options <= 0 or fds <= 0 or settings["fmv_per_share_inr"] <= 0 or not (0 <= settings["esop_future_dilution_pct"] < 95) \
                or not (0 <= settings["esop_tax_rate_pct"] <= 60) or float(form["strike"]) < 0:
            raise ValueError
        result = esop_mod.scenarios(options, settings, float(form["strike"]))
        if "options" in args or "grant_pct" in args:
            track("esop_calculator", {"mode": form["mode"]})
    except (ValueError, ZeroDivisionError):
        error = "Check the numbers: options, FMV and fully diluted shares must be positive; dilution under 95%."
    return render_template("site/esop.html", form=form, r=result, error=error)


# --------------------------------------------------------------------------
# Content pages
# --------------------------------------------------------------------------

PAGES = {
    "product": "site/product.html",
    "benchmarks": "site/benchmarks.html",
    "for_founders": "site/for_founders.html",
    "for_people": "site/for_people.html",
    "pricing": "site/pricing.html",
    "security": "site/security.html",
    "privacy": "site/privacy.html",
    "terms": "site/terms.html",
}


def _page(name):
    return lambda: render_template(PAGES[name])


for _url, _name in (("/product", "product"), ("/benchmarks", "benchmarks"), ("/for-founders", "for_founders"),
                    ("/for-people", "for_people"), ("/pricing", "pricing"), ("/security", "security"),
                    ("/privacy", "privacy"), ("/terms", "terms")):
    bp.add_url_rule(_url, _name, _page(_name))


# --------------------------------------------------------------------------
# Pilot application
# --------------------------------------------------------------------------

def parse_application(form) -> dict:
    name = (form.get("name") or "").strip()[:120]
    email = (form.get("work_email") or "").strip().lower()[:200]
    company = (form.get("company") or "").strip()[:160]
    if not name or not company:
        raise ValueError("Name and company are required.")
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid work email.")
    if email.split("@", 1)[1] in FREE_MAIL:
        raise ValueError("Use your work email - the pilot is for company teams, and we verify the company domain.")
    if form.get("persona") not in PERSONAS or form.get("employees") not in EMPLOYEE_BANDS \
            or form.get("hires") not in HIRES_PER_YEAR:
        raise ValueError("Pick your role, company size and hiring volume.")
    cities = [c for c in form.getlist("cities") if c in CITIES]
    if form.get("consent") != "on":
        raise ValueError("Tick the box so we can contact you about the pilot.")
    return {"name": name, "work_email": email, "company": company, "persona": form["persona"],
            "employees": form["employees"], "hires": form["hires"], "cities": cities,
            "notes": (form.get("notes") or "").strip()[:1000] or None}


@bp.route("/pilot", methods=["GET", "POST"])
def pilot():
    if request.method == "POST":
        try:
            app_ = parse_application(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("site/pilot.html", personas=PERSONAS, employees=EMPLOYEE_BANDS,
                                   hires=HIRES_PER_YEAR, cities=list(CITIES), f=request.form), 400
        db.execute("INSERT INTO pilot_applications (submitted_at, name, work_email, company, persona, employees, "
                   "hires, cities, notes, consent_contact, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                   (db.now_iso(), app_["name"], app_["work_email"], app_["company"], app_["persona"],
                    app_["employees"], app_["hires"], db.dumps(app_["cities"]), app_["notes"],
                    (request.args.get("from") or "")[:40] or None))
        db.commit()
        track("pilot_application", {"persona": app_["persona"], "employees": app_["employees"]})
        return redirect(url_for("mk.pilot_thanks"))
    return render_template("site/pilot.html", personas=PERSONAS, employees=EMPLOYEE_BANDS, hires=HIRES_PER_YEAR,
                           cities=list(CITIES), f={})


@bp.route("/pilot/thanks")
def pilot_thanks():
    return render_template("site/pilot_thanks.html")


def launch_metrics() -> dict:
    """Counts for the admin view. Bounce rate and time-on-page need a real analytics tool."""
    counts = {r["event"]: r["n"] for r in db.query("SELECT event, COUNT(*) n FROM site_events GROUP BY event")}
    samples = counts.get("sample_benchmark", 0)
    apps = counts.get("pilot_application", 0)
    return {"samples": samples, "ctc": counts.get("ctc_calculator", 0), "esop": counts.get("esop_calculator", 0),
            "applications": apps, "sample_to_pilot_pct": round(apps / samples * 100, 1) if samples else None}
