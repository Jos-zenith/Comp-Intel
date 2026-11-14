"""
Benchmark engine: peer cohort, confidence, policy-based target, ESOP range.

The P25/P50/P75 numbers come from the existing quantile models (via the
predict_fn main.py hands us). This module adds what the PRD asks for around
them: which peer cohort they describe and how big it is, how fresh the data
is, how confident to be - and confidence changes what is shown:

  High          full P25/P50/P75 and a target
  Medium        range widened by 8% each side, target shown, caution notice
  Low           broad band only (15% wider), no target, recruiter must acknowledge
  Insufficient  no recommendation; package is entered manually with a reason

A workspace in pilot or production mode never runs the synthetic demo model.
Its benchmarks come from observed, consented offers instead (workflow/pilot.py)
and are Insufficient until that data clears the privacy thresholds.

Company health never moves the market range. It moves where the company aims
inside that range (target percentile) and the cash/ESOP tilt - see
saas_metrics.pay_positioning.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

import saas_metrics as sm
from saas_domain import CITIES, FUNDING_STAGES, experience_to_level, role_archetype_for

DATA_SOURCE_LABEL = "Synthetic, benchmark-calibrated demo dataset (not real market data)"

WORKSPACE_MODES = {
    "demo": "Demo - synthetic benchmark model, all outputs watermarked",
    "pilot": "Pilot - synthetic model off; benchmarks from observed, consented offers once privacy thresholds are met",
    "production": "Production - synthetic model off; benchmarks from observed, consented offers only",
}

DISPLAY_POLICY = {
    "High": {"mode": "full", "widen": 0.0, "show_quantiles": True, "show_target": True, "ack_required": False,
             "manual_reason_required": False, "notice": None},
    "Medium": {"mode": "widened", "widen": 0.08, "show_quantiles": True, "show_target": True, "ack_required": False,
               "manual_reason_required": False,
               "notice": "Medium confidence: the market range is widened by 8% each side. Treat the target as a guide."},
    "Low": {"mode": "broad", "widen": 0.15, "show_quantiles": False, "show_target": False, "ack_required": True,
            "manual_reason_required": False,
            "notice": "Low confidence: only a broad band is shown and there is no target. "
                      "You'll need to acknowledge this before submitting."},
    "Insufficient": {"mode": "none", "widen": None, "show_quantiles": False, "show_target": False,
                     "ack_required": False, "manual_reason_required": True,
                     "notice": "No recommendation: there isn't enough reliable data. Enter the package manually "
                               "and choose a reason; approvers will see it."},
}


class CohortIndex:
    """Peer-cohort lookup over the training dataset, with a widening ladder so a
    cohort is never shown below the minimum aggregation threshold."""

    def __init__(self, csv_path: Path, predict_batch=None):
        self.predict_batch = predict_batch
        self.available = csv_path.exists()
        if self.available:
            raw = csv_path.read_bytes()
            self.dataset_sha = hashlib.sha256(raw).hexdigest()
            self.df = pd.read_csv(csv_path)
            mtime = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=timezone.utc)
            self.vintage = mtime.date().isoformat()
        else:
            self.dataset_sha = None
            self.df = pd.DataFrame()
            self.vintage = None

    def find(self, role, level, city, funding_stage, min_n: int) -> dict:
        if not self.available:
            return {"suppressed": True, "reason": "No benchmark dataset loaded.", "vintage": None}
        tier = CITIES[city][0]
        archetype = role_archetype_for(role)
        ladder = [
            # Market peers aren't filtered by health band: health doesn't set the market price.
            (f"{role} · {level} · {city} · {funding_stage}",
             {"role": role, "level": level, "city": city, "funding_stage": funding_stage}),
            (f"{role} · {level} · {tier} · {funding_stage}",
             {"role": role, "level": level, "saas_hub_tier": tier, "funding_stage": funding_stage}),
            (f"{role} · {level} · {funding_stage} (all cities)",
             {"role": role, "level": level, "funding_stage": funding_stage}),
            (f"{archetype} roles · {level} (all cities and stages)",
             {"role_archetype": archetype, "level": level}),
        ]
        df = self.df
        for rung, (label, filters) in enumerate(ladder):
            mask = pd.Series(True, index=df.index)
            for col, val in filters.items():
                mask &= df[col] == val
            sub = df.loc[mask, "cash_ctc_lpa"]
            if len(sub) >= min_n:
                ids = ",".join(str(i) for i in sorted(sub.index.tolist()))
                return {
                    "suppressed": False,
                    "definition": label,
                    "filters": filters,
                    "rung": rung,
                    "widened": rung > 0,
                    "n": int(len(sub)),
                    "companies": int(df.loc[mask, "company_id"].nunique()),
                    "empirical_p25": round(float(sub.quantile(0.25)), 2),
                    "empirical_p50": round(float(sub.quantile(0.50)), 2),
                    "empirical_p75": round(float(sub.quantile(0.75)), 2),
                    "vintage": self.vintage,
                    "source": DATA_SOURCE_LABEL,
                    "min_n": min_n,
                    # Pins the exact observations behind this cohort to this exact dataset file.
                    "cohort_hash": hashlib.sha256(f"{self.dataset_sha}:{ids}".encode()).hexdigest(),
                    "dataset_sha": self.dataset_sha,
                }
        return {
            "suppressed": True,
            "reason": f"No peer cohort reaches the minimum of {min_n} observations, even after widening.",
            "vintage": self.vintage,
            "min_n": min_n,
        }

    def reconcile(self, cohort: dict, profile: dict, p25: float, p50: float, p75: float) -> dict | None:
        """
        Explains why the model's range for THIS profile differs from the peer cohort's
        raw percentiles, instead of leaving two contradictory numbers on the page.

        The cohort's offers are priced by the same model at their own profiles. Then:
          raw cohort median -> model median for the cohort's own mix   = model error on these peers
          model median for the cohort's mix -> model P50 for this profile = composition
            (other cities in the tier, other target markets, years, skills)
        and the cohort's actual/model ratios, applied to this profile's P50, give a
        "cohort adjusted to this profile" range that is directly comparable.
        """
        if cohort.get("suppressed") or self.predict_batch is None or not self.available:
            return None
        mask = pd.Series(True, index=self.df.index)
        for col, val in cohort["filters"].items():
            mask &= self.df[col] == val
        rows = self.df.loc[mask]
        preds = self.predict_batch(rows)
        ratios = rows["cash_ctc_lpa"].to_numpy() / preds
        raw_p50 = cohort["empirical_p50"]
        model_mix_p50 = float(pd.Series(preds).median())
        ratio_med = float(pd.Series(ratios).median())
        adj = {q: round(p50 * float(pd.Series(ratios).quantile(v)), 2) for q, v in (("p25", .25), ("p50", .5), ("p75", .75))}

        mix = []
        n = len(rows)
        same_city = int((rows["city"] == profile["city"]).sum())
        if same_city < n:
            others = rows.loc[rows["city"] != profile["city"], "city"].value_counts()
            mix.append(f"{n - same_city} of {n} offers are outside {profile['city']} "
                       f"({', '.join(f'{c} {k}' for c, k in others.items())})")
        same_market = int((rows["target_market"] == profile["target_market"]).sum())
        if same_market < n:
            mix.append(f"{n - same_market} of {n} are from {'domestic' if profile['target_market'] == 'Cross-border' else 'cross-border'} "
                       f"companies; this offer is priced as {profile['target_market']}")
        yrs = float(rows["years_experience"].median())
        if abs(yrs - float(profile["years_experience"])) >= 1:
            mix.append(f"median experience in the cohort is {yrs:.1f} years vs {float(profile['years_experience']):g}")
        same_skill = int((rows["skill_category"] == profile["skill_category"]).sum())
        if same_skill < n:
            mix.append(f"{n - same_skill} of {n} have a different skill category than {profile['skill_category']}")

        gap = p50 - raw_p50
        composition = p50 - model_mix_p50
        model_error = model_mix_p50 - raw_p50
        model_vs_cohort_pct = round((1 / ratio_med - 1) * 100, 1) if ratio_med else None
        if abs(gap) < 0.05 * p50:
            summary = (f"The model's P50 (Rs.{p50:.1f}) and the cohort's raw median (Rs.{raw_p50:.1f}) agree within 5%.")
        else:
            err_pct = abs(model_error) / raw_p50 * 100
            cause = ("mostly the cohort's mix, not model error" if abs(composition) >= abs(model_error)
                     else "mostly model error on this cohort, not its mix")
            summary = (f"The model's P50 for this candidate (Rs.{p50:.1f} LPA) is {abs(gap) / raw_p50 * 100:.0f}% "
                       f"{'above' if gap > 0 else 'below'} the cohort's raw median (Rs.{raw_p50:.1f}). That gap is "
                       f"{cause}: priced at their own profiles, the model expects Rs.{model_mix_p50:.1f} for these "
                       f"{n} offers, {err_pct:.0f}% {'above' if model_error > 0 else 'below'} what they were actually "
                       f"paid. Moving from the cohort's mix to this candidate's profile adds Rs.{composition:+.1f}.")
        return {
            "summary": summary, "mix": mix, "n": n,
            "raw_p50": raw_p50, "model_p50_for_cohort_mix": round(model_mix_p50, 2),
            "composition_lpa": round(composition, 2), "model_error_lpa": round(model_error, 2),
            "model_vs_cohort_pct": model_vs_cohort_pct,
            "adjusted_p25": adj["p25"], "adjusted_p50": adj["p50"], "adjusted_p75": adj["p75"],
            "method": "Cohort offers priced by the same model at their own profiles; actual/model ratios applied "
                      "to this profile's P50.",
        }

    def band_seed(self, funding_stage: str | None = None, target_market: str | None = None) -> list[dict]:
        """Median cash CTC per role/level/city tier, for seeding demo bands. Uses
        companies like this one (same stage and target market) where there are
        at least 5 observations, otherwise the whole dataset."""
        if not self.available:
            return []
        keys = ["role", "level", "saas_hub_tier"]
        overall = self.df.groupby(keys)["cash_ctc_lpa"].median()
        similar = self.df
        if funding_stage:
            similar = similar[similar["funding_stage"] == funding_stage]
        if target_market:
            similar = similar[similar["target_market"] == target_market]
        grouped = similar.groupby(keys)["cash_ctc_lpa"].agg(["median", "size"])
        rows = []
        for key, overall_median in overall.items():
            median = grouped.loc[key, "median"] if key in grouped.index and grouped.loc[key, "size"] >= 5 else overall_median
            rows.append({"role": key[0], "level": key[1], "saas_hub_tier": key[2], "cash_ctc_lpa": float(median)})
        return rows


interpolate_percentile = sm.interpolate_percentile  # kept importable from here


def confidence(cohort: dict, profile: dict, metrics_age_days: int | None, settings: dict,
               level_inferred_mismatch: bool = False, affordability_missing: list | None = None) -> dict:
    """Score 0-100 with one plain reason per deduction."""
    reasons = []
    score = 100.0
    if cohort.get("suppressed"):
        return {"level": "Insufficient", "score": 0, "reasons": [cohort.get("reason", "Peer cohort below minimum size.")]}
    rung_penalty = {0: 0, 1: 10, 2: 20, 3: 35}[cohort["rung"]]
    if rung_penalty:
        score -= rung_penalty
        reasons.append(f"Exact peer cohort was too small, so it was widened to: {cohort['definition']}.")
    if cohort["n"] < 30:
        score -= 20
        reasons.append(f"Only {cohort['n']} peer observations.")
    rec = cohort.get("reconciliation") or {}
    if rec.get("model_vs_cohort_pct") is not None and abs(rec["model_vs_cohort_pct"]) > 10:
        score -= 10
        reasons.append(f"Priced at their own profiles, the model is {abs(rec['model_vs_cohort_pct']):.0f}% "
                       f"{'above' if rec['model_vs_cohort_pct'] > 0 else 'below'} what this peer cohort was "
                       f"actually paid.")
    # Observed-offer cohorts only: how many companies the data comes from, and how old it is.
    if cohort.get("contributors") is not None and cohort["contributors"] < 5:
        score -= 10
        reasons.append(f"Data comes from only {cohort['contributors']} contributing companies.")
    if cohort.get("median_age_months") is not None and cohort["median_age_months"] > 12:
        score -= 10
        reasons.append(f"Half the observed offers are more than {cohort['median_age_months']} months old.")
    if affordability_missing and metrics_age_days is not None:
        score -= 5
        reasons.append(f"{' and '.join(affordability_missing)} missing, so runway impact can't be calculated "
                       f"(the market range isn't affected).")
    filled = [c for c in profile.get("missing_impact", [])]
    if filled:
        missing_weight = 1 - profile["coverage"]
        score -= missing_weight * 15
        names = ", ".join(m["component"] for m in filled)
        reasons.append(f"{names} missing - {round(missing_weight * 100)}% of the health score uses "
                       f"{profile.get('funding_stage', 'funding-stage')} defaults, so the health-based target "
                       f"is less certain (the market range isn't affected).")
    stale_after = int(settings.get("metrics_stale_days", 180))
    if metrics_age_days is None:
        score -= 5
        reasons.append("No company metrics on file - health score is provisional, so the target position "
                       "is too. The market range doesn't use metrics.")
    elif metrics_age_days > stale_after:
        score -= 15
        reasons.append(f"Company metrics are {metrics_age_days} days old (stale after {stale_after}).")
    bench_stale = int(settings.get("benchmark_stale_days", 90))
    if cohort.get("vintage"):
        vintage_age = (date.today() - date.fromisoformat(cohort["vintage"])).days
        if vintage_age > bench_stale:
            score -= 20
            reasons.append(f"Benchmark data is {vintage_age} days old (older than {bench_stale}).")
    if level_inferred_mismatch:
        score -= 10
        reasons.append("Level is inferred from years of experience and doesn't match the requisition level.")
    score = max(0.0, score)
    level = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
    reasons.append(cohort.get("source", DATA_SOURCE_LABEL) + ".")
    return {"level": level, "score": round(score), "reasons": reasons}


def display_for(level: str, p25, p75) -> dict:
    policy = dict(DISPLAY_POLICY[level])
    if policy["mode"] != "none" and p25 is not None:
        policy["lo"] = round(p25 * (1 - policy["widen"]), 2)
        policy["hi"] = round(p75 * (1 + policy["widen"]), 2)
    else:
        policy["lo"] = policy["hi"] = None
    return policy


def esop_recommendation(esop_annual_lpa: float, health_score: float, settings: dict, esop_tilt: float = 1.0) -> dict:
    vesting_years = float(settings.get("default_vesting_years", 4))
    fmv = float(settings.get("fmv_per_share_inr", 0) or 0)
    fds = float(settings.get("fully_diluted_shares", 0) or 0)
    grant_value_lakh = esop_annual_lpa * vesting_years
    ev = sm.esop_expected_value(esop_annual_lpa, health_score)
    out = {
        "annual_fmv_lpa": round(esop_annual_lpa, 2),
        "grant_value_lakh_mid": round(grant_value_lakh, 2),
        "vesting_years": vesting_years,
        "expected_value_annual_lpa": ev["expected_value_lpa"],
        "liquidity_probability": ev["liquidity_probability"],
        "payout_multiple": ev["payout_multiple"],
        "band": ev["band"],
        "fmv_per_share_inr": fmv,
        "esop_tilt": esop_tilt,
    }
    if fmv > 0 and grant_value_lakh > 0:
        mid = grant_value_lakh * 1e5 / fmv
        out.update(options_low=int(round(mid * 0.75, -1)), options_mid=int(round(mid, -1)),
                   options_high=int(round(mid * 1.25, -1)))
        if fds > 0:
            out["dilution_mid_pct"] = round(mid / fds * 100, 4)
    else:
        out.update(options_low=0, options_mid=0, options_high=0)
    return out


def _lpa(v):
    return f"Rs.{v:.1f} LPA"


def _common(company, profile, offer, model_level, model_version, mode, band, accepted_peer, settings):
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "model_version": model_version,
        "health_score_version": profile["version"],
        "data_mode": mode,
        "inputs": {
            "role": offer["role"], "years_experience": offer["years_experience"], "city": offer["city"],
            "funding_stage": company["funding_stage"], "target_market": company["target_market"],
            "pricing_model": company["pricing_model"], "skill_category": offer["skill_category"],
            "education_pedigree": offer["education_pedigree"], "health_score_override": profile["health_score"],
            "level": offer["level"], "model_level": model_level,
        },
        "health": {"score": profile["health_score"], "band": profile["health_band"],
                   "coverage": profile["coverage"], "stage_default": profile["stage_default_health"]},
        "band": dict(band) if band is not None else None,
        "accepted_peer": accepted_peer,
        "policy_percentile": float(settings.get("target_percentile", 50)),
        "target_percentile": float(settings.get("target_percentile", 50)),
        "stage_esop_ratio": FUNDING_STAGES[company["funding_stage"]]["esop_ratio"],
    }


def unavailable_benchmark(*, company, profile, offer, settings, band, accepted_peer, model_version, mode, reason):
    model_level = experience_to_level(float(offer["years_experience"]))
    bm = _common(company, profile, offer, model_level, model_version, mode, band, accepted_peer, settings)
    bm.update({
        "p25": None, "p50": None, "p75": None, "target": None, "positioning": None,
        "cohort": {"suppressed": True, "reason": reason, "vintage": None},
        "confidence": {"level": "Insufficient", "score": 0, "reasons": [reason]},
        "display": display_for("Insufficient", None, None),
        "drivers": [reason], "shap_drivers": [], "shap_base_lpa": None, "segment_median": None,
        "compa_target_vs_peer": None, "compa_target_vs_band": None,
        "esop": {"options_low": 0, "options_mid": 0, "options_high": 0, "annual_fmv_lpa": 0, "vesting_years":
                 float(settings.get("default_vesting_years", 4)), "fmv_per_share_inr": settings.get("fmv_per_share_inr")},
    })
    return bm


def build_benchmark(predict_fn, cohorts: CohortIndex, *, company, profile: dict, offer: dict,
                    settings: dict, band, accepted_peer, metrics_age_days, model_version: str,
                    mode: str = "demo", affordability_missing: list | None = None) -> dict:
    """offer: role, years_experience, city, skill_category, education_pedigree, level,
    expected_ctc, competing_offer. band: sqlite Row or None. accepted_peer: dict from
    routes.accepted_peer_stats()."""
    if mode != "demo":
        return unavailable_benchmark(
            company=company, profile=profile, offer=offer, settings=settings, band=band, accepted_peer=accepted_peer,
            model_version=model_version, mode=mode,
            reason=f"This workspace is in {mode} mode, so the synthetic demo model is switched off and no real-data "
                   f"benchmark source is connected yet.")

    stage = company["funding_stage"]
    form = {
        "role": offer["role"], "years_experience": offer["years_experience"], "city": offer["city"],
        "funding_stage": stage, "target_market": company["target_market"],
        "pricing_model": company["pricing_model"], "skill_category": offer["skill_category"],
        "education_pedigree": offer["education_pedigree"],
        "health_score_override": profile["health_score"],
    }
    policy_pct = float(settings.get("target_percentile", 50))
    positioning_on = bool(settings.get("health_positioning", True))
    result = predict_fn(form, apply_signals=False, policy_percentile=policy_pct, positioning_enabled=positioning_on)

    p25, p50, p75 = result["p25"], result["p50"], result["p75"]
    assert p25 <= p50 <= p75  # BM-08: generate_prediction sorts the quantiles
    pos = result["positioning"]
    pct = pos["target_percentile"]
    target = result["target"]

    model_level = result["level"]
    cohort = cohorts.find(offer["role"], model_level, offer["city"], stage, int(settings.get("min_cohort_size", 10)))
    if not cohort.get("suppressed"):
        cohort["reconciliation"] = cohorts.reconcile(
            cohort, {"city": offer["city"], "target_market": company["target_market"],
                     "years_experience": offer["years_experience"], "skill_category": offer["skill_category"]},
            p25, p50, p75)
    conf = confidence(cohort, profile, metrics_age_days, settings,
                      level_inferred_mismatch=model_level != offer["level"],
                      affordability_missing=affordability_missing)
    display = display_for(conf["level"], p25, p75)

    shap_drivers = [
        {"feature": c["feature"], "value": c["display_value"], "lpa": round(c["lpa"], 2)}
        for c in result["contributions"][:5]
    ]

    drivers = []
    if display["show_target"]:
        if pos["percentile_shift"]:
            drivers.append(f"Your policy targets P{policy_pct:g}. Health score {pos['health_score']:.0f}/100 "
                           f"({pos['band']}) moves that to P{pct:g}, which gives {_lpa(target)}. {pos['stance']}")
        else:
            drivers.append(f"Your policy targets P{pct:g} of the market range, which gives {_lpa(target)}. "
                           f"{pos['stance']}")
    drivers.append(f"The market range is the same for any {stage} company: company health doesn't change what "
                   f"the market pays, only where you aim in it and how much you put in ESOP.")
    if pos["esop_tilt"] != 1.0:
        drivers.append(f"ESOP is tilted up {round((pos['esop_tilt'] - 1) * 100)}% to make up for lower cash. "
                       f"At this health band a candidate can expect to realise about "
                       f"{round(pos['esop_value_ratio'] * 100)}% of paper value, so equity won't fully close a cash gap.")
    if model_level != offer["level"]:
        drivers.append(f"The model prices {offer['years_experience']:g} years of experience as {model_level}; "
                       f"the requisition is for {offer['level']}.")
    if offer.get("competing_offer"):
        co = offer["competing_offer"]
        where = ("above" if co > display["hi"] else "inside" if co >= display["lo"] else "below") if display["lo"] else "-"
        drivers.append(f"Candidate holds a competing offer of {_lpa(co)}, {where} the shown market range.")
    if offer.get("expected_ctc") and display["show_target"]:
        ex = offer["expected_ctc"]
        drivers.append(f"Candidate expects {_lpa(ex)}, {(ex / target - 1) * 100:+.1f}% versus the target.")
    if band is not None:
        drivers.append(f"Internal band for {offer['role']} / {offer['level']}: "
                       f"Rs.{band['min_lpa']:.1f}-{band['max_lpa']:.1f} LPA (midpoint Rs.{band['mid_lpa']:.1f}).")

    bm = _common(company, profile, offer, model_level, model_version, mode, band, accepted_peer, settings)
    bm.update({
        "p25": round(p25, 2), "p50": round(p50, 2), "p75": round(p75, 2),
        "target": round(target, 2),
        "target_percentile": pct,
        "positioning": pos,
        "cohort": cohort,
        "confidence": conf,
        "display": display,
        "drivers": drivers,
        "shap_drivers": shap_drivers,
        "shap_base_lpa": round(result["base_lpa"], 2),
        "segment_median": round(result["segment_median"], 2) if result["segment_median"] else None,
        "compa_target_vs_peer": round(target / p50, 2) if p50 else None,
        "compa_target_vs_band": round(target / band["mid_lpa"], 2) if band is not None and band["mid_lpa"] else None,
        "esop": esop_recommendation(result["esop_annualized_lpa"], profile["health_score"], settings,
                                    esop_tilt=pos["esop_tilt"]),
    })
    return bm
