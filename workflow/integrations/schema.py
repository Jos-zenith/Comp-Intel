"""
The unified OfferEvent: every HRIS/ATS payload, whatever its shape, is mapped into
this one record before anything else touches it.

    role_id, role, level, city, fixed_ctc_lpa, variable_ctc_lpa, esop_grant,
    company_stage, status, timestamp (+ counter, decline reason, kind)

It deliberately has no candidate name, email, phone or employee name field, so
nothing personal can leak downstream. The provider's own record id is kept only
as a salted hash (source_ref) for idempotency and status updates.

Normalisation is strict where a guess would corrupt the benchmark:
  * money must be INR. There's no FX conversion: an earlier version of this
    project applied a flat USD->INR rate and called it an India estimate, which
    was wrong (see README). Non-INR records are rejected, not converted.
  * the role, level and city must map onto the model's vocabulary
    (saas_domain.py); an unmapped title or a city we don't model is rejected
    with a reason, never bucketed into the nearest match.
  * amounts outside Rs.1-500 LPA are rejected as a likely unit error
    (monthly vs annual, paise vs rupees).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from saas_domain import CITIES, FUNDING_STAGES, ROLE_ARCHETYPES, experience_to_level, normalize_city

SCHEMA_VERSION = "offer-event/1"
STATUSES = ("sent", "accepted", "declined", "withdrawn", "active", "ignored")
MIN_LPA, MAX_LPA = 1.0, 500.0

ALL_ROLES = [r for roles in ROLE_ARCHETYPES.values() for r in roles]

# Most specific first: "QA Engineer" must not fall through to Software Engineer.
ROLE_PATTERNS = [
    (r"\b(machine learning|ml|ai|mlops|applied scientist|llm|gen ?ai)\b", "ML / AI Engineer"),
    (r"\bdata scien", "Data Scientist"),
    (r"\b(devops|sre|site reliability|cloud|platform|infrastructure|infra)\b", "DevOps / Cloud Engineer"),
    (r"\b(qa|quality|test|sdet|automation tester)\b", "QA Engineer"),
    (r"\b(product manager|product owner|apm|pm)\b", "Product Manager"),
    (r"\b(ui|ux|product designer|designer|interaction design)\b", "UI/UX Designer"),
    (r"\b(sdr|bdr|sales development|business development rep)", "Sales Development Rep (SDR)"),
    (r"\b(enterprise sales|sales manager|head of sales|regional sales)\b", "Enterprise Sales Manager"),
    (r"\b(account executive|ae)\b", "Account Executive"),
    (r"\b(customer success|csm|account manager)\b", "Customer Success Manager"),
    (r"\b(marketing|growth marketer|demand gen|content marketing|product marketing)\b", "Marketing Manager"),
    (r"\b(sde|swe|software|developer|programmer|backend|back-end|frontend|front-end|full ?stack|"
     r"member of technical staff|mts|engineer)\b", "Software Engineer"),
]

# Explicit level labels (from a grade/band field, or the title itself).
LEVEL_PATTERNS = [
    (r"\b(lead|principal|staff|architect|head|director|l[6-9]|sde[- ]?(iv|4)|e[6-9])\b", "Lead / Principal"),
    (r"\b(senior|sr\.?|sde[- ]?(iii|3)|l[45]|e5|grade c)\b", "Senior"),
    (r"\b(mid|intermediate|sde[- ]?(ii|2)|l3|e4|grade b)\b", "Mid"),
    (r"\b(junior|jr\.?|associate|entry|graduate|fresher|trainee|intern|sde[- ]?(i|1)|l[12]|e3|grade a)\b", "Junior"),
]
LEVELS = {"Junior", "Mid", "Senior", "Lead / Principal"}

ANNUAL = {"annual", "annually", "yearly", "year", "per-year", "per-year-salary", "per_year", "pa", "p.a.", "yr", "ctc"}
MONTHLY = {"monthly", "month", "per-month", "per-month-salary", "per_month", "pm", "p.m.", "mo"}


class NormalizationError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass
class OfferEvent:
    provider: str
    source_ref: str            # salted hash of the provider's record id
    kind: str                  # "offer" (ATS) or "payroll" (HRIS)
    status: str                # sent | accepted | declined | withdrawn | active | ignored
    role_id: str               # stable slug, e.g. "software-engineer"
    role: str
    level: str
    city: str
    city_tier: str
    fixed_ctc_lpa: float
    variable_ctc_lpa: float
    esop_grant: int | None
    company_stage: str | None
    timestamp: str             # ISO date-time (UTC) the provider says this happened
    counter_cash_ctc_lpa: float | None = None
    decline_reason: str | None = None
    joined_on: str | None = None
    schema_version: str = SCHEMA_VERSION
    notes: list = field(default_factory=list)

    @property
    def cash_ctc_lpa(self) -> float:
        return round(self.fixed_ctc_lpa + self.variable_ctc_lpa, 2)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["cash_ctc_lpa"] = self.cash_ctc_lpa
        return d


def role_id(role: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")


def map_role(title) -> str | None:
    if not title:
        return None
    text = str(title).strip()
    if text in ALL_ROLES:
        return text
    low = text.lower()
    for pattern, role in ROLE_PATTERNS:
        if re.search(pattern, low):
            return role
    return None


def map_level(level_raw, title=None, years=None) -> str | None:
    for candidate in (level_raw, title):
        if candidate in (None, ""):
            continue
        text = str(candidate).strip()
        if text in LEVELS:
            return text
        low = text.lower()
        for pattern, level in LEVEL_PATTERNS:
            if re.search(pattern, low):
                return level
    if years not in (None, ""):
        try:
            return experience_to_level(float(years))
        except (TypeError, ValueError):
            return None
    return None


def parse_money_inr(value) -> float | None:
    """Rupees from 1800000 / "18,00,000" / "Rs. 18 LPA" / "₹1.8 Cr" / "18L" / "150k"."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    text = re.sub(r"(₹|rs\.?|inr)", "", text).replace(",", "").strip()
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(lpa|lakhs?|lacs?|l|crores?|cr|k)?", text)
    if not m:
        raise NormalizationError("amount", f"can't read {value!r} as a rupee amount")
    number = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("lpa", "lakh", "lakhs", "lac", "lacs", "l"):
        return number * 1e5
    if unit in ("cr", "crore", "crores"):
        return number * 1e7
    if unit == "k":
        return number * 1e3
    return number


def _period_multiplier(period) -> int:
    if period in (None, ""):
        return 1
    p = str(period).strip().lower()
    if p in ANNUAL:
        return 1
    if p in MONTHLY:
        return 12
    raise NormalizationError("pay_period", f"{period!r} isn't an annual or monthly amount (hourly/daily pay is "
                                           f"not modelled)")


def _to_lpa(amount, period, scale, label) -> float | None:
    rupees = parse_money_inr(amount)
    if rupees is None:
        return None
    lpa = rupees * scale * _period_multiplier(period) / 1e5
    return round(lpa, 2)


def parse_timestamp(value) -> datetime:
    if value in (None, ""):
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc)  # epoch ms or s
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise NormalizationError("timestamp", f"can't read {value!r} as a date")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize(raw: dict, *, provider: str, kind: str, default_currency: str | None, source_ref: str,
              company_stage: str | None, new_joiner_days: int = 60) -> OfferEvent:
    """raw: the canonical intermediate dict an adapter produced (keys: id, status,
    title, level, years_experience, location, currency, fixed_amount, pay_period,
    amount_scale, variable_amount, variable_pct, esop_options, stage, timestamp,
    counter_amount, decline_reason, joined_on)."""
    notes = []
    status = raw.get("status")
    if status not in STATUSES:
        raise NormalizationError("status", f"unmapped status {raw.get('status_raw', status)!r}")

    currency = (str(raw.get("currency") or default_currency or "")).strip().upper()
    if not currency:
        raise NormalizationError("currency", "missing; this provider doesn't imply INR, so the currency must be sent")
    if currency not in ("INR", "RS", "RUPEE", "RUPEES"):
        raise NormalizationError("currency", f"{currency} isn't INR. Non-INR offers are rejected, not converted")

    role = map_role(raw.get("title"))
    if role is None:
        raise NormalizationError("title", f"{raw.get('title')!r} doesn't map to a modelled role")
    level = map_level(raw.get("level"), raw.get("title"), raw.get("years_experience"))
    if level is None:
        raise NormalizationError("level", "no level, grade or years of experience to infer one from")
    city = normalize_city(raw.get("location"))
    if city is None:
        raise NormalizationError("location", f"{raw.get('location')!r} isn't a city in the model "
                                             f"({', '.join(CITIES)})")

    scale = float(raw.get("amount_scale") or 1.0)
    fixed = _to_lpa(raw.get("fixed_amount"), raw.get("pay_period"), scale, "fixed")
    if fixed is None:
        raise NormalizationError("fixed_amount", "no fixed pay / CTC in the record")
    variable = _to_lpa(raw.get("variable_amount"), raw.get("pay_period"), scale, "variable")
    if variable is None and raw.get("variable_pct") not in (None, ""):
        try:
            variable = round(fixed * float(raw["variable_pct"]) / 100, 2)
        except (TypeError, ValueError):
            raise NormalizationError("variable_pct", "must be a number")
    variable = variable or 0.0
    if raw.get("amount_is_total_ctc"):  # some HRIS send total CTC with the variable inside it
        fixed = round(fixed - variable, 2)
    if not (MIN_LPA <= fixed + variable <= MAX_LPA):
        raise NormalizationError("fixed_amount", f"Rs.{fixed + variable:.2f} LPA is outside Rs.{MIN_LPA:g}-"
                                                 f"{MAX_LPA:g} LPA; probably a unit error (monthly vs annual?)")
    counter = _to_lpa(raw.get("counter_amount"), raw.get("pay_period"), scale, "counter")

    esop = raw.get("esop_options")
    try:
        esop = int(float(str(esop).replace(",", ""))) if esop not in (None, "") else None
    except ValueError:
        raise NormalizationError("esop_options", f"{esop!r} isn't a number of options")

    stage = raw.get("stage") if raw.get("stage") in FUNDING_STAGES else company_stage
    when = parse_timestamp(raw.get("timestamp"))
    joined = None
    if raw.get("joined_on") not in (None, ""):
        joined = parse_timestamp(raw["joined_on"]).date()

    if kind == "payroll" and status == "active":
        # Incumbent pay isn't an offer price. A payroll record only counts as an
        # accepted offer when the person joined recently (their first structure).
        if joined and (when.date() - joined).days <= new_joiner_days:
            status = "accepted"
            notes.append(f"New joiner (joined {joined.isoformat()}): payroll record treated as the accepted offer.")
        else:
            status = "active"
            notes.append("Incumbent payroll record: stored for the integration log, not used as a market offer.")

    return OfferEvent(provider=provider, source_ref=source_ref, kind=kind, status=status, role_id=role_id(role),
                      role=role, level=level, city=city, city_tier=CITIES[city][0], fixed_ctc_lpa=fixed,
                      variable_ctc_lpa=variable, esop_grant=esop, company_stage=stage,
                      timestamp=when.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
                      counter_cash_ctc_lpa=counter, decline_reason=(str(raw["decline_reason"])[:80]
                                                                    if raw.get("decline_reason") else None),
                      joined_on=joined.isoformat() if joined else None, notes=notes)


def today() -> date:
    return date.today()
