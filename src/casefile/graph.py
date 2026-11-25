"""The LangGraph wiring. Hub and spoke: every specialist returns to the supervisor, which is the only
node with conditional edges, and whose policy is the pure function ``router.decide_next``.

    START -> supervisor -+-> extractor ----+
                         +-> investigator -+-> supervisor
                         +-> recommend ----+
                         +-> reviewer -----+
                         +-> human_gate --(APPROVE)--> write_back -> END
                         |             \\-(REJECT/RETURN)---------> END
                         +-> escalate -> END
"""

from __future__ import annotations

import os
import time
from typing import Any, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from casefile import agents, db, tools
from casefile.config import settings
from casefile.contracts import Approval
from casefile.router import decide_next
from casefile.runtime import (
    BudgetExceeded,
    Ledger,
    LLMUnavailable,
    RunContext,
    SchemaViolation,
    use,
)
from casefile.telemetry import tracer_for


class CaseState(TypedDict, total=False):
    claim_id: str
    system: str
    documents: list[str]
    limits: dict
    reviewer_enabled: bool
    started_at: float
    extracted: dict | None
    findings: dict | None
    recommendation: dict | None
    reviews: list[dict]
    reviews_consumed: int
    rework: dict | None
    revisions: dict
    issue_history: list[str]
    ledger: dict
    step: int
    path: list[str]
    route_log: list[dict]
    next: str
    escalation: dict | None
    terminal: dict | None
    approval: dict | None
    write_back: dict | None


KIND = {"extractor": "agent", "investigator": "agent", "recommend": "agent", "reviewer": "evaluator"}
SPECIALIST_FNS = {"extractor": agents.extractor, "investigator": agents.investigator,
                  "recommend": agents.recommend, "reviewer": agents.reviewer}


def _cfg(config) -> dict:
    return config.get("configurable", {})


def _summary(updates: dict) -> dict:
    out = {}
    for k, v in updates.items():
        if k == "recommendation" and v:
            out[k] = {x: v[x] for x in ("decision", "reason_codes", "payout_c", "coverage_code")}
        elif k == "reviews" and v:
            out["review"] = v[-1]
        elif k == "findings" and v:
            out[k] = {x: v[x] for x in ("policy_active", "coverage_code", "coverage_present", "siu_flags",
                                         "missing_documents", "approved_total_c", "total_loss")}
        elif k == "extracted" and v:
            out[k] = {x: v[x] for x in ("policy_number", "vin", "loss_date", "loss_cause", "driver_name")} | {
                "n_estimates": len(v["estimates"])}
        else:
            out[k] = v
    return out


def specialist(name: str):
    fn = SPECIALIST_FNS[name]

    def node(state: CaseState, config) -> dict:
        cfg = _cfg(config)
        run_id = cfg["thread_id"]
        tracer = tracer_for(run_id)
        if os.environ.get("CASEFILE_CRASH_BEFORE") == name:  # resume demo: die hard, mid-run
            tracer.event("simulated_crash", before=name)
            tracer.flush()
            os._exit(17)
        ledger = Ledger.from_dict(state["ledger"])
        visit = state["path"].count(name)
        ctx = RunContext(run_id, state["claim_id"], state["system"], cfg.get("llm_mode", "live"),
                         cfg.get("cassette_run"), ledger, tracer, node=name, visit=visit)
        updates: dict[str, Any] = {}
        meta = {"step": state["step"] + 1, "visit": visit}
        with use(ctx), tracer.span(f"node.{name}", kind=KIND[name], parent=tracer.root_id, metadata=meta) as sp:
            try:
                updates = fn(state, ctx)
            except BudgetExceeded as e:
                updates = {"terminal": {"kind": "ESCALATED_BUDGET", "detail": str(e)}}
                sp.update(level="WARNING", status=str(e))
            except (LLMUnavailable, SchemaViolation) as e:
                updates = {"terminal": {"kind": "ESCALATED_ERROR", "detail": f"{type(e).__name__}: {e}"[:600]}}
                sp.update(level="ERROR", status=str(e)[:300])
            finally:
                ctx.close()
            sp.update(output=_summary(updates),
                      metadata={"ledger_usd": ledger.usd, "ledger_tokens": ledger.tokens, "llm_calls": ledger.llm_calls})
        return updates | {"step": state["step"] + 1, "path": state["path"] + [name], "ledger": ledger.to_dict()}

    node.__name__ = name
    return node


def supervisor(state: CaseState, config) -> dict:
    cfg = _cfg(config)
    tracer = tracer_for(cfg["thread_id"])
    nxt, updates, reason = decide_next(state)
    wall = state["limits"].get("wall_clock_s")
    if (cfg.get("llm_mode", "live") == "live" and wall and nxt not in (END, "escalate", "human_gate")
            and time.time() - state["started_at"] > wall):
        nxt, updates, reason = "escalate", {"escalation": {"kind": "ESCALATED_STEP_LIMIT",
                                                           "detail": f"wall clock > {wall}s"}}, "wall clock"
    with tracer.span("supervisor.route", kind="chain", parent=tracer.root_id,
                     metadata={"step": state["step"]}) as sp:
        sp.update(output={"next": nxt, "reason": reason})
    log = state.get("route_log", []) + [{"step": state["step"], "next": nxt, "reason": reason}]
    return updates | {"next": nxt, "route_log": log}


def human_gate(state: CaseState, config) -> dict:
    """Pause for an adjuster. Nothing below this node runs without an explicit human action."""
    rec = state["recommendation"]
    decision = interrupt({
        "claim_id": state["claim_id"], "recommendation": rec, "path": state["path"],
        "message": "Approve, reject or return this recommendation.",
    })
    approval = Approval.model_validate(decision)
    tracer = tracer_for(_cfg(config)["thread_id"])
    with tracer.span("human_gate", kind="guardrail", parent=tracer.root_id, input={"rec_hash": rec["hash"]}) as sp:
        sp.update(output=approval.model_dump())
    upd: dict[str, Any] = {"approval": approval.model_dump(), "path": state["path"] + ["human_gate"]}
    if approval.action == "REJECT":
        upd["terminal"] = {"kind": "REJECTED_BY_HUMAN", "detail": approval.note or "rejected by adjuster"}
    elif approval.action == "RETURN":
        upd["terminal"] = {"kind": "RETURNED_BY_HUMAN", "detail": approval.note or "returned by adjuster"}
    return upd


def write_back(state: CaseState, config) -> dict:
    cfg = _cfg(config)
    tracer = tracer_for(cfg["thread_id"])
    rec, appr = state["recommendation"], state["approval"]
    if appr["action"] != "APPROVE" or appr["recommendation_hash"] != rec["hash"]:
        raise RuntimeError("write_back reached without a matching approval")  # unreachable by construction
    with db.conn() as c, tracer.span("tool.record_decision", kind="tool", parent=tracer.root_id) as sp:
        res = tools.record_decision(c, claim_id=state["claim_id"], run_id=cfg["thread_id"],
                                    approval_id=appr["approval_id"], recommendation_hash=rec["hash"],
                                    decision=rec["decision"], payout_c=rec["payout_c"])
        sp.update(output=res)
    return {"write_back": res, "path": state["path"] + ["write_back"],
            "terminal": {"kind": "DECIDED", "detail": f"{rec['decision']} written back after approval"}}


def escalate(state: CaseState, config) -> dict:
    tracer = tracer_for(_cfg(config)["thread_id"])
    esc = state["escalation"]
    with tracer.span("escalate", kind="guardrail", parent=tracer.root_id) as sp:
        sp.update(output=esc, level="WARNING")
    return {"terminal": esc, "path": state["path"] + ["escalate"]}


def build(checkpointer) -> Any:
    g = StateGraph(CaseState)
    g.add_node("supervisor", supervisor)
    for name in SPECIALIST_FNS:
        g.add_node(name, specialist(name))
        g.add_edge(name, "supervisor")
    g.add_node("human_gate", human_gate)
    g.add_node("write_back", write_back)
    g.add_node("escalate", escalate)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["next"],
                            {n: n for n in (*SPECIALIST_FNS, "human_gate", "escalate")} | {"__end__": END})
    g.add_conditional_edges("human_gate", lambda s: "write_back" if s["approval"]["action"] == "APPROVE" else END,
                            {"write_back": "write_back", END: END})
    g.add_edge("write_back", END)
    g.add_edge("escalate", END)
    return g.compile(checkpointer=checkpointer)


_saver_conn = None
_graph = None


def compiled():
    """Process-wide compiled graph with a Postgres checkpointer."""
    global _saver_conn, _graph
    if _graph is None:
        import psycopg
        from psycopg.rows import dict_row

        _saver_conn = psycopg.connect(db.dsn(), autocommit=True, prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(_saver_conn)
        saver.setup()
        _graph = build(saver)
    return _graph


def recursion_limit() -> int:
    # Each specialist step is two graph super-steps (node + supervisor). This is a backstop only:
    # the router escalates at max_steps long before it, and a test asserts it never fires.
    return 2 * settings()["limits"]["max_steps"] + 10


def mermaid() -> str:
    from langgraph.checkpoint.memory import InMemorySaver

    return build(InMemorySaver()).get_graph().draw_mermaid()
