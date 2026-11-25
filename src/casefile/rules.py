"""The Halvorne Mutual claims manual, as code.

Everything here is deterministic and works in integer cents. It is used in three places:

* ``datagen.adjudicate`` computes ground truth from the true world facts;
* the investigator's ``price_check`` tool applies the estimate rules to *extracted* line items;
* the reviewer's rules cross-check re-derives the decision from the investigator's findings.

The prose version of the same rules lives in ``data/spec/claims_manual.md`` and is what the
LLM agents read. Keep the two in sync (``tests/test_rules.py`` pins the numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# tolerances and thresholds
PART_TOLERANCE = 0.15  # billed part price may exceed the reference price by up to 15%
HOURS_TOLERANCE = 0.15  # billed labor/paint hours may exceed reference hours by up to 15%
RATE_TOLERANCE = 0.10  # billed hourly rates may exceed the state rate by up to 10%
TOTAL_LOSS_RATIO = 0.75  # approved repair cost above 75% of ACV -> total loss
DATE_CONFLICT_DAYS = 1  # FNOL vs police report loss date may differ by at most 1 day
PRIOR_DAMAGE_WINDOW_DAYS = 365

# State rates in cents per hour; tax in basis points (575 = 5.75%), applied to parts + materials.
STATE_RATES: dict[str, dict[str, int]] = {
    "OH": {"body_rate": 5800, "paint_rate": 6000, "materials_rate": 3800, "tax_bp": 575},
    "IN": {"body_rate": 5400, "paint_rate": 5600, "materials_rate": 3600, "tax_bp": 700},
    "MI": {"body_rate": 6200, "paint_rate": 6400, "materials_rate": 4000, "tax_bp": 600},
}

CAUSE_TO_COVERAGE = {
    "collision": "COLL",
    "animal": "COMP",
    "weather": "COMP",
    "theft": "COMP",
    "vandalism": "COMP",
    "glass": "COMP",
}

SIU_FLAGS = ("PRIOR_DAMAGE", "DAMAGE_MISMATCH", "DATE_CONFLICT", "VIN_MISMATCH")

DECISIONS = ("PAY", "PAY_ADJUSTED", "DENY", "REFER_SIU", "REQUEST_INFO")
PAYING = ("PAY", "PAY_ADJUSTED")


# money helpers
def tenths(hours: float) -> int:
    """Hours are billed in tenths. 2.35 -> 24 (half up)."""
    return int(round(hours * 10 + 1e-9))


def hours_cost(hours: float, rate_c: int) -> int:
    return (tenths(hours) * rate_c + 5) // 10


def apply_bp(amount_c: int, bp: int) -> int:
    return (amount_c * bp + 5000) // 10000


def to_cents(dollars: float | int | None) -> int:
    if dollars is None:
        return 0
    return int(round(float(dollars) * 100 + 1e-9))


def fmt_usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# estimates
@dataclass
class Line:
    op: str  # "Repl" (replace: part + labor + paint) or "Refn" (refinish only)
    part_number: str
    description: str
    qty: int
    part_price_c: int  # unit price
    labor_hours: float
    paint_hours: float


@dataclass
class EstimateRates:
    body_rate_c: int
    paint_rate_c: int
    materials_rate_c: int
    misc_c: int
    tax_bp: int


@dataclass
class Totals:
    parts_c: int
    body_hours: float
    body_c: int
    paint_hours: float
    paint_c: int
    materials_c: int
    misc_c: int
    tax_c: int
    total_c: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def estimate_totals(lines: list[Line], r: EstimateRates) -> Totals:
    parts = sum(ln.part_price_c * ln.qty for ln in lines)
    body_t = sum(tenths(ln.labor_hours) for ln in lines)
    paint_t = sum(tenths(ln.paint_hours) for ln in lines)
    body_c = (body_t * r.body_rate_c + 5) // 10
    paint_c = (paint_t * r.paint_rate_c + 5) // 10
    materials_c = (paint_t * r.materials_rate_c + 5) // 10
    tax_c = apply_bp(parts + materials_c, r.tax_bp)
    total = parts + body_c + paint_c + materials_c + r.misc_c + tax_c
    return Totals(parts, body_t / 10, body_c, paint_t / 10, paint_c, materials_c, r.misc_c, tax_c, total)


@dataclass
class RefPart:
    part_number: str
    description: str
    price_c: int
    labor_hours: float
    paint_hours: float


@dataclass
class Adjustment:
    line: int  # 1-based line number, 0 for estimate-level rates
    field: str  # part_price | labor_hours | paint_hours | body_rate | paint_rate | materials_rate | part_number
    billed: float
    reference: float
    approved: float
    reason: str


@dataclass
class PriceCheck:
    billed: Totals
    approved: Totals
    adjustments: list[Adjustment] = field(default_factory=list)
    unknown_parts: list[str] = field(default_factory=list)

    @property
    def adjusted(self) -> bool:
        return bool(self.adjustments)


def price_check(lines: list[Line], rates: EstimateRates, refs: dict[str, RefPart], state: str) -> PriceCheck:
    """Apply the claims-manual price rules. Over-tolerance items are approved at the reference."""
    s = STATE_RATES[state]
    adj: list[Adjustment] = []
    unknown: list[str] = []
    approved_lines: list[Line] = []
    for i, ln in enumerate(lines, start=1):
        ref = refs.get(ln.part_number)
        if ref is None:
            unknown.append(ln.part_number)
            approved_lines.append(Line(ln.op, ln.part_number, ln.description, 0, 0, 0.0, 0.0))
            adj.append(Adjustment(i, "part_number", 0, 0, 0, "part number not in reference catalog; line not approved"))
            continue
        price, labor, paint = ln.part_price_c, ln.labor_hours, ln.paint_hours
        if ln.op == "Repl" and price > ref.price_c * (1 + PART_TOLERANCE):
            adj.append(Adjustment(i, "part_price", price / 100, ref.price_c / 100, ref.price_c / 100, "over parts tolerance"))
            price = ref.price_c
        if ln.op == "Repl" and tenths(labor) > tenths(ref.labor_hours * (1 + HOURS_TOLERANCE)):
            adj.append(Adjustment(i, "labor_hours", labor, ref.labor_hours, ref.labor_hours, "over labor-hours tolerance"))
            labor = ref.labor_hours
        if tenths(paint) > tenths(ref.paint_hours * (1 + HOURS_TOLERANCE)):
            adj.append(Adjustment(i, "paint_hours", paint, ref.paint_hours, ref.paint_hours, "over paint-hours tolerance"))
            paint = ref.paint_hours
        approved_lines.append(Line(ln.op, ln.part_number, ln.description, ln.qty, price, labor, paint))

    ar = EstimateRates(rates.body_rate_c, rates.paint_rate_c, rates.materials_rate_c, rates.misc_c, s["tax_bp"])
    for key in ("body_rate", "paint_rate", "materials_rate"):
        billed = getattr(rates, f"{key}_c")
        if billed > s[key] * (1 + RATE_TOLERANCE):
            adj.append(Adjustment(0, key, billed / 100, s[key] / 100, s[key] / 100, "over state rate tolerance"))
            setattr(ar, f"{key}_c", s[key])
    return PriceCheck(estimate_totals(lines, rates), estimate_totals(approved_lines, ar), adj, unknown)


# documents
def required_documents(cause: str, other_vehicle: bool) -> list[str]:
    docs = ["fnol", "estimate"]
    if cause in ("theft", "vandalism") or (cause == "collision" and other_vehicle):
        docs.append("police_report")
    return docs


def policy_active(loss: date, effective: date, expiry: date, cancel: date | None) -> bool:
    if loss < effective or loss > expiry:
        return False
    return cancel is None or loss < cancel


def dates_conflict(fnol_loss: date | None, police_incident: date | None) -> bool:
    if fnol_loss is None or police_incident is None:
        return False
    return abs((fnol_loss - police_incident).days) > DATE_CONFLICT_DAYS


# decision
@dataclass
class DecisionFacts:
    policy_active: bool
    coverage_code: str | None  # COLL / COMP / None when the cause maps to nothing
    coverage_present: bool
    deductible_c: int
    missing_documents: list[str]
    siu_flags: list[str]
    driver_listed: bool
    approved_total_c: int | None  # None when there is no estimate
    adjusted: bool
    acv_c: int


@dataclass
class Decision:
    decision: str
    reason_codes: list[str]
    payout_c: int
    coverage_code: str | None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def decide(f: DecisionFacts) -> Decision:
    """The claims-manual decision order. Ground truth and the reviewer's cross-check both use it."""
    cov = f.coverage_code
    if not f.policy_active:
        return Decision("DENY", ["POLICY_INACTIVE"], 0, cov)
    if not f.coverage_present:
        return Decision("DENY", ["NO_COVERAGE"], 0, cov)
    if f.siu_flags:
        return Decision("REFER_SIU", sorted(set(f.siu_flags)), 0, cov)
    if f.missing_documents or f.approved_total_c is None:
        return Decision("REQUEST_INFO", ["MISSING_DOC"], 0, cov)
    if not f.driver_listed:
        return Decision("REQUEST_INFO", ["DRIVER_VERIFICATION"], 0, cov)
    if f.approved_total_c > TOTAL_LOSS_RATIO * f.acv_c:
        return Decision("PAY", ["TOTAL_LOSS"], max(f.acv_c - f.deductible_c, 0), cov)
    if f.approved_total_c <= f.deductible_c:
        return Decision("DENY", ["BELOW_DEDUCTIBLE"], 0, cov)
    payout = f.approved_total_c - f.deductible_c
    if f.adjusted:
        return Decision("PAY_ADJUSTED", ["PRICE_ADJUSTED"], payout, cov)
    return Decision("PAY", ["COVERED_LOSS"], payout, cov)


def payout_for(decision: str, approved_total_c: int | None, acv_c: int, deductible_c: int, total_loss: bool) -> int:
    """What the system pays for a given decision. The LLM picks the decision; this computes cents."""
    if decision not in PAYING or approved_total_c is None:
        return 0
    if total_loss:
        return max(acv_c - deductible_c, 0)
    return max(approved_total_c - deductible_c, 0)
