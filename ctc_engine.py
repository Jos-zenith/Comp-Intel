"""
India CTC & statutory compliance rule engine (standalone, no Flask).

Rule *parameters* live in versioned JSON files (rulesets/<family>/<version>.json);
rule *types* (how a parameterised rule is evaluated) live here. Changing a
percentage, a tolerance, a severity, the metro list or the order of checks is a
new ruleset file, not a code change, and an old offer can always be re-checked
against the exact version it was approved under.

Selection:
  ruleset_for(version="2026.04-r2")   that exact version (historical re-checks)
  ruleset_for(as_of=date(2026, 5, 1)) the newest version in force on that date
  active()                            the newest version in force today

Used by workflow/checks.py (offer checks), ctc_rules.py (constants for the
methodology pages) and the stateless POST /api/v1/ctc/validate endpoint, so the
recruiter UI, the PDF briefs and any external offer-letter generator all run the
same rules.

Decision support only - not legal, tax or payroll-audit advice.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

RULESET_DIR = Path(__file__).resolve().parent / "rulesets"
STATUS_ORDER = {"block": 0, "exception": 1, "warn": 2, "info": 3, "pass": 4}
RULE_TYPES = {"variable_fits", "wage_addback", "basic_min_pct_of_ctc", "ratio_of_basic", "components_within_ctc",
              "sums_to_ctc", "epf_wage_ceiling"}
REQUIRED_STRUCTURE_KEYS = {"basic_pct_of_ctc", "hra_pct_of_basic", "metro_cities", "employer_epf_pct_of_basic",
                           "gratuity_accrual_pct_of_basic", "lta_pct_of_basic", "default_variable_pct_of_ctc",
                           "wage_excluded_cap_pct"}
COMPONENT_LABELS = {"hra": "HRA", "employer_epf": "Employer EPF", "gratuity": "Gratuity"}


class RulesetError(ValueError):
    pass


@dataclass(frozen=True)
class Ruleset:
    id: str
    version: str
    status: str
    effective_from: date
    effective_to: date | None
    changes: str
    sources: tuple
    structure: dict = field(hash=False)
    rules: tuple = field(hash=False)
    path: str = ""

    def meta(self) -> dict:
        """The shape the workflow has always stored with a submission (CR-04)."""
        return {"id": self.id, "version": self.version, "effective_from": self.effective_from.isoformat(),
                "changes": self.changes, "sources": list(self.sources), "status": self.status}

    def is_metro(self, city: str) -> bool:
        return city in self.structure["metro_cities"]

    def hra_pct(self, city: str) -> float:
        return self.structure["hra_pct_of_basic"]["metro" if self.is_metro(city) else "non_metro"]

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.effective_to is None or on < self.effective_to)


def _version_key(r: Ruleset):
    m = re.match(r"^(\d{4})\.(\d{2})-r(\d+)$", r.version)
    return (r.effective_from, tuple(int(p) for p in m.groups()) if m else (0, 0, 0), r.version)


def _parse(path: Path) -> Ruleset:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RulesetError(f"{path.name}: unreadable ruleset ({exc})")
    for key in ("id", "version", "effective_from", "structure", "rules"):
        if key not in raw:
            raise RulesetError(f"{path.name}: missing '{key}'")
    missing = REQUIRED_STRUCTURE_KEYS - set(raw["structure"])
    if missing:
        raise RulesetError(f"{path.name}: structure is missing {', '.join(sorted(missing))}")
    seen = set()
    for rule in raw["rules"]:
        if rule.get("type") not in RULE_TYPES:
            raise RulesetError(f"{path.name}: unknown rule type {rule.get('type')!r}")
        if rule.get("on_fail") not in STATUS_ORDER:
            raise RulesetError(f"{path.name}: rule {rule.get('id')!r} has an invalid on_fail severity")
        if rule.get("id") in seen:
            raise RulesetError(f"{path.name}: duplicate rule id {rule['id']!r}")
        seen.add(rule.get("id"))
    try:
        eff_from = date.fromisoformat(raw["effective_from"])
        eff_to = date.fromisoformat(raw["effective_to"]) if raw.get("effective_to") else None
    except ValueError:
        raise RulesetError(f"{path.name}: effective dates must be YYYY-MM-DD")
    return Ruleset(id=raw["id"], version=raw["version"], status=raw.get("status", "active"),
                   effective_from=eff_from, effective_to=eff_to, changes=raw.get("changes", ""),
                   sources=tuple(raw.get("sources", [])), structure=raw["structure"], rules=tuple(raw["rules"]),
                   path=str(path))


_cache: dict[str, list[Ruleset]] = {}


def load(family: str = "in_ctc", directory: Path | None = None, refresh: bool = False) -> list[Ruleset]:
    """All rulesets in a family, oldest first. Validated at load, so a malformed
    file fails loudly at start-up rather than silently skipping a rule."""
    key = f"{directory or RULESET_DIR}:{family}"
    if refresh or key not in _cache:
        folder = (directory or RULESET_DIR) / family
        rulesets = [_parse(p) for p in sorted(folder.glob("*.json"))]
        if not rulesets:
            raise RulesetError(f"No rulesets found in {folder}")
        versions = [r.version for r in rulesets]
        if len(set(versions)) != len(versions):
            raise RulesetError(f"Duplicate ruleset versions in {folder}")
        _cache[key] = sorted(rulesets, key=_version_key)
    return _cache[key]


def ruleset_for(as_of: date | None = None, version: str | None = None, family: str = "in_ctc") -> Ruleset:
    rulesets = load(family)
    if version:
        for r in rulesets:
            if r.version == version:
                return r
        raise RulesetError(f"Unknown ruleset version {version!r}. Known: {', '.join(r.version for r in rulesets)}")
    on = as_of or date.today()
    in_force = [r for r in rulesets if r.in_force(on)]
    if not in_force:
        raise RulesetError(f"No ruleset is in force on {on.isoformat()} (earliest takes effect "
                           f"{rulesets[0].effective_from.isoformat()}).")
    return in_force[-1]


def active(family: str = "in_ctc") -> Ruleset:
    """Newest ruleset in force today. If the clock is before the first effective
    date, fall back to the newest one rather than failing the whole app."""
    try:
        return ruleset_for(family=family)
    except RulesetError:
        logger.warning("No %s ruleset in force today; using the newest one.", family)
        return load(family)[-1]


# --------------------------------------------------------------------------
# Structuring
# --------------------------------------------------------------------------

def structure(cash_ctc_lpa: float, variable_lpa: float, city: str, rs: Ruleset | None = None) -> dict:
    """
    Splits cash CTC into Basic/HRA/EPF/Gratuity/LTA/Special/Variable in whole
    rupees, with Special Allowance as the balancing line so the rows sum exactly
    to the CTC (CR-02).

    Order matters: Basic and the statutory employer contributions are fixed
    first, then the recruiter's variable pay, then HRA/LTA at their conventions
    *if there's room*. With a large variable component (common in sales roles)
    HRA is squeezed below convention - a warning, because that convention isn't a
    statute. If variable pay doesn't fit at all, that's a block.
    """
    rs = rs or active()
    s = rs.structure
    total = round(cash_ctc_lpa * 1e5)
    variable = round(variable_lpa * 1e5)
    basic = round(total * s["basic_pct_of_ctc"])
    epf = round(basic * s["employer_epf_pct_of_basic"])
    gratuity = round(basic * s["gratuity_accrual_pct_of_basic"])
    room = total - basic - epf - gratuity - variable
    hra_target = round(basic * rs.hra_pct(city))
    hra = max(0, min(hra_target, room))
    lta = max(0, min(round(basic * s["lta_pct_of_basic"]), room - hra))
    special = room - hra - lta  # negative only when variable doesn't fit
    rows = [
        ("Basic + DA", basic), ("HRA", hra), ("Special allowance", special), ("LTA", lta),
        ("Variable / performance pay", variable), ("Employer EPF", epf), ("Gratuity (accrued)", gratuity),
    ]
    out = _finish(rows, total, rs, city, hra_target)
    out.update(fits=special >= 0, hra_squeezed=hra < hra_target, mode="auto",
               max_variable_lpa=round((total - basic - epf - gratuity) / 1e5, 2))
    return out


def structure_from_components(cash_ctc_lpa: float, city: str, components: dict, rs: Ruleset | None = None) -> dict:
    """An already-decided breakup (e.g. from an offer-letter generator). Missing
    optional lines default to 0; Special allowance balances unless it was given."""
    rs = rs or active()
    total = round(cash_ctc_lpa * 1e5)
    inr = {k: round(float(components.get(f"{k}_lpa") or 0) * 1e5)
           for k in ("basic", "hra", "employer_epf", "gratuity", "lta", "variable", "special")}
    given_special = components.get("special_lpa") not in (None, "")
    allocated = sum(v for k, v in inr.items() if k != "special")
    special = inr["special"] if given_special else total - allocated
    rows = [
        ("Basic + DA", inr["basic"]), ("HRA", inr["hra"]), ("Special allowance", special), ("LTA", inr["lta"]),
        ("Variable / performance pay", inr["variable"]), ("Employer EPF", inr["employer_epf"]),
        ("Gratuity (accrued)", inr["gratuity"]),
    ]
    out = _finish(rows, total, rs, city, round(inr["basic"] * rs.hra_pct(city)))
    room = total - inr["basic"] - inr["employer_epf"] - inr["gratuity"] - inr["variable"]
    out.update(fits=room >= 0 and special >= 0, hra_squeezed=False, mode="components",
               max_variable_lpa=round((total - inr["basic"] - inr["employer_epf"] - inr["gratuity"]) / 1e5, 2))
    return out


def _finish(rows, total, rs, city, hra_target) -> dict:
    amounts = dict(rows)
    basic, epf, gratuity = amounts["Basic + DA"], amounts["Employer EPF"], amounts["Gratuity (accrued)"]
    # Statutory wage (Code on Wages s.2(y)), approximated: wages are Basic + DA; if the
    # excluded components (HRA, allowances, bonus...) exceed the cap share of total
    # remuneration, the excess is added back to wages for PF/gratuity purposes.
    # Remuneration = CTC minus employer EPF and gratuity accrual, which aren't paid
    # to the employee. This is the PF/gratuity wage base, distinct from "Basic".
    remuneration = total - epf - gratuity
    addback = max(0, (remuneration - basic) - round(remuneration * rs.structure["wage_excluded_cap_pct"]))
    return {
        "rows": [{"component": name, "inr": amount, "lpa": round(amount / 1e5, 2)} for name, amount in rows],
        "total_inr": total,
        "sum_inr": sum(a for _n, a in rows),
        "hra_target_inr": hra_target,
        "is_metro": rs.is_metro(city),
        "remuneration_inr": remuneration,
        "wage_addback_inr": addback,
        "statutory_wage_inr": basic + addback,
        "ruleset": f"{rs.id} v{rs.version}",
    }


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------

def _amounts(st: dict) -> dict:
    by = {r["component"]: r["inr"] for r in st["rows"]}
    return {"basic": by["Basic + DA"], "hra": by["HRA"], "employer_epf": by["Employer EPF"],
            "gratuity": by["Gratuity (accrued)"], "variable": by["Variable / performance pay"]}


def _result(rule, status, title, detail):
    return {"id": rule["id"], "category": rule["category"], "status": status, "title": title, "detail": detail,
            "statute": bool(rule.get("statute"))}


def _eval_variable_fits(rule, st, ctx):
    if st["fits"]:
        return None  # nothing to say when it fits; the other rules describe the structure
    a = _amounts(st)
    basic_pct = ctx["rs"].structure["basic_pct_of_ctc"] * 100
    return _result(rule, rule["on_fail"], "Variable pay too large for this structure",
                   f"After Basic ({basic_pct:g}% of CTC), employer EPF and gratuity, at most "
                   f"Rs.{st['max_variable_lpa']:.2f} LPA is left for variable pay; Rs.{a['variable'] / 1e5:.2f} "
                   f"was entered.")


def _eval_wage_addback(rule, st, ctx):
    cap = rule.get("params", {}).get("excluded_cap_pct", 0.5) * 100
    addback = st["wage_addback_inr"]
    if addback:
        return _result(rule, rule["on_fail"], f"Allowances exceed {cap:g}% of remuneration",
                       f"Under the Code on Wages, Rs.{addback:,} would be added back to wages, making the "
                       f"PF/gratuity wage base Rs.{st['statutory_wage_inr']:,} rather than Basic. Contributions in "
                       f"this structure are computed on Basic only.")
    return _result(rule, "pass", f"Wages are at least {100 - cap:g}% of remuneration",
                   f"Basic + DA (Rs.{st['statutory_wage_inr']:,}) is at least half of remuneration "
                   f"(Rs.{st['remuneration_inr']:,}, i.e. CTC less employer EPF and gratuity), so no allowances are "
                   f"added back to wages.")


def _eval_basic_min(rule, st, ctx):
    p = rule.get("params", {})
    basic = _amounts(st)["basic"]
    pct = basic / st["total_inr"] if st["total_inr"] else 0
    ok = pct >= p.get("min_pct", 0.5) - p.get("tolerance_pp", 0)
    return _result(rule, "pass" if ok else rule["on_fail"],
                   f"Basic + DA >= {p.get('min_pct', 0.5) * 100:g}% of CTC",
                   f"Basic is {pct * 100:.1f}% of CTC (Rs.{basic / 1e5:.2f} of Rs.{st['total_inr'] / 1e5:.2f} LPA).")


def _eval_ratio(rule, st, ctx):
    p = rule["params"]
    comp = p["component"]
    a = _amounts(st)
    expected = p["expected"]
    city = ctx["city"]
    if isinstance(expected, dict):
        metro = st["is_metro"]
        expected = expected["metro" if metro else "non_metro"]
        title = (f"{COMPONENT_LABELS[comp]} ~{expected * 100:.0f}% of Basic "
                 f"({'metro' if metro else 'non-metro'} convention for {city})")
    elif comp == "gratuity":
        title = f"Gratuity accrual ~{expected * 100:.2f}% of Basic"
    else:
        title = f"{COMPONENT_LABELS[comp]} = {expected * 100:g}% of Basic"
    actual = a[comp] / a["basic"] if a["basic"] > 0 else 0
    tol = p["tolerance"]
    ok = abs(actual - expected) <= tol + 1e-9
    digits = 2 if comp == "gratuity" else 1
    label = "EPF" if comp == "employer_epf" else COMPONENT_LABELS[comp]
    exp_txt = f"{expected * 100:.{2 if comp == 'gratuity' else 0}f}%"
    detail = (f"{label} is {actual * 100:.{digits}f}% of Basic (expected "
              f"{'~' if comp != 'employer_epf' else ''}{exp_txt}, +/-{tol * 100:g}pp).")
    if comp == "hra" and st.get("hra_squeezed"):
        detail += " HRA is below convention because variable pay uses the room - a convention, not a statute."
    return _result(rule, "pass" if ok else rule["on_fail"], title, detail)


def _eval_within(rule, st, ctx):
    a = _amounts(st)
    four = a["basic"] + a["hra"] + a["employer_epf"] + a["gratuity"]
    total = st["total_inr"]
    return _result(rule, "pass" if four <= total + 1000 else rule["on_fail"],
                   "Basic + HRA + EPF + Gratuity does not exceed total CTC",
                   f"These four components sum to Rs.{four / 1e5:.2f} LPA against a Rs.{total / 1e5:.2f} LPA CTC "
                   f"(remainder of Rs.{(total - four) / 1e5:.2f} LPA is available for LTA/Special Allowance/Bonus).")


def _eval_sum(rule, st, ctx):
    ok = st["sum_inr"] == st["total_inr"]
    return _result(rule, "pass" if ok else rule["on_fail"], "Breakup sums exactly to cash CTC",
                   f"Components sum to Rs.{st['sum_inr']:,} of Rs.{st['total_inr']:,}.")


def _eval_epf_ceiling(rule, st, ctx):
    p = rule.get("params", {})
    ceiling = p.get("monthly_wage_ceiling_inr", 15000)
    pct = p.get("employer_pct", 0.12)
    capped = round(ceiling * pct)
    monthly = round(_amounts(st)["employer_epf"] / 12)
    return _result(rule, "info", "EPF wage ceiling",
                   f"Employer EPF is shown at {pct * 100:g}% of full Basic (Rs.{monthly:,}/month). Many employers cap "
                   f"it at Rs.{capped:,}/month (the Rs.{ceiling:,} wage ceiling) - confirm your payroll policy.")


_EVALUATORS = {
    "variable_fits": _eval_variable_fits, "wage_addback": _eval_wage_addback, "basic_min_pct_of_ctc": _eval_basic_min,
    "ratio_of_basic": _eval_ratio, "components_within_ctc": _eval_within, "sums_to_ctc": _eval_sum,
    "epf_wage_ceiling": _eval_epf_ceiling,
}


def evaluate(st: dict, city: str, rs: Ruleset | None = None) -> list[dict]:
    """Runs every rule in ruleset order. Rules marked requires_fit are skipped when
    the structure doesn't fit (variable_fits has already blocked it)."""
    rs = rs or active()
    ctx = {"rs": rs, "city": city}
    out = []
    for rule in rs.rules:
        if rule.get("requires_fit") and not st["fits"]:
            continue
        res = _EVALUATORS[rule["type"]](rule, st, ctx)
        if res is not None:
            res["ruleset"] = f"{rs.id} v{rs.version}"
            out.append(res)
    return out


# --------------------------------------------------------------------------
# Stateless validation (the API contract)
# --------------------------------------------------------------------------

class ValidationInputError(ValueError):
    pass


def _num(payload, key, required=False, lo=0.0, hi=10000.0):
    raw = payload.get(key)
    if raw in (None, ""):
        if required:
            raise ValidationInputError(f"{key} is required.")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationInputError(f"{key} must be a number.")
    if not (lo <= value <= hi):
        raise ValidationInputError(f"{key} must be between {lo:g} and {hi:g}.")
    return value


def validate(payload: dict, normalize_city=None) -> dict:
    """
    payload: cash_ctc_lpa (required), city (required), variable_lpa (optional),
    components (optional dict of basic_lpa/hra_lpa/employer_epf_lpa/gratuity_lpa/
    lta_lpa/variable_lpa/special_lpa - validates an already-decided breakup instead
    of structuring one), as_of (YYYY-MM-DD) or ruleset_version (optional).
    """
    cash = _num(payload, "cash_ctc_lpa", required=True)
    if cash <= 0:
        raise ValidationInputError("cash_ctc_lpa must be greater than zero.")
    raw_city = payload.get("city")
    if not raw_city:
        raise ValidationInputError("city is required.")
    city = (normalize_city(raw_city) if normalize_city else None) or str(raw_city).strip()
    version = payload.get("ruleset_version") or None
    as_of = None
    if payload.get("as_of"):
        try:
            as_of = date.fromisoformat(str(payload["as_of"]))
        except ValueError:
            raise ValidationInputError("as_of must be YYYY-MM-DD.")
    rs = ruleset_for(as_of=as_of, version=version)  # RulesetError -> caller maps to 422
    components = payload.get("components")
    if components:
        if not isinstance(components, dict):
            raise ValidationInputError("components must be an object.")
        for k in ("basic_lpa", "hra_lpa", "employer_epf_lpa", "gratuity_lpa", "lta_lpa", "variable_lpa",
                  "special_lpa"):
            _num(components, k, required=k == "basic_lpa", lo=-10000 if k == "special_lpa" else 0)
        st = structure_from_components(cash, city, components, rs)
    else:
        variable = _num(payload, "variable_lpa")
        if variable is None:
            variable = round(cash * rs.structure["default_variable_pct_of_ctc"], 2)
        st = structure(cash, variable, city, rs)
    checks = sorted(evaluate(st, city, rs), key=lambda c: STATUS_ORDER[c["status"]])
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in STATUS_ORDER}
    worst = checks[0]["status"] if checks else "pass"
    return {
        "compliant": summary["block"] == 0,
        "status": worst,
        "summary": summary,
        "city": city,
        "ruleset": rs.meta(),
        "structure": {k: st[k] for k in ("rows", "total_inr", "sum_inr", "fits", "hra_squeezed", "is_metro",
                                         "remuneration_inr", "wage_addback_inr", "statutory_wage_inr",
                                         "max_variable_lpa", "mode")},
        "checks": checks,
        "disclaimer": disclaimer(rs),
    }


def disclaimer(rs: Ruleset | None = None) -> str:
    rs = rs or active()
    return ("Decision support only - not legal, tax or payroll-audit advice. Checks use "
            f"ruleset {rs.id} v{rs.version} (effective {rs.effective_from.isoformat()}). "
            "Confirm statutory treatment with your payroll provider or counsel.")
