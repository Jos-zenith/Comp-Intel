"""
Observed-offer benchmark for pilot and production workspaces (PRD v2 sections 8, 11, 12.3).

The synthetic model never runs outside demo mode. Here the market range comes from
real offer outcomes in the consented benchmark network (benchmark_observations):

  * tenant outcomes, shared only when the workspace has data sharing switched on, and
  * design-partner records imported by an admin with a recorded consent basis.

Only ACCEPTED offers set the market range: a final accepted package is a
market-clearing price, a declined one isn't. Declines and counters are reported
alongside as context, which is exactly the signal HRIS-based data can't see.

Privacy and quality gates, all configurable in Settings:
  min_cohort_size     minimum accepted observations before a range is shown
  min_contributors    minimum distinct contributing companies (k-anonymity style)
  pilot_lookback_months  observations older than this are ignored

The cohort widens through the same kind of ladder as the demo engine; the exact
observation rows used are hashed (AP-06) and their IDs stored, so a submission
can be re-derived later from the same rows.
"""
from __future__ import annotations

import csv
import hashlib
import io
import statistics
from datetime import date

import saas_metrics as sm
from saas_domain import CITIES, FUNDING_STAGES, LEVEL_BANDS, ROLE_ARCHETYPES, role_archetype_for

from . import db
from .benchmark import _common, confidence, display_for, unavailable_benchmark

ENGINE_VERSION = "observed-offers-1.0"
SOURCE_LABEL = "Observed accepted offers from consented contributors (real data)"

LEVELS = [b[2] for b in LEVEL_BANDS]
ALL_ROLES = [r for roles in ROLE_ARCHETYPES.values() for r in roles]
EMPLOYMENT_TYPES = ["Full-time", "Fixed-term contract"]
OUTCOMES = ("accepted", "declined", "withdrawn")
NDR_BANDS = ["Below 90%", "90-100%", "100-110%", "110-120%", "120%+"]
EMPLOYEE_BANDS = [(30, "1-29"), (100, "30-99"), (200, "100-199"), (500, "200-499"), (None, "500+")]
CONSENT_BASES = {
    "design_partner_dsa": "Design-partner data-sharing agreement",
    "recruiting_partner": "Recruiting-partner agreement",
    "contributor_terms": "Contributor terms (free tier)",
}
PARTNER_CSV_COLUMNS = [
    "company_ref", "funding_stage", "employee_count", "arr_band", "ndr_band", "city", "role", "level",
    "employment_type", "offer_date", "initial_cash_ctc", "variable_pct", "initial_esop_options",
    "counter_cash_ctc", "counter_esop_options", "final_cash_ctc", "final_esop_options", "outcome",
    "decline_reason", "competing_offer",
]
HASHED_FIELDS = ("id", "contributor_key", "role", "level", "city_tier", "funding_stage", "final_cash_ctc",
                 "final_esop_options", "observed_month")


def employee_band(count) -> str | None:
    if count in (None, ""):
        return None
    count = int(count)
    for upper, label in EMPLOYEE_BANDS:
        if upper is None or count < upper:
            return label
    return None


def ndr_band(ndr_pct) -> str | None:
    if ndr_pct is None:
        return None
    if ndr_pct < 90:
        return NDR_BANDS[0]
    if ndr_pct < 100:
        return NDR_BANDS[1]
    if ndr_pct < 110:
        return NDR_BANDS[2]
    if ndr_pct < 120:
        return NDR_BANDS[3]
    return NDR_BANDS[4]


def quantile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between order statistics (same as pandas' default)."""
    if not sorted_values:
        raise ValueError("no values")
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _month_cutoff(months: int) -> str:
    today = date.today()
    total = today.year * 12 + (today.month - 1) - int(months)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _months_ago(month: str) -> int:
    y, m = (int(p) for p in month.split("-")[:2])
    today = date.today()
    return (today.year - y) * 12 + (today.month - m)


def _rows_hash(rows: list[dict]) -> str:
    canon = [{k: r[k] for k in HASHED_FIELDS} for r in sorted(rows, key=lambda r: r["id"])]
    return hashlib.sha256(db.dumps(canon).encode()).hexdigest()


def network_rows(settings: dict, status: str | None = "accepted") -> list[dict]:
    cutoff = _month_cutoff(int(settings.get("pilot_lookback_months", 24)))
    sql = "SELECT * FROM benchmark_observations WHERE observed_month >= ?"
    params: list = [cutoff]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return [dict(r) for r in db.query(sql, params)]


def find_cohort(role: str, level: str, city: str, funding_stage: str, settings: dict) -> dict:
    min_n = int(settings.get("min_cohort_size", 10))
    min_contrib = int(settings.get("min_contributors", 3))
    tier = CITIES[city][0]
    archetype = role_archetype_for(role)
    all_rows = network_rows(settings, status=None)
    accepted = [r for r in all_rows if r["status"] == "accepted" and r["final_cash_ctc"] is not None]
    ladder = [
        (f"{role} · {level} · {tier} · {funding_stage}",
         lambda r: r["role"] == role and r["level"] == level and r["city_tier"] == tier
         and r["funding_stage"] == funding_stage),
        (f"{role} · {level} · {tier} (all stages)",
         lambda r: r["role"] == role and r["level"] == level and r["city_tier"] == tier),
        (f"{role} · {level} (all cities and stages)",
         lambda r: r["role"] == role and r["level"] == level),
        (f"{archetype} roles · {level} (all cities and stages)",
         lambda r: role_archetype_for(r["role"]) == archetype and r["level"] == level),
    ]
    best_seen = {"n": 0, "contributors": 0}
    for rung, (label, match) in enumerate(ladder):
        rows = [r for r in accepted if match(r)]
        contributors = len({r["contributor_key"] for r in rows})
        if len(rows) > best_seen["n"]:
            best_seen = {"n": len(rows), "contributors": contributors}
        if len(rows) < min_n or contributors < min_contrib:
            continue
        values = sorted(r["final_cash_ctc"] for r in rows)
        months = sorted(r["observed_month"] for r in rows)
        others = [r for r in all_rows if r["status"] != "accepted" and match(r)]
        countered = [r for r in rows + others if r.get("counter_cash_ctc") is not None]
        esop = sorted(r["final_esop_options"] for r in rows if r.get("final_esop_options"))
        return {
            "suppressed": False,
            "engine": ENGINE_VERSION,
            "definition": label,
            "rung": rung,
            "widened": rung > 0,
            "n": len(rows),
            "companies": contributors,
            "contributors": contributors,
            "min_n": min_n,
            "min_contributors": min_contrib,
            "empirical_p25": round(quantile(values, 0.25), 2),
            "empirical_p50": round(quantile(values, 0.50), 2),
            "empirical_p75": round(quantile(values, 0.75), 2),
            "vintage": months[-1] + "-01",
            "oldest_month": months[0],
            "median_age_months": int(statistics.median(_months_ago(m) for m in months)),
            "declined_n": sum(1 for r in others if r["status"] == "declined"),
            "withdrawn_n": sum(1 for r in others if r["status"] == "withdrawn"),
            "countered_n": len(countered),
            "esop_options": ({"n": len(esop), "p25": int(quantile(esop, 0.25)), "p50": int(quantile(esop, 0.5)),
                              "p75": int(quantile(esop, 0.75))} if len(esop) >= 5 else None),
            "source": SOURCE_LABEL,
            "observation_ids": sorted(r["id"] for r in rows),
            "cohort_hash": _rows_hash(rows),
        }
    return {
        "suppressed": True,
        "engine": ENGINE_VERSION,
        "reason": (f"Not enough observed offers yet: the best cohort found has {best_seen['n']} accepted offer(s) from "
                   f"{best_seen['contributors']} contributor(s); at least {min_n} offers from {min_contrib} companies "
                   f"are needed, even after widening."),
        "vintage": None, "min_n": min_n, "min_contributors": min_contrib,
        "best_n": best_seen["n"], "best_contributors": best_seen["contributors"],
    }


def reproduce(cohort: dict) -> dict:
    """Re-derive a stored observed cohort from its exact rows (reproducibility, AP-06)."""
    ids = cohort.get("observation_ids") or []
    if not ids:
        return {"ok": False, "missing": 0, "reason": "No observation IDs stored."}
    placeholders = ",".join("?" for _ in ids)
    rows = [dict(r) for r in db.query(f"SELECT * FROM benchmark_observations WHERE id IN ({placeholders})", ids)]
    missing = len(ids) - len(rows)
    if missing:
        return {"ok": False, "missing": missing,
                "reason": f"{missing} of {len(ids)} observations have since been removed (e.g. a partner batch was "
                          f"withdrawn), so the cohort can't be re-derived. The stored snapshot remains the record."}
    values = sorted(r["final_cash_ctc"] for r in rows)
    rerun = {q: round(quantile(values, p), 2) for q, p in (("p25", 0.25), ("p50", 0.5), ("p75", 0.75))}
    hash_ok = _rows_hash(rows) == cohort.get("cohort_hash")
    same = all(abs(rerun[q] - cohort[f"empirical_{q}"]) < 0.005 for q in ("p25", "p50", "p75"))
    return {"ok": hash_ok and same, "missing": 0, "rows_hash_ok": hash_ok, "rerun": rerun, "reproduced": same}


def build_observed_benchmark(*, company, profile: dict, offer: dict, settings: dict, band, accepted_peer,
                             metrics_age_days, mode: str, affordability_missing=None) -> dict:
    cohort = find_cohort(offer["role"], offer["level"], offer["city"], company["funding_stage"], settings)
    if cohort["suppressed"]:
        bm = unavailable_benchmark(company=company, profile=profile, offer=offer, settings=settings, band=band,
                                   accepted_peer=accepted_peer, model_version=ENGINE_VERSION, mode=mode,
                                   reason=cohort["reason"])
        bm["cohort"] = cohort
        bm["inputs"]["model_level"] = offer["level"]
        return bm

    p25, p50, p75 = cohort["empirical_p25"], cohort["empirical_p50"], cohort["empirical_p75"]
    policy_pct = float(settings.get("target_percentile", 50))
    pos = sm.pay_positioning(profile["health_score"], policy_pct, bool(settings.get("health_positioning", True)))
    target = sm.interpolate_percentile(p25, p50, p75, pos["target_percentile"])
    conf = confidence(cohort, profile, metrics_age_days, settings, affordability_missing=affordability_missing)
    display = display_for(conf["level"], p25, p75)

    drivers = []
    if display["show_target"]:
        shift = (f" Health score {pos['health_score']:.0f}/100 ({pos['band']}) moves that to "
                 f"P{pos['target_percentile']:g}." if pos["percentile_shift"] else "")
        drivers.append(f"Your policy targets P{policy_pct:g}.{shift} That gives Rs.{target:.1f} LPA. {pos['stance']}")
    drivers.append(f"Range from {cohort['n']} accepted offers at {cohort['contributors']} companies "
                   f"({cohort['oldest_month']} to {cohort['vintage'][:7]}), cohort: {cohort['definition']}.")
    if cohort["declined_n"] or cohort["withdrawn_n"]:
        drivers.append(f"The same cohort also has {cohort['declined_n']} declined and {cohort['withdrawn_n']} "
                       f"withdrawn offers; they aren't in the range because a declined package didn't clear the market.")
    if cohort["countered_n"]:
        drivers.append(f"{cohort['countered_n']} offers in this cohort drew a candidate counteroffer.")
    if offer.get("competing_offer"):
        co = offer["competing_offer"]
        where = "above" if co > display["hi"] else "inside" if co >= display["lo"] else "below"
        drivers.append(f"Candidate holds a competing offer of Rs.{co:.1f} LPA, {where} the shown range.")
    if offer.get("expected_ctc") and display["show_target"]:
        drivers.append(f"Candidate expects Rs.{offer['expected_ctc']:.1f} LPA, "
                       f"{(offer['expected_ctc'] / target - 1) * 100:+.1f}% versus the target.")
    if band is not None:
        drivers.append(f"Internal band for {offer['role']} / {offer['level']}: Rs.{band['min_lpa']:.1f}-"
                       f"{band['max_lpa']:.1f} LPA (midpoint Rs.{band['mid_lpa']:.1f}).")

    fmv = float(settings.get("fmv_per_share_inr", 0) or 0)
    fds = float(settings.get("fully_diluted_shares", 0) or 0)
    vesting = float(settings.get("default_vesting_years", 4))
    obs_esop = cohort.get("esop_options")
    esop = {"source": "observed", "vesting_years": vesting, "fmv_per_share_inr": fmv, "esop_tilt": pos["esop_tilt"],
            "options_low": 0, "options_mid": 0, "options_high": 0, "annual_fmv_lpa": 0, "observed_n": 0}
    if obs_esop:
        tilt = pos["esop_tilt"]
        low, mid, high = (int(round(obs_esop[k] * tilt, -1)) for k in ("p25", "p50", "p75"))
        grant_lakh = mid * fmv / 1e5
        ev = sm.esop_expected_value(grant_lakh / vesting if vesting else 0, profile["health_score"])
        esop.update(options_low=low, options_mid=mid, options_high=high, observed_n=obs_esop["n"],
                    grant_value_lakh_mid=round(grant_lakh, 2), annual_fmv_lpa=round(grant_lakh / vesting, 2),
                    expected_value_annual_lpa=ev["expected_value_lpa"],
                    liquidity_probability=ev["liquidity_probability"], payout_multiple=ev["payout_multiple"],
                    band=ev["band"])
        if fds:
            esop["dilution_mid_pct"] = round(mid / fds * 100, 4)

    bm = _common(company, profile, offer, offer["level"], ENGINE_VERSION, mode, band, accepted_peer, settings)
    bm.update({
        "p25": p25, "p50": p50, "p75": p75, "target": round(target, 2),
        "target_percentile": pos["target_percentile"], "positioning": pos,
        "cohort": cohort, "confidence": conf, "display": display, "drivers": drivers,
        "shap_drivers": [], "shap_base_lpa": None, "segment_median": None,
        "compa_target_vs_peer": round(target / p50, 2) if p50 else None,
        "compa_target_vs_band": round(target / band["mid_lpa"], 2) if band is not None and band["mid_lpa"] else None,
        "esop": esop,
    })
    return bm


# --------------------------------------------------------------------------
# Design-partner data import
# --------------------------------------------------------------------------

class ImportError_(ValueError):
    pass


def partner_contributor_key(secret: str, company_ref: str) -> str:
    return hashlib.sha256(f"{secret}:partner:{company_ref.strip().lower()}".encode()).hexdigest()[:12]


def _opt_float(row, key, lo=0.0, hi=10000.0):
    raw = (row.get(key) or "").strip()
    if raw == "":
        return None
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        raise ImportError_(f"{key} must be a number")
    if not (lo <= value <= hi):
        raise ImportError_(f"{key} must be between {lo:g} and {hi:g}")
    return value


def _opt_int(row, key):
    value = _opt_float(row, key, 0, 10**12)
    return int(value) if value is not None else None


def parse_partner_csv(text: str, secret: str) -> list[dict]:
    """Validates every row before anything is written; one bad row rejects the batch."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportError_("the file is empty")
    header = {h.strip() for h in reader.fieldnames if h}
    required = {"company_ref", "funding_stage", "city", "role", "level", "offer_date", "outcome"}
    if not required <= header:
        raise ImportError_("missing columns: " + ", ".join(sorted(required - header)))
    out = []
    for i, raw in enumerate(reader, start=2):
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        try:
            ref = row["company_ref"]
            if not ref or "@" in ref or len(ref) > 64:
                raise ImportError_("company_ref must be a short pseudonymous code (no names or emails)")
            if row["funding_stage"] not in FUNDING_STAGES:
                raise ImportError_(f"unknown funding_stage {row['funding_stage']!r}")
            if row["city"] not in CITIES:
                raise ImportError_(f"unknown city {row['city']!r}")
            if row["role"] not in ALL_ROLES or row["level"] not in LEVELS:
                raise ImportError_("unknown role or level")
            emp = row.get("employment_type") or "Full-time"
            if emp not in EMPLOYMENT_TYPES:
                raise ImportError_("unknown employment_type")
            try:
                offer_date = date.fromisoformat(row["offer_date"])
            except ValueError:
                raise ImportError_("offer_date must be YYYY-MM-DD")
            if offer_date > date.today():
                raise ImportError_("offer_date is in the future")
            outcome = row["outcome"].lower()
            if outcome not in OUTCOMES:
                raise ImportError_("outcome must be accepted, declined or withdrawn")
            ndr = row.get("ndr_band") or None
            if ndr and ndr not in NDR_BANDS:
                raise ImportError_("ndr_band must be one of: " + " | ".join(NDR_BANDS))
            rec = {
                "contributor_key": partner_contributor_key(secret, ref),
                "role": row["role"], "level": row["level"], "city_tier": CITIES[row["city"]][0],
                "funding_stage": row["funding_stage"], "employment_type": emp,
                "employee_band": employee_band(_opt_int(row, "employee_count")),
                "arr_band": row.get("arr_band") or None, "ndr_band": ndr,
                "status": outcome, "observed_month": offer_date.isoformat()[:7],
                "initial_cash_ctc": _opt_float(row, "initial_cash_ctc"),
                "variable_pct": _opt_float(row, "variable_pct", 0, 100),
                "initial_esop_options": _opt_int(row, "initial_esop_options"),
                "counter_cash_ctc": _opt_float(row, "counter_cash_ctc"),
                "counter_esop_options": _opt_int(row, "counter_esop_options"),
                "final_cash_ctc": _opt_float(row, "final_cash_ctc"),
                "final_esop_options": _opt_int(row, "final_esop_options"),
                "decline_reason": (row.get("decline_reason") or None),
                "competing_offer": _opt_float(row, "competing_offer"),
            }
            if outcome == "accepted" and rec["final_cash_ctc"] is None:
                raise ImportError_("final_cash_ctc is required for an accepted offer")
            if rec["decline_reason"] and len(rec["decline_reason"]) > 80:
                raise ImportError_("decline_reason is too long")
        except ImportError_ as exc:
            raise ImportError_(f"row {i}: {exc}")
        # Same company, role, date and money -> same record; stops a file being imported twice row by row.
        fingerprint = db.dumps([rec["contributor_key"], rec["role"], rec["level"], rec["city_tier"],
                                row["offer_date"], rec["status"], rec["initial_cash_ctc"], rec["final_cash_ctc"],
                                rec["final_esop_options"]])
        rec["record_hash"] = hashlib.sha256(fingerprint.encode()).hexdigest()
        out.append(rec)
    if not out:
        raise ImportError_("no data rows")
    return out


def partner_template() -> str:
    return (",".join(PARTNER_CSV_COLUMNS) + "\n"
            "P-017,Series A,85,Rs.25-100 Cr,110-120%,Bengaluru,Software Engineer,Senior,Full-time,2026-08-14,"
            "42,10,9000,46,12000,44,10000,accepted,,\n"
            "P-017,Series A,85,Rs.25-100 Cr,110-120%,Pune,Account Executive,Mid,Full-time,2026-07-02,"
            "18,40,0,,,18,0,declined,Accepted competing offer,21\n")


def usable_cohorts(settings: dict) -> int:
    """Exact cohorts (role, level, city tier, stage) that clear both privacy thresholds
    today - i.e. how many offers could get a real-data range in pilot mode without widening."""
    min_n = int(settings.get("min_cohort_size", 10))
    min_contrib = int(settings.get("min_contributors", 3))
    groups: dict = {}
    for r in network_rows(settings):
        if r["final_cash_ctc"] is None:
            continue
        g = groups.setdefault((r["role"], r["level"], r["city_tier"], r["funding_stage"]), [0, set()])
        g[0] += 1
        g[1].add(r["contributor_key"])
    return sum(1 for n, cos in groups.values() if n >= min_n and len(cos) >= min_contrib)


def network_stats(settings: dict) -> dict:
    """Pilot exit-criteria inputs: record and contributor counts across the whole network."""
    rows = network_rows(settings, status=None)
    decided = [r for r in rows if r["status"] in ("accepted", "declined")]
    declined = [r for r in rows if r["status"] == "declined"]
    return {
        "records": len(rows),
        "decided": len(decided),
        "accepted": sum(1 for r in rows if r["status"] == "accepted"),
        "declined": len(declined),
        "contributors": len({r["contributor_key"] for r in rows}),
        "partner_records": sum(1 for r in rows if r.get("source") == "partner_import"),
        "with_counter": sum(1 for r in rows if r.get("counter_cash_ctc") is not None),
        "decline_reason_capture": (round(sum(1 for r in declined if r.get("decline_reason")) / len(declined) * 100)
                                   if declined else None),
        "lookback_months": int(settings.get("pilot_lookback_months", 24)),
        "usable_cohorts": usable_cohorts(settings),
    }
