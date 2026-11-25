"""Baselines that answer "do we even need the graph?"

* ``single_prompt``: one call: every document, the prefetched policy / valuation / prior claims /
  reference prices for the vehicle class, and the full claims manual. One structured answer.
* ``single_agent``: one LLM in a tool loop with the same lookups exposed as tools, under the same
  budget and a step cap.

Both produce the same output the graph does, and the payout is computed by the same code from
their own extraction. The only thing that differs between the three systems is the orchestration.
"""

from __future__ import annotations

import json
import re

from casefile import llm, rules, tools
from casefile.agents import manual_sections, render_docs
from casefile.config import settings
from casefile.contracts import AgentStep, Recommendation, SinglePassDecision
from casefile.runtime import BudgetExceeded, LLMUnavailable, RunContext, SchemaViolation

BASE_SYSTEM = """You are a senior auto-claims adjuster at Halvorne Mutual. Read the claim documents and the
claims-system records, then return ONE JSON object: a full extraction of the documents and a recommendation that
applies the claims manual exactly. Rules for the extraction:
- Copy identifiers exactly; `vin` is the VIN as written on the FNOL. Dates YYYY-MM-DD; names "First Last".
- One `estimates` entry per estimate document (estimate, estimate_supplement); op "Repl" for replace/R&R lines,
  "Refn" for refinish-only; unit part price; per-line labor and paint hours; rates from the totals block.
- `loss_cause` is classified from the facts, not the claimant's label.
- Citations: short verbatim quotes.
For the recommendation, follow section 7's decision order (first match wins). The claims system computes the payout
from your extraction; do not state an amount.

CLAIMS MANUAL:
{manual}"""


def _policy_number(docs: dict) -> str | None:
    m = re.search(r"Policy number:\s*(\S+)", "\n".join(docs["fnol"]["pages"]))
    return m.group(1) if m else None


def _finish(ctx: RunContext, claim_id: str, out: SinglePassDecision) -> dict:
    """Shared post-processing: code computes facts and payout from the model's own extraction."""
    ex = out.extraction
    pol = ctx.tool("lookup_policy", tools.lookup_policy, policy_number=ex.policy_number)
    val = ctx.tool("vehicle_valuation", tools.vehicle_valuation, vin=ex.vin)
    if not val["found"] and pol["found"] and pol.get("vehicle"):
        val = ctx.tool("vehicle_valuation", tools.vehicle_valuation, vin=pol["vehicle"]["vin"])
    pc = None
    if ex.estimates:
        pc = ctx.tool("price_check", tools.price_check, estimates=[e.model_dump() for e in ex.estimates],
                      state=pol["state"] if pol["found"] else ex.loss_state)
    covs = {c["code"]: rules.to_cents(c["deductible"]) for c in pol.get("coverages", [])} if pol["found"] else {}
    acv = rules.to_cents(val["acv"]) if val.get("found") else 0
    approved = pc["approved_total_c"] if pc else None
    total_loss = approved is not None and acv > 0 and approved > rules.TOTAL_LOSS_RATIO * acv
    ded = covs.get(out.coverage_code, 0)
    payout = rules.payout_for(out.decision, approved, acv, ded, total_loss)
    rec = Recommendation(decision=out.decision, reason_codes=list(out.reason_codes), coverage_code=out.coverage_code,
                         payout_c=payout, rationale=out.rationale, citations=ex.citations[:4])
    return {"extracted": ex.model_dump(mode="json"), "recommendation": rec.model_dump(mode="json") | {"hash": rec.digest()}}


def single_prompt(ctx: RunContext, claim_id: str) -> dict:
    docs = tools.claim_documents(claim_id)
    ctx.node, ctx.visit = "single_prompt", 0
    pn = _policy_number(docs)
    pol = ctx.tool("lookup_policy", tools.lookup_policy, policy_number=pn or "")
    records: dict = {"policy": pol}
    if pol["found"] and pol.get("vehicle"):
        vin = pol["vehicle"]["vin"]
        records["valuation"] = ctx.tool("vehicle_valuation", tools.vehicle_valuation, vin=vin)
        records["prior_claims"] = ctx.tool("get_prior_claims", tools.get_prior_claims, vin=vin)
        cls = pol["vehicle"]["vehicle_class"]
        parts = [r["part_number"] for r in ctx.conn.execute(
            "SELECT part_number FROM parts_catalog WHERE vehicle_class=%s ORDER BY part_number", (cls,))]
        records["reference_prices"] = ctx.tool("reference_prices", tools.reference_prices, part_numbers=parts)
        records["state_rates"] = tools.state_rates(ctx.conn, pol["state"])
    system = BASE_SYSTEM.replace("{manual}", "\n\n".join(manual_sections().values()))
    user = (f"Claim {claim_id}.\n\nCLAIMS-SYSTEM RECORDS (fetched by the intake system):\n"
            f"{json.dumps(records, indent=1, default=str)}\n\nDOCUMENTS:\n\n{render_docs(docs)}")
    try:
        out = llm.complete("single_prompt", system, user, SinglePassDecision)
    except BudgetExceeded as e:
        return {"terminal": {"kind": "ESCALATED_BUDGET", "detail": str(e)}, "path": ["single_prompt"]}
    except (LLMUnavailable, SchemaViolation) as e:
        return {"terminal": {"kind": "ESCALATED_ERROR", "detail": str(e)[:600]}, "path": ["single_prompt"]}
    return _finish(ctx, claim_id, out) | {"path": ["single_prompt"],
                                          "terminal": {"kind": "AWAITING_APPROVAL", "detail": "baseline"}}


AGENT_SYSTEM = BASE_SYSTEM + """

You work in steps. Each step returns JSON with `thought`, `tool_calls` and `final`.
Tools (claims system):
- lookup_policy(policy_number): policy record, drivers, coverages, vehicle.
- vehicle_valuation(vin): vehicle record and actual cash value (ACV).
- get_prior_claims(vin): prior claims on that VIN.
- reference_prices(part_numbers): reference catalog price and hours per part number.
Call the tools you need (several per step is fine). When you have what you need, set `final` and leave
`tool_calls` empty. You have at most {rounds} tool steps."""


def single_agent(ctx: RunContext, claim_id: str) -> dict:
    docs = tools.claim_documents(claim_id)
    rounds = settings()["limits"]["agent_max_tool_rounds"]
    system = AGENT_SYSTEM.replace("{manual}", "\n\n".join(manual_sections().values())).replace("{rounds}", str(rounds))
    transcript = [f"Claim {claim_id}.\n\nDOCUMENTS:\n\n{render_docs(docs)}"]
    path: list[str] = []
    fns = {"lookup_policy": tools.lookup_policy, "vehicle_valuation": tools.vehicle_valuation,
           "get_prior_claims": tools.get_prior_claims, "reference_prices": tools.reference_prices}
    try:
        for i in range(rounds + 1):
            ctx.node, ctx.visit, ctx.call_idx = "agent_step", i, 0
            path.append("agent_step")
            last = i == rounds
            user = "\n\n".join(transcript) + ("\n\nYou are out of tool steps: give `final` now." if last else "")
            with ctx.tracer.span("node.agent_step", kind="agent", parent=ctx.tracer.root_id,
                                 metadata={"step": i + 1}) as sp:
                step = llm.complete("single_agent", system, user, AgentStep)
                sp.update(output={"thought": step.thought, "tools": [t.tool for t in step.tool_calls],
                                  "final": step.final is not None})
            if step.final is not None:
                return _finish(ctx, claim_id, step.final) | {
                    "path": path, "terminal": {"kind": "AWAITING_APPROVAL", "detail": "baseline"}}
            if last or not step.tool_calls:
                transcript.append("You returned no tool calls and no final answer. Give `final` now.")
                continue
            results = []
            for tc in step.tool_calls[:6]:
                if tc.tool == "reference_prices":
                    res = ctx.tool(tc.tool, fns[tc.tool], part_numbers=tc.part_numbers[:30])
                elif tc.tool == "lookup_policy":
                    res = ctx.tool(tc.tool, fns[tc.tool], policy_number=tc.policy_number or "")
                else:
                    res = ctx.tool(tc.tool, fns[tc.tool], vin=tc.vin or "")
                results.append({"tool": tc.tool, "args": tc.model_dump(exclude_none=True), "result": res})
            transcript.append(f"STEP {i + 1} you thought: {step.thought}\nTOOL RESULTS:\n"
                              f"{json.dumps(results, indent=1, default=str)}")
        return {"terminal": {"kind": "ESCALATED_STEP_LIMIT", "detail": "agent never answered"}, "path": path}
    except BudgetExceeded as e:
        return {"terminal": {"kind": "ESCALATED_BUDGET", "detail": str(e)}, "path": path}
    except (LLMUnavailable, SchemaViolation) as e:
        return {"terminal": {"kind": "ESCALATED_ERROR", "detail": str(e)[:600]}, "path": path}
