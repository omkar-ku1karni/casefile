"""An oracle LLM for tests and the offline mechanics demo.

It answers every agent's schema *correctly* from the world's ground truth, with optional injected
faults (``CASEFILE_FAKE_FAULTS``) that exercise the control paths: a dropped supplement, a misread
total, a wrong first decision, a reviewer that never accepts. It exists to test orchestration --
routing, rework, termination, budget, checkpoints, replay, the human gate - without any API key.
Numbers produced with it are NOT model results and are never reported as such.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from casefile import rules
from casefile.paths import EVAL_DIR, WORLD_DIR
from casefile.runtime import current

FAULTS_ENV = "CASEFILE_FAKE_FAULTS"


@lru_cache
def _world() -> dict:
    w = json.loads((WORLD_DIR / "world.json").read_text())
    return {"claims": {c["claim_id"]: c for c in w["claims"]}, "shops": {s["shop_id"]: s for s in w["shops"]}}


@lru_cache
def _truth() -> dict:
    return {r["claim_id"]: r for r in map(json.loads, (EVAL_DIR / "claims.jsonl").read_text().splitlines())}


def faults() -> set[str]:
    return {f.strip() for f in os.environ.get(FAULTS_ENV, "").split(",") if f.strip()}


def _short(full: str) -> str:
    p = full.split()
    return f"{p[0]} {p[-1]}"


def extraction(claim_id: str, visit: int = 0) -> dict:
    from casefile.datagen import narratives
    from casefile.tools import claim_documents

    c = _world()["claims"][claim_id]
    shops = _world()["shops"]
    docs = claim_documents(claim_id)
    fnol = docs["fnol"]["pages"][0]
    ests = []
    for e in c["estimates"]:
        tot = rules.estimate_totals(*_lines(e))
        ests.append({
            "doc_id": "estimate_supplement" if e["supplement"] else "estimate",
            "estimate_number": e["estimate_number"], "shop_name": shops[e["shop_id"]]["name"], "vin": c["vin"],
            "is_supplement": e["supplement"],
            "lines": [{"op": ln["op"], "part_number": ln["part_number"], "description": ln["description"],
                       "qty": ln["qty"], "part_price": ln["part_price_c"] / 100, "labor_hours": ln["labor_hours"],
                       "paint_hours": ln["paint_hours"]} for ln in e["lines"]],
            "body_rate": e["body_rate_c"] / 100, "paint_rate": e["paint_rate_c"] / 100,
            "materials_rate": e["materials_rate_c"] / 100, "misc": e["misc_c"] / 100,
            "tax_rate_pct": e["tax_bp"] / 100, "printed_total": tot.total_c / 100,
        })
    f = faults()
    if "drop_supplement" in f and visit == 0 and len(ests) > 1:
        ests = ests[:1]
    if "misread_total" in f and visit == 0 and ests and ests[0]["lines"]:
        ests[0]["lines"][0]["part_price"] = round(ests[0]["lines"][0]["part_price"] * 1.5, 2)
    pr = c["police"]
    line = next(ln for ln in fnol.splitlines() if "Date of loss" in ln)
    cite = line[line.index("Date of loss"):]
    return {
        "policy_number": c["policy_number"], "vin": c["fnol_vin"], "driver_name": _short(c["driver_name"]),
        "loss_date": c["loss_date"], "loss_time": c["loss_time"][:5], "loss_state": c["state"],
        "claimant_loss_type": narratives.CLAIMANT_LOSS_TYPE[c["subtype"]], "loss_cause": c["cause"],
        "other_vehicle_involved": c["other_vehicle"], "impact_area_described": c["impact_area"],
        "narrative_summary": f"{c['subtype'].replace('_', ' ')} loss in {c['city']}",
        "police_report": None if not pr else {
            "report_number": pr["report_number"], "incident_date": pr["incident_date"],
            "driver_name": _short(c["driver_name"]), "vin": c["vin"], "impact_described": c["impact_area"]},
        "estimates": ests,
        "citations": [{"doc_id": "fnol", "quote": cite}],
    }


def _lines(e: dict):
    lines = [rules.Line(ln["op"], ln["part_number"], ln["description"], ln["qty"], ln["part_price_c"],
                        ln["labor_hours"], ln["paint_hours"]) for ln in e["lines"]]
    return lines, rules.EstimateRates(e["body_rate_c"], e["paint_rate_c"], e["materials_rate_c"], e["misc_c"],
                                      e["tax_bp"])


def respond(agent: str, system: str, user: str, schema_name: str) -> str:
    ctx = current()
    claim_id, visit = ctx.claim_id, ctx.visit
    c = _world()["claims"][claim_id]
    t = _truth()[claim_id]["truth"]
    f = faults()
    if schema_name == "ExtractedClaim":
        return json.dumps(extraction(claim_id, visit))
    if schema_name == "InvestigatorJudgment":
        return json.dumps({
            "coverage_code": rules.CAUSE_TO_COVERAGE[c["cause"]], "coverage_reasoning": "per section 1",
            "driver_listed": c["driver_listed"],
            "vin_assessment": "CLERICAL_TYPO" if c["fnol_vin"] != c["vin"] else "MATCH",
            "damage_consistent": c["scenario"] != "S11", "damage_reasoning": "compared parts and narrative",
            "notes": ""})
    if schema_name == "RecommendationDraft":
        decision = t["decision"]
        if "wrong_decision" in f and visit == 0:
            decision = "PAY" if decision != "PAY" else "DENY"
        return json.dumps({"decision": decision, "reason_codes": t["reason_codes"],
                           "rationale": f"Section 7 applied to the findings gives {t['decision']}.",
                           "citations": extraction(claim_id)["citations"]})
    if schema_name == "LLMReview":
        if "always_revise" in f:
            n = ctx.ledger.llm_calls
            return json.dumps({"verdict": "REVISE", "issues": [{
                "code": "MISSED_RED_FLAG", "target": "investigator", "field": f"flag_{n}",
                "detail": "adversarial reviewer: never satisfied", "source": "llm"}]})
        return json.dumps({"verdict": "ACCEPT", "issues": []})
    if schema_name == "SinglePassDecision":
        return json.dumps({"extraction": extraction(claim_id), "coverage_code": rules.CAUSE_TO_COVERAGE[c["cause"]],
                           "decision": t["decision"], "reason_codes": t["reason_codes"], "rationale": "oracle"})
    if schema_name == "AgentStep":
        final = json.loads(respond(agent, system, user, "SinglePassDecision"))
        return json.dumps({"thought": "oracle", "tool_calls": [], "final": final})
    raise ValueError(schema_name)


def enable() -> None:
    from casefile import llm

    llm.set_fake(respond)
    os.environ["CASEFILE_MODELS"] = "fake:oracle"
