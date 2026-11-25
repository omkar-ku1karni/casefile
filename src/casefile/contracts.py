"""Typed handoffs. Nothing passes between nodes except these models (serialised as JSON in graph state).

LLM-facing models use dollars/strings the way documents print them; code converts to integer cents
at the boundary (``rules.to_cents``) and does every money computation itself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocId = Literal["fnol", "police_report", "estimate", "estimate_supplement"]
Area = Literal["front", "rear", "left", "right", "roof", "glass", "undercarriage"]
Cause = Literal["collision", "animal", "weather", "theft", "vandalism", "glass"]
CoverageCode = Literal["COLL", "COMP"]
DecisionCode = Literal["PAY", "PAY_ADJUSTED", "DENY", "REFER_SIU", "REQUEST_INFO"]
ReasonCode = Literal[
    "COVERED_LOSS", "PRICE_ADJUSTED", "TOTAL_LOSS", "BELOW_DEDUCTIBLE", "POLICY_INACTIVE", "NO_COVERAGE",
    "PRIOR_DAMAGE", "DAMAGE_MISMATCH", "DATE_CONFLICT", "VIN_MISMATCH", "MISSING_DOC", "DRIVER_VERIFICATION",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _iso(v: str | None) -> str | None:
    if v is None:
        return v
    date.fromisoformat(v)  # raises -> validation error -> repair round trip
    return v


class Cite(Strict):
    doc_id: DocId
    quote: str = Field(description="Short verbatim quote from the document (max 160 chars).", max_length=200)


# extractor
class EstimateLine(Strict):
    op: Literal["Repl", "Refn"] = Field(description="Repl for replace (R&R/Replace), Refn for refinish only.")
    part_number: str
    description: str
    qty: int
    part_price: float = Field(description="Unit part price in dollars; 0 for refinish-only lines.")
    labor_hours: float = Field(description="Body/sheet-metal labor hours for the line; 0 if none.")
    paint_hours: float = Field(description="Paint/refinish hours for the line; 0 if none.")


class ExtractedEstimate(Strict):
    doc_id: Literal["estimate", "estimate_supplement"]
    estimate_number: str
    shop_name: str
    vin: str
    is_supplement: bool
    lines: list[EstimateLine]
    body_rate: float = Field(description="Body labor rate, dollars per hour.")
    paint_rate: float = Field(description="Paint labor rate, dollars per hour.")
    materials_rate: float = Field(description="Paint materials rate, dollars per paint hour.")
    misc: float = Field(description="Misc / hazmat / shop supplies / sublet amount in dollars.")
    tax_rate_pct: float = Field(description="Tax rate in percent, e.g. 5.75.")
    printed_total: float = Field(description="Grand total printed on the document, dollars.")


class ExtractedPoliceReport(Strict):
    report_number: str
    incident_date: str = Field(description="YYYY-MM-DD")
    driver_name: str = Field(description="Unit 1 driver as 'First Last'.")
    vin: str | None
    impact_described: Area = Field(description="Where the officer says the insured vehicle was damaged.")

    @field_validator("incident_date")
    @classmethod
    def _date_ok(cls, v: str) -> str:
        return _iso(v)


class ExtractedClaim(Strict):
    policy_number: str
    vin: str = Field(description="VIN exactly as written on the FNOL.")
    driver_name: str = Field(description="Driver at time of loss as 'First Last'.")
    loss_date: str = Field(description="Date of loss from the FNOL, YYYY-MM-DD.")
    loss_time: str | None = Field(description="24h HH:MM, if stated.")
    loss_state: Literal["OH", "IN", "MI"]
    claimant_loss_type: str = Field(description="The 'type of loss' label the claimant chose on the FNOL.")
    loss_cause: Cause = Field(description="Cause of loss classified from the facts, per the claims manual.")
    other_vehicle_involved: bool
    impact_area_described: Area = Field(description="Where the claimant says the vehicle was hit.")
    narrative_summary: str = Field(max_length=400)
    police_report: ExtractedPoliceReport | None
    estimates: list[ExtractedEstimate] = Field(description="One entry per estimate document, including supplements.")
    citations: list[Cite] = Field(description="Quotes supporting loss date, cause and driver (2-4).")

    @field_validator("loss_date")
    @classmethod
    def _date_ok(cls, v: str) -> str:
        return _iso(v)


# investigator
class InvestigatorJudgment(Strict):
    coverage_code: CoverageCode = Field(description="Coverage the cause of loss requires (claims manual section 1).")
    coverage_reasoning: str = Field(max_length=400)
    driver_listed: bool = Field(description="Is the driver at time of loss a listed driver on the policy?")
    vin_assessment: Literal["MATCH", "CLERICAL_TYPO", "MISMATCH"]
    damage_consistent: bool = Field(description="Does the repaired damage match how the loss is described?")
    damage_reasoning: str = Field(max_length=400)
    notes: str = Field(max_length=400)


class PriorClaimHit(Strict):
    prior_claim_number: str
    loss_date: str
    damage_areas: list[str]
    paid_c: int
    repair_verified: bool
    days_before_loss: int
    overlaps: bool  # computed by code: same VIN, <=365d, unrepaired, overlapping area


class Findings(Strict):
    """Facts (computed by code from tool results) + judgment (from the investigator LLM)."""

    policy_found: bool
    policy_vin: str | None
    vehicle_class: str | None
    state: str | None
    policy_active: bool
    coverages: list[str]
    coverage_code: CoverageCode | None
    coverage_present: bool
    deductible_c: int
    drivers_on_policy: list[str]
    missing_documents: list[str]
    date_conflict: bool
    prior_claims: list[PriorClaimHit]
    damage_areas: list[str]
    billed_total_c: int | None
    approved_total_c: int | None
    adjustments: list[dict]
    unknown_parts: list[str]
    acv_c: int
    total_loss: bool
    siu_flags: list[str]
    judgment: InvestigatorJudgment


# recommendation
class RecommendationDraft(Strict):
    decision: DecisionCode
    reason_codes: list[ReasonCode]
    rationale: str = Field(max_length=900, description="For the adjuster: 2-5 sentences, cite facts.")
    citations: list[Cite] = Field(description="1-4 verbatim quotes supporting the decision.")


class Recommendation(Strict):
    decision: DecisionCode
    reason_codes: list[str]
    coverage_code: str | None
    payout_c: int  # computed by rules.payout_for, never by the LLM
    rationale: str
    citations: list[Cite]

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:16]


# review
IssueCode = Literal[
    "MISSING_FIELD", "FIELD_CONFLICT", "COVERAGE_MISAPPLIED", "PAYOUT_MISMATCH", "UNSUPPORTED_CITATION",
    "MISSED_RED_FLAG", "DECISION_RULE_VIOLATION",
]
Target = Literal["extractor", "investigator", "recommend"]


class ReviewIssue(Strict):
    code: IssueCode
    target: Target
    field: str | None
    detail: str = Field(max_length=300)
    source: Literal["rule", "llm"] = "llm"

    def key(self) -> str:
        return f"{self.code}:{self.target}:{self.field}"


class LLMReview(Strict):
    verdict: Literal["ACCEPT", "REVISE"]
    issues: list[ReviewIssue]


class ReviewVerdict(Strict):
    verdict: Literal["ACCEPT", "REVISE"]
    issues: list[ReviewIssue]
    recommendation_hash: str


class ReworkOrder(Strict):
    target: Target
    issues: list[ReviewIssue]


# terminal / approval
TerminalKind = Literal[
    "AWAITING_APPROVAL", "DECIDED", "REJECTED_BY_HUMAN", "RETURNED_BY_HUMAN", "ESCALATED_BUDGET",
    "ESCALATED_STEP_LIMIT", "ESCALATED_NO_PROGRESS", "ESCALATED_REVISION_LIMIT", "ESCALATED_ERROR",
]


class Terminal(Strict):
    kind: TerminalKind
    detail: str


class Approval(Strict):
    approval_id: str
    action: Literal["APPROVE", "REJECT", "RETURN"]
    approver: str
    recommendation_hash: str
    note: str = ""


# baselines
class SinglePassDecision(Strict):
    """What a single prompt / single agent must produce in one go."""

    extraction: ExtractedClaim
    coverage_code: CoverageCode
    decision: DecisionCode
    reason_codes: list[ReasonCode]
    rationale: str = Field(max_length=900)


class ToolCall(Strict):
    tool: Literal["lookup_policy", "vehicle_valuation", "get_prior_claims", "reference_prices"]
    policy_number: str | None
    vin: str | None
    part_numbers: list[str] = Field(description="Only for reference_prices.")


class AgentStep(Strict):
    thought: str = Field(max_length=500)
    tool_calls: list[ToolCall] = Field(description="Tools to call now. Empty when giving the final answer.")
    final: SinglePassDecision | None = Field(description="Set only when done.")


ALL_SCHEMAS = [ExtractedClaim, InvestigatorJudgment, RecommendationDraft, LLMReview, SinglePassDecision, AgentStep,
               Findings, Recommendation, ReviewVerdict, Approval, Terminal]
