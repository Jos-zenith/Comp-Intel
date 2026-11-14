"""
Market clearing price from offer-outcome telemetry.

Most compensation data only sees offers that were accepted, so it can tell you
what people were paid but not what it took to get a yes. Declines and counters
are the missing half: a declined offer is a price the market refused.

From the decided offers in a cohort's rolling window this module fits an
acceptance curve, P(accept | cash offer), and reads off:

  clearing price   the offer at which acceptance reaches 50% - the median
                   candidate's reservation price for this cohort
  close price      the offer at which acceptance reaches the close-rate target
                   (Settings, default 80%)
  decline at P50   how often an offer at the accepted-offer P50 is declined;
                   at or above the alert threshold (default 50%) the cohort's
                   P50 has stopped clearing - the "market moved" signal

How each outcome becomes evidence (amount, accepted?):
  accepted, no negotiation    (final, yes)
  accepted after negotiation  (opening offer, no) and (final, yes): the opening
                              number didn't clear, the final one did
  declined                    (offer, no)
  declined with a counter     also (counter, yes) at half weight - a counter is
                              the candidate's stated price, but it's an ask, not
                              a transaction
  withdrawn / sent            ignored (no candidate decision on price)

The curve is a weighted isotonic regression (pool-adjacent-violators): it only
assumes that a higher offer is never less likely to be accepted. No logistic
shape, no parameters to tune. Recent offers carry more weight (same recency
weights as the live percentiles). Nothing is shown below the privacy thresholds.
"""
from __future__ import annotations

COUNTER_WEIGHT = 0.5
NEAR_P50_BAND = 0.05  # +/-5% of P50 counts as "a P50 offer" for the empirical check
CURVE_RATIOS = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3)
METHOD = ("Weighted isotonic acceptance curve over decided offers (accepts, declines, negotiated openings; "
          "counters at half weight)")


def evidence(rows: list[dict], weights: list[float]) -> list[tuple[float, int, float]]:
    """(amount_lpa, accepted 0/1, weight) points; see module docstring."""
    pts = []
    for r, w in zip(rows, weights):
        status = r["status"]
        initial, final, counter = r.get("initial_cash_ctc"), r.get("final_cash_ctc"), r.get("counter_cash_ctc")
        if status == "accepted" and final:
            if initial and final > initial * 1.005:
                pts.append((float(initial), 0, w))
            pts.append((float(final), 1, w))
        elif status == "declined":
            offered = initial or final
            if offered:
                pts.append((float(offered), 0, w))
                if counter and counter > offered:
                    pts.append((float(counter), 1, w * COUNTER_WEIGHT))
    return pts


def isotonic_fit(points: list[tuple[float, int, float]]) -> list[tuple[float, float]]:
    """Weighted pool-adjacent-violators for a non-decreasing fit. Returns
    (x, fitted p) at each distinct x, in ascending x."""
    by_x: dict[float, list[float]] = {}
    for x, y, w in points:
        acc = by_x.setdefault(x, [0.0, 0.0])
        acc[0] += y * w
        acc[1] += w
    xs = sorted(by_x)
    # blocks: [weighted sum, weight, first index, last index]
    blocks: list[list[float]] = []
    for i, x in enumerate(xs):
        s, w = by_x[x]
        blocks.append([s, w, i, i])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s2, w2, _a, b = blocks.pop()
            blocks[-1][0] += s2
            blocks[-1][1] += w2
            blocks[-1][3] = b
    fitted = [0.0] * len(xs)
    for s, w, a, b in blocks:
        for i in range(int(a), int(b) + 1):
            fitted[i] = s / w
    return list(zip(xs, fitted))


def predict(curve: list[tuple[float, float]], x: float) -> float:
    """Linear interpolation on the fitted curve, flat outside the observed range."""
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def price_for(curve: list[tuple[float, float]], target: float) -> dict:
    """Lowest offer where the fitted acceptance reaches `target`."""
    if curve[0][1] >= target:
        return {"lpa": round(curve[0][0], 2), "bound": "at_or_below",
                "note": "Every offer in the observed range already clears this rate; the true price may be lower."}
    if curve[-1][1] < target:
        return {"lpa": round(curve[-1][0], 2), "bound": "above",
                "note": "No observed offer reached this acceptance rate; the price is above the highest offer seen."}
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if y0 < target <= y1:
            x = x0 + (x1 - x0) * (target - y0) / (y1 - y0) if y1 > y0 else x1
            return {"lpa": round(x, 2), "bound": "estimate", "note": None}
    return {"lpa": round(curve[-1][0], 2), "bound": "above", "note": None}


def _decline_at(curve, p50):
    return round((1 - predict(curve, p50)) * 100, 1)


def compute(decided: list[dict], weights: list[float], p50: float | None, settings: dict,
            previous: tuple[list[dict], list[float]] | None = None, anchor_label: str = "P50") -> dict:
    """decided: accepted/declined observation rows in the window (with contributor_key).
    p50: the anchor - the benchmark P50 offers are made against (streaming passes the
    long-lookback accepted-offer P50); None when there isn't one.
    previous: the prior window's rows and weights, for the trend."""
    min_n = int(settings.get("min_cohort_size", 10))
    min_contrib = int(settings.get("min_contributors", 3))
    alert_pct = float(settings.get("clearing_alert_decline_pct", 50))
    close_rate = float(settings.get("clearing_close_rate_pct", 80)) / 100
    accepted = sum(1 for r in decided if r["status"] == "accepted")
    declined = sum(1 for r in decided if r["status"] == "declined")
    contributors = len({r["contributor_key"] for r in decided})
    base = {"method": METHOD, "anchor_p50": p50, "anchor_label": anchor_label,
            "n_decided": len(decided), "accepted": accepted, "declined": declined,
            "contributors": contributors, "alert": False, "alert_threshold_pct": alert_pct,
            "close_rate_pct": round(close_rate * 100), "clearing_price_lpa": None, "close_price_lpa": None,
            "decline_at_p50_pct": None, "near_p50": None, "previous_decline_at_p50_pct": None, "trend_pp": None}
    if len(decided) < min_n or contributors < min_contrib:
        return {**base, "status": "insufficient",
                "message": f"Needs {min_n} decided offers from {min_contrib} companies in the window "
                           f"(has {len(decided)} from {contributors})."}
    if p50 is None:
        return {**base, "status": "insufficient", "message": "No accepted-offer P50 for this cohort yet."}
    if declined == 0:
        return {**base, "status": "all_accepted",
                "message": "Every decided offer in the window was accepted, so there's no decline signal: the "
                           "clearing price is at or below the lowest accepted offer."}
    pts = evidence(decided, weights)
    curve = isotonic_fit(pts)
    clearing = price_for(curve, 0.5)
    close = price_for(curve, close_rate)
    decline_p50 = _decline_at(curve, p50)
    near = [(x, y) for x, y, _w in pts if abs(x / p50 - 1) <= NEAR_P50_BAND]
    out = {**base, "status": "ok",
           "clearing_price_lpa": clearing["lpa"], "clearing_bound": clearing["bound"], "clearing_note": clearing["note"],
           "close_price_lpa": close["lpa"], "close_bound": close["bound"],
           "clearing_vs_p50_pct": round((clearing["lpa"] / p50 - 1) * 100, 1),
           "decline_at_p50_pct": decline_p50,
           "near_p50": {"n": len(near), "decline_pct": round(sum(1 for _x, y in near if y == 0) / len(near) * 100)
                        if near else None},
           # Acceptance at fixed multiples of P50, never the individual offer amounts behind the curve.
           "curve_by_p50": [{"ratio": r, "lpa": round(p50 * r, 2), "accept_pct": round(predict(curve, p50 * r) * 100)}
                            for r in CURVE_RATIOS]}
    if previous and previous[0]:
        prev_rows, prev_w = previous
        prev_pts = evidence(prev_rows, prev_w)
        if prev_pts and any(y == 0 for _x, y, _w in prev_pts) and len(prev_rows) >= min_n:
            prev = _decline_at(isotonic_fit(prev_pts), p50)
            out["previous_decline_at_p50_pct"] = prev
            out["trend_pp"] = round(decline_p50 - prev, 1)
    out["alert"] = decline_p50 >= alert_pct
    if out["alert"]:
        trend = (f", up {out['trend_pp']:g} points on the previous window" if out["trend_pp"] and out["trend_pp"] > 0
                 else "")
        out["message"] = (f"Offers at the benchmark P50 (Rs.{p50:.1f} LPA, {anchor_label}) are being declined "
                          f"{decline_p50:.0f}% of the time{trend}. The market is clearing around "
                          f"Rs.{clearing['lpa']:.1f} LPA ({out['clearing_vs_p50_pct']:+.1f}% vs that P50).")
    else:
        out["message"] = (f"Offers at the benchmark P50 (Rs.{p50:.1f} LPA, {anchor_label}) are declined "
                          f"{decline_p50:.0f}% of the time; the clearing price is Rs.{clearing['lpa']:.1f} LPA "
                          f"({out['clearing_vs_p50_pct']:+.1f}% vs that P50).")
    return out
