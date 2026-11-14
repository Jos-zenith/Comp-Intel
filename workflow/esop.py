"""
ESOP grant scenarios (PRD v2 section 10.5, ES-01 to ES-07).

Illustrative, not option pricing and not investment advice. A private-company
option has no observable volatility or liquid market, so instead of a single
"value" this shows three exit scenarios side by side, with every assumption on
screen:

  exit value per share  = current FMV x exit multiple x (1 - future dilution)
  gain per option       = max(0, exit value per share - strike price)
  gross gain            = options x gain per option
  illustrative net      = gross gain x (1 - flat tax rate)

The tax line is a deliberate simplification. In India the spread at exercise is
taxed as a perquisite at slab rates and the later sale as capital gains; one
flat, configurable rate stands in for both so the order of magnitude is visible.
"""
from __future__ import annotations

ESOP_SCENARIO_VERSION = "ESOP-SC-1.0"

SCENARIOS = [
    ("Conservative", "esop_exit_conservative_x"),
    ("Base", "esop_exit_base_x"),
    ("Upside", "esop_exit_upside_x"),
]

DEFAULTS = {
    "strike_price_inr": 10.0,
    "esop_exit_conservative_x": 0.5,
    "esop_exit_base_x": 2.0,
    "esop_exit_upside_x": 5.0,
    "esop_future_dilution_pct": 30.0,
    "esop_tax_rate_pct": 30.0,
}

LABEL = "Illustrative scenarios - not option pricing, a valuation or investment advice."


def _num(settings: dict, key: str) -> float:
    value = settings.get(key, DEFAULTS[key])
    return float(DEFAULTS[key] if value in (None, "") else value)


def scenarios(options: int, settings: dict, strike: float | None = None) -> dict | None:
    """None when there is no grant or no FMV to anchor scenarios to."""
    fmv = float(settings.get("fmv_per_share_inr", 0) or 0)
    if not options or options <= 0 or fmv <= 0:
        return None
    strike = _num(settings, "strike_price_inr") if strike is None else float(strike)
    fds = float(settings.get("fully_diluted_shares", 0) or 0)
    dilution = _num(settings, "esop_future_dilution_pct") / 100
    tax = _num(settings, "esop_tax_rate_pct") / 100
    rows = []
    for name, key in SCENARIOS:
        multiple = _num(settings, key)
        exit_value = fmv * multiple * (1 - dilution)
        gain = max(0.0, exit_value - strike)
        gross = options * gain / 1e5
        rows.append({
            "name": name,
            "exit_multiple": multiple,
            "exit_value_per_share": round(exit_value, 2),
            "gain_per_option": round(gain, 2),
            "gross_lakh": round(gross, 2),
            "net_lakh": round(gross * (1 - tax), 2),
            "underwater": exit_value <= strike,
        })
    ownership = options / fds * 100 if fds else None
    return {
        "version": ESOP_SCENARIO_VERSION,
        "options": int(options),
        "strike_price_inr": strike,
        "fmv_per_share_inr": fmv,
        "exercise_cost_lakh": round(options * strike / 1e5, 2),
        "spread_now_lakh": round(options * max(0.0, fmv - strike) / 1e5, 2),
        "underwater_now": strike >= fmv,
        "ownership_pct": round(ownership, 4) if ownership is not None else None,
        "ownership_after_dilution_pct": round(ownership * (1 - dilution), 4) if ownership is not None else None,
        "future_dilution_pct": round(dilution * 100, 1),
        "tax_rate_pct": round(tax * 100, 1),
        "scenarios": rows,
        "any_underwater": any(r["underwater"] for r in rows),
        "label": LABEL,
    }
