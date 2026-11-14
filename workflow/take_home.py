"""
Candidate take-home estimate (CT-05). Decision support only.

Versioned like the CTC ruleset so an estimate can always be traced to the slab
table it used. Assumptions, all shown to the user:

  * new tax regime (s.115BAC), slabs and rebate as in the ruleset below;
  * standard deduction applied; no other deductions or exemptions (HRA exemption
    isn't available under the new regime);
  * variable pay is paid in full;
  * employee EPF is 12% of Basic, with no wage ceiling;
  * a flat professional-tax figure (it varies by state; some states levy none);
  * surcharge applied without marginal relief, so estimates just above Rs.50 lakh /
    Rs.1 crore / Rs.2 crore taxable income are slightly high.

Slabs change with each Finance Act. Check the current year's slabs before relying
on this, and update TAX_RULESET when they change.
"""
from __future__ import annotations

TAX_RULESET = {
    "id": "IN-TAX-NEW",
    "version": "FY2025-26",
    "effective_from": "2025-04-01",
    "source": "Finance Act 2025, new tax regime (s.115BAC): 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, "
              "20-24L 25%, above 24L 30%; standard deduction Rs.75,000; s.87A rebate up to Rs.60,000 (taxable income "
              "up to Rs.12 lakh); 4% health and education cess.",
}

SLABS = [(400_000, 0.00), (800_000, 0.05), (1_200_000, 0.10), (1_600_000, 0.15), (2_000_000, 0.20),
         (2_400_000, 0.25), (float("inf"), 0.30)]
STANDARD_DEDUCTION = 75_000
REBATE_INCOME_LIMIT = 1_200_000
CESS = 0.04
SURCHARGE = [(20_000_000, 0.25), (10_000_000, 0.15), (5_000_000, 0.10)]  # new regime caps surcharge at 25%
EMPLOYEE_PF_PCT = 0.12
PROFESSIONAL_TAX = 2_500


def income_tax(taxable: float) -> float:
    """Annual tax including cess, under the new regime."""
    tax, lower = 0.0, 0.0
    for upper, rate in SLABS:
        if taxable > lower:
            tax += (min(taxable, upper) - lower) * rate
        lower = upper
    if taxable <= REBATE_INCOME_LIMIT:
        tax = 0.0
    else:
        tax = min(tax, taxable - REBATE_INCOME_LIMIT)  # marginal relief just above the rebate limit
    for threshold, rate in SURCHARGE:
        if taxable > threshold:
            tax *= 1 + rate
            break
    return round(tax * (1 + CESS))


def estimate_take_home(structure: dict) -> dict:
    """structure: output of checks.structure_ctc (whole rupees)."""
    rows = {r["component"]: r["inr"] for r in structure["rows"]}
    basic = rows["Basic + DA"]
    gross = structure["total_inr"] - rows["Employer EPF"] - rows["Gratuity (accrued)"]
    employee_pf = round(basic * EMPLOYEE_PF_PCT)
    taxable = max(0, gross - STANDARD_DEDUCTION)
    tax = income_tax(taxable)
    net = gross - employee_pf - PROFESSIONAL_TAX - tax
    return {
        "gross_salary_inr": gross,
        "employee_pf_inr": employee_pf,
        "professional_tax_inr": PROFESSIONAL_TAX,
        "taxable_income_inr": taxable,
        "income_tax_inr": tax,
        "annual_take_home_inr": net,
        "monthly_take_home_inr": round(net / 12),
        "employer_cost_inr": structure["total_inr"],
        "ruleset": f"{TAX_RULESET['id']} {TAX_RULESET['version']}",
        "assumptions": ("New tax regime; standard deduction only; variable paid in full; employee EPF 12% of Basic "
                        f"without the wage ceiling; professional tax Rs.{PROFESSIONAL_TAX:,}/year (varies by state)."),
    }
