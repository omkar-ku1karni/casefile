"""The four specialists. Each takes the graph state + RunContext and returns a partial state update.

Division of labour, deliberately:
* LLM: reading messy documents, classifying the cause of loss, judging whether a driver is
  listed / a VIN is a typo / the damage matches the story, writing the rationale.
* Code: every lookup (tools), every date and money computation, the claims-manual rules
  cross-check, citation verification.
"""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache

from casefile import llm, rules, tools
from casefile.contracts import (
    ExtractedClaim,
    Findings,
    InvestigatorJudgment,
    LLMReview,
    PriorClaimHit,
    Recommendation,
    RecommendationDraft,
    ReviewIssue,
    ReviewVerdict,
    ReworkOrder,
)
from casefile.paths import SPEC_DIR
from casefile.runtime import RunContext

TARGET_ORDER = ("extractor", "investigator", "recommend")


# shared prompt material
@lru_cache
def manual_sections() -> dict[str, str]:
    text = (SPEC_DIR / "claims_manual.md").read_text()
    parts = re.split(r"(?m)^## ", text)
    return {p.split(".", 1)[0].strip(): "## " + p.strip() for p in parts[1:]}


def manual(*nums: int) -> str:
    s = manual_sections()
    return "\n\n".join(s[str(n)] for n in nums)


def render_docs(docs: dict[str, dict], only: tuple[str, ...] | None = None) -> str:
    out = []
    for doc_id, d in docs.items():
        if only and doc_id not in only:
            continue
        for i, page in enumerate(d["pages"], start=1):
            out.append(f"=== DOCUMENT {doc_id} ({d['type']}), page {i} ===\n{page}")
    return "\n\n".join(out)


def _rework_block(state: dict, target: str, previous: dict | None) -> str:
    rw = state.get("rework")
    if not rw or rw["target"] != target:
        return ""
    issues = "\n".join(f"- [{i['code']}] {i.get('field') or ''}: {i['detail']}" for i in rw["issues"])
    prev = json.dumps(previous, indent=1)[:6000] if previous else "(none)"
    return (f"\n\n---\nREVIEWER SENT YOUR PREVIOUS OUTPUT BACK. Fix these issues, re-checking the documents; keep "
            f"everything that was correct.\nIssues:\n{issues}\n\nYour previous output:\n{prev}")


def _j(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


# extractor
EXTRACTOR_SYSTEM = """You are the document-extraction specialist in Halvorne Mutual's auto-claims triage system.
Read the claim documents and return ONE JSON object that matches the schema. You do not decide coverage or payment.

Rules:
- Copy identifiers exactly as printed: policy number, report number, part numbers. `vin` is the VIN as written
  on the FNOL, even if it differs from other documents. Each estimate's `vin` is the VIN printed on that estimate.
- Dates -> YYYY-MM-DD. Times -> 24-hour HH:MM.
- Names -> "First Last": drop middle initials; turn "LAST, FIRST M" into "First Last".
- `loss_cause`: classify from the FACTS of the loss using the coverage table below, not from the claimant's
  "type of loss" label (which goes in `claimant_loss_type`).
- `impact_area_described`: where the claimant says the vehicle was hit.
- `estimates`: one entry per estimate document - `estimate` and, if present, `estimate_supplement`. Use op "Repl"
  for replace lines (Repl / R&R / Replace) and "Refn" for refinish-only lines (Refn / Refinish). `part_number` is
  exactly the code in the part-number column (pattern like "MS-FBC-204"); never include words from the
  description column. `part_price` is
  the unit price (0 when the column shows "-"); hours are the per-line body/labor and paint/refinish hours
  (0 for "-"). Rates are the dollars-per-hour figures in the totals block; `misc` is the misc / shop supplies /
  sublet amount; `tax_rate_pct` is the percentage shown; `printed_total` is the grand total printed.
- `police_report`: null when there is no police-report document.
- `citations`: 2-4 SHORT quotes copied character-for-character from the documents (no paraphrase) that support
  the loss date, the cause and the driver.

""" + "{manual}"


def extractor(state: dict, ctx: RunContext) -> dict:
    docs = tools.claim_documents(state["claim_id"])
    user = (f"Claim {state['claim_id']}. Documents:\n\n{render_docs(docs)}"
            + _rework_block(state, "extractor", state.get("extracted")))
    out = llm.complete("extractor", EXTRACTOR_SYSTEM.replace("{manual}", manual(1)), user, ExtractedClaim)
    return {"extracted": out.model_dump(mode="json"), "rework": None}


# investigator
INVESTIGATOR_SYSTEM = """You are the investigator in Halvorne Mutual's auto-claims triage system. Code has already
looked up the policy, the vehicle valuation, prior claims and reference prices; those results are FACTS. Your job
is the judgment calls that need reading, and nothing else:

1. `coverage_code`: which coverage the cause of loss requires (COLL or COMP) per section 1. Decide from what
   actually happened, not from the claimant's label.
2. `driver_listed`: is the driver at the time of loss one of the policy's listed drivers? Ignore middle
   initials and letter case; a different person is not listed.
3. `vin_assessment`: compare the FNOL VIN, the estimate/police VINs and the policy vehicle VIN. MATCH if all
   agree; CLERICAL_TYPO if only the FNOL differs by a character or two while the other documents match the
   policy vehicle; MISMATCH if the vehicle being claimed is not the policy vehicle.
4. `damage_consistent`: do the parts being repaired on the estimate(s) fit how the claimant and the police
   describe the impact? Front-end parts for a described rear impact are NOT consistent. Damage that spreads to
   adjacent panels on the impacted side (a side scrape reaching the fender or quarter panel; a front impact
   reaching the hood, fenders or headlamps) and hidden damage behind the impact (radiator support, condenser)
   ARE consistent. Judge by the parts' names and sides (LH = driver side, RH = passenger side).
Keep reasoning fields to one or two sentences. Use `notes` for anything an adjuster should know.

""" + "{manual}"


def investigator(state: dict, ctx: RunContext) -> dict:
    ex = ExtractedClaim.model_validate(state["extracted"])
    doc_ids = state["documents"]
    pol = ctx.tool("lookup_policy", tools.lookup_policy, policy_number=ex.policy_number)
    val = ctx.tool("vehicle_valuation", tools.vehicle_valuation, vin=ex.vin)
    vin_fallback = False
    if not val["found"] and pol["found"] and pol.get("vehicle"):
        val = ctx.tool("vehicle_valuation", tools.vehicle_valuation, vin=pol["vehicle"]["vin"])
        vin_fallback = True
    lookup_vin = val["vin"] if val["found"] else ex.vin
    priors = ctx.tool("get_prior_claims", tools.get_prior_claims, vin=lookup_vin)
    state_code = pol["state"] if pol["found"] else ex.loss_state
    pc = None
    if ex.estimates:
        pc = ctx.tool("price_check", tools.price_check, estimates=[e.model_dump() for e in ex.estimates],
                      state=state_code)

    docs = tools.claim_documents(state["claim_id"])
    est_lines = [f"{e.doc_id}: " + "; ".join(f"{ln.op} {ln.description} ({ln.part_number})" for ln in e.lines)
                 for e in ex.estimates]
    facts_for_llm = {
        "extraction": {k: v for k, v in ex.model_dump(mode="json").items() if k not in ("estimates", "citations")},
        "estimate_vins": {e.doc_id: e.vin for e in ex.estimates},
        "estimate_lines": est_lines,
        "policy": pol,
        "valuation_lookup": {"by_fnol_vin_found": not vin_fallback and val["found"],
                             "fell_back_to_policy_vin": vin_fallback, "result": val},
    }
    user = (f"Facts:\n{_j(facts_for_llm)}\n\nFNOL and police report text:\n\n"
            f"{render_docs(docs, only=('fnol', 'police_report'))}"
            + _rework_block(state, "investigator", (state.get("findings") or {}).get("judgment")))
    j = llm.complete("investigator", INVESTIGATOR_SYSTEM.replace("{manual}", manual(1, 4, 5)), user,
                     InvestigatorJudgment)

    findings = compute_findings(ex, j, pol, val, priors, pc, doc_ids)
    return {"findings": findings.model_dump(mode="json"), "rework": None}


def compute_findings(ex: ExtractedClaim, j: InvestigatorJudgment, pol: dict, val: dict, priors: list[dict],
                     pc: dict | None, doc_ids: list[str]) -> Findings:
    """Pure: tool results + judgment -> Findings. All dates and money here are code."""
    from datetime import date

    loss = date.fromisoformat(ex.loss_date)
    active = False
    covs: dict[str, int] = {}
    if pol["found"]:
        active = rules.policy_active(loss, date.fromisoformat(pol["effective_date"]),
                                     date.fromisoformat(pol["expiry_date"]),
                                     date.fromisoformat(pol["cancel_date"]) if pol["cancel_date"] else None)
        covs = {c["code"]: rules.to_cents(c["deductible"]) for c in pol["coverages"]}
    present = {"fnol"} | ({"estimate"} if any(d.startswith("estimate") for d in doc_ids) else set()) \
        | ({"police_report"} if "police_report" in doc_ids else set())
    missing = [d for d in rules.required_documents(ex.loss_cause, ex.other_vehicle_involved) if d not in present]
    police_date = date.fromisoformat(ex.police_report.incident_date) if ex.police_report else None
    date_conflict = rules.dates_conflict(loss, police_date)
    damage_areas = pc["damage_areas"] if pc else []
    hits = []
    for p in priors:
        days = (loss - date.fromisoformat(p["loss_date"])).days
        overlaps = (0 <= days <= rules.PRIOR_DAMAGE_WINDOW_DAYS and not p["repair_verified"]
                    and bool(set(p["damage_areas"]) & set(damage_areas)))
        hits.append(PriorClaimHit(prior_claim_number=p["prior_claim_number"], loss_date=p["loss_date"],
                                  damage_areas=p["damage_areas"], paid_c=rules.to_cents(p["paid"]),
                                  repair_verified=p["repair_verified"], days_before_loss=days, overlaps=overlaps))
    acv = rules.to_cents(val["acv"]) if val.get("found") else 0
    approved = pc["approved_total_c"] if pc else None
    siu = []
    if any(h.overlaps for h in hits):
        siu.append("PRIOR_DAMAGE")
    if not j.damage_consistent and pc:
        siu.append("DAMAGE_MISMATCH")
    if date_conflict:
        siu.append("DATE_CONFLICT")
    if j.vin_assessment == "MISMATCH":
        siu.append("VIN_MISMATCH")
    return Findings(
        policy_found=pol["found"],
        policy_vin=(pol.get("vehicle") or {}).get("vin") if pol["found"] else None,
        vehicle_class=val.get("vehicle_class"),
        state=pol.get("state"),
        policy_active=active,
        coverages=sorted(covs),
        coverage_code=j.coverage_code,
        coverage_present=j.coverage_code in covs,
        deductible_c=covs.get(j.coverage_code, 0),
        drivers_on_policy=pol.get("drivers", []),
        missing_documents=missing,
        date_conflict=date_conflict,
        prior_claims=hits,
        damage_areas=damage_areas,
        billed_total_c=pc["billed_total_c"] if pc else None,
        approved_total_c=approved,
        adjustments=pc["adjustments"] if pc else [],
        unknown_parts=pc["unknown_parts"] if pc else [],
        acv_c=acv,
        total_loss=approved is not None and acv > 0 and approved > rules.TOTAL_LOSS_RATIO * acv,
        siu_flags=siu,
        judgment=j,
    )


def decision_facts(f: Findings) -> rules.DecisionFacts:
    return rules.DecisionFacts(
        policy_active=f.policy_active, coverage_code=f.coverage_code, coverage_present=f.coverage_present,
        deductible_c=f.deductible_c, missing_documents=f.missing_documents, siu_flags=f.siu_flags,
        driver_listed=f.judgment.driver_listed, approved_total_c=f.approved_total_c,
        adjusted=bool(f.adjustments), acv_c=f.acv_c,
    )


def findings_brief(f: Findings) -> dict:
    """What the supervisor and reviewer see: facts with money as dollars."""
    d = f.model_dump(mode="json")
    for k in ("billed_total_c", "approved_total_c", "acv_c", "deductible_c"):
        if d.get(k) is not None:
            d[k.replace("_c", "")] = round(d.pop(k) / 100, 2)
        else:
            d.pop(k, None)
    d["total_loss_threshold"] = round(f.acv_c * rules.TOTAL_LOSS_RATIO / 100, 2)
    d["price_adjusted"] = bool(f.adjustments)
    return d


# supervisor: recommendation draft
RECOMMEND_SYSTEM = """You are the claims supervisor in Halvorne Mutual's auto-claims triage system. Specialists
have extracted the documents and investigated the claim. Draft the recommendation an adjuster will approve.

- Apply the decision order in section 7 to the FINDINGS exactly (first match wins). Findings marked as facts were
  computed by the claims system from tool results; do not second-guess dates, totals or the price check.
- `reason_codes`: the code(s) from section 7 for the matched step (for REFER_SIU: every SIU trigger present).
- `rationale`: 2-5 plain sentences for the adjuster, naming the facts that drove the decision.
- `citations`: 1-4 quotes copied character-for-character from the documents (doc_id + exact text).
- Do not state a payout amount: the claims system computes it.

""" + "{manual}"


def recommend(state: dict, ctx: RunContext) -> dict:
    ex = ExtractedClaim.model_validate(state["extracted"])
    f = Findings.model_validate(state["findings"])
    docs = tools.claim_documents(state["claim_id"])
    summary = {k: v for k, v in ex.model_dump(mode="json").items() if k not in ("estimates", "citations")}
    user = (f"FINDINGS:\n{_j(findings_brief(f))}\n\nEXTRACTION SUMMARY:\n{_j(summary)}\n\nDOCUMENTS:\n\n"
            f"{render_docs(docs)}" + _rework_block(state, "recommend", state.get("recommendation")))
    draft = llm.complete("recommend", RECOMMEND_SYSTEM.replace("{manual}", manual(4, 6, 7)), user,
                         RecommendationDraft)
    payout = rules.payout_for(draft.decision, f.approved_total_c, f.acv_c, f.deductible_c, f.total_loss)
    rec = Recommendation(decision=draft.decision, reason_codes=list(draft.reason_codes),
                         coverage_code=f.coverage_code, payout_c=payout, rationale=draft.rationale,
                         citations=draft.citations)
    return {"recommendation": rec.model_dump(mode="json") | {"hash": rec.digest()}, "rework": None}


# reviewer
REVIEWER_SYSTEM = """You are the independent reviewer (maker-checker) in Halvorne Mutual's auto-claims triage
system. Arithmetic, dates, document completeness, citations and the decision order have ALREADY been verified by
code. Check only the judgment calls, against the documents:

1. Cause of loss / coverage (section 1): was COLL vs COMP chosen from what actually happened?
2. Driver: is the driver at the time of loss really a listed driver (or really not)?
3. Damage consistency: do the repaired parts fit the described impact?
4. VIN: typo vs a different vehicle.
5. Any SIU trigger in section 4 supported by the documents that the findings missed.

ACCEPT with an empty issue list when the findings are supported. Use REVISE only for a concrete problem you can
point to in the documents, and set `target`: "extractor" if a field was misread from a document,
"investigator" if a judgment is wrong, "recommend" if the rationale misstates the facts. Each unnecessary
revision costs money and time; do not revise for style.

""" + "{manual}"


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip().lower()


def quote_found(quote: str, doc_text: str) -> bool:
    q, t = _norm_text(quote).strip(" .\"'"), _norm_text(doc_text)
    if not q:
        return False
    if q in t:
        return True
    pieces = [p.strip() for p in re.split(r"\.\.\.|…", q) if p.strip()]
    if len(pieces) > 1 and all(p in t for p in pieces):
        return True
    m = difflib.SequenceMatcher(None, t, q, autojunk=False).find_longest_match(0, len(t), 0, len(q))
    return m.size >= 0.85 * len(q)


def rule_checks(state: dict) -> list[ReviewIssue]:
    """Deterministic maker-checker. Cheap, exact, and run before any LLM review."""
    ex = ExtractedClaim.model_validate(state["extracted"])
    f = Findings.model_validate(state["findings"])
    rec = state["recommendation"]
    docs = tools.claim_documents(state["claim_id"])
    issues: list[ReviewIssue] = []
    got = {e.doc_id for e in ex.estimates}
    for d in docs:
        if d.startswith("estimate") and d not in got:
            issues.append(ReviewIssue(code="MISSING_FIELD", target="extractor", field=f"estimates[{d}]",
                                      detail=f"Document '{d}' is in the file but was not extracted as an estimate.",
                                      source="rule"))
    if f.unknown_parts:
        issues.append(ReviewIssue(
            code="FIELD_CONFLICT", target="extractor", field="estimates.lines.part_number",
            detail=(f"Part numbers not in the reference catalog: {', '.join(sorted(set(f.unknown_parts)))}. Re-read the "
                    "part-number column exactly; it is a separate column from the description."),
            source="rule"))
    if "police_report" in docs and ex.police_report is None:
        issues.append(ReviewIssue(code="MISSING_FIELD", target="extractor", field="police_report",
                                  detail="A police report is in the file but police_report is null.", source="rule"))
    for e in ex.estimates:
        lines, r = tools.to_rules(e)
        computed = rules.estimate_totals(lines, r).total_c
        printed = rules.to_cents(e.printed_total)
        if abs(computed - printed) > 100:
            issues.append(ReviewIssue(
                code="FIELD_CONFLICT", target="extractor", field=f"estimates[{e.doc_id}]",
                detail=(f"Extracted lines and rates add up to {rules.fmt_usd(computed)} but the printed total is "
                        f"{rules.fmt_usd(printed)}: a price, hours, rate, misc or tax value was misread."),
                source="rule"))
    all_text = {d: "\n".join(v["pages"]) for d, v in docs.items()}
    for c in rec["citations"]:
        if not quote_found(c["quote"], all_text.get(c["doc_id"], "")):
            issues.append(ReviewIssue(code="UNSUPPORTED_CITATION", target="recommend", field="citations",
                                      detail=f"Quote not found verbatim in {c['doc_id']}: \"{c['quote'][:120]}\"",
                                      source="rule"))
    expected = rules.decide(decision_facts(f))
    if rec["decision"] != expected.decision:
        issues.append(ReviewIssue(
            code="DECISION_RULE_VIOLATION", target="recommend", field="decision",
            detail=(f"Section 7 applied to the findings gives {expected.decision} "
                    f"({', '.join(expected.reason_codes)}), not {rec['decision']}."),
            source="rule"))
    return issues


def reviewer(state: dict, ctx: RunContext) -> dict:
    rec = state["recommendation"]
    with ctx.tracer.span("review.rule_checks", kind="guardrail") as sp:
        issues = rule_checks(state)
        sp.update(output=[i.model_dump() for i in issues])
    if not issues:
        docs = tools.claim_documents(state["claim_id"])
        f = Findings.model_validate(state["findings"])
        ex = ExtractedClaim.model_validate(state["extracted"])
        summary = {k: v for k, v in ex.model_dump(mode="json").items() if k != "citations"}
        user = (f"FINDINGS:\n{_j(findings_brief(f))}\n\nEXTRACTION:\n{_j(summary)}\n\n"
                f"RECOMMENDATION:\n{_j({k: rec[k] for k in ('decision', 'reason_codes', 'rationale')})}\n\n"
                f"DOCUMENTS:\n\n{render_docs(docs)}")
        out = llm.complete("reviewer", REVIEWER_SYSTEM.replace("{manual}", manual(1, 4, 5)), user, LLMReview)
        issues = [i.model_copy(update={"source": "llm"}) for i in out.issues] if out.verdict == "REVISE" else []
    verdict = ReviewVerdict(verdict="REVISE" if issues else "ACCEPT", issues=issues, recommendation_hash=rec["hash"])
    return {"reviews": state["reviews"] + [verdict.model_dump(mode="json")]}


def rework_for(issues: list[dict]) -> ReworkOrder:
    target = min((i["target"] for i in issues), key=TARGET_ORDER.index)
    return ReworkOrder(target=target, issues=[ReviewIssue.model_validate(i) for i in issues if i["target"] == target])
