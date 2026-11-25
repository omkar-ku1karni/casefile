"""Run, resume, approve and replay claims. Every run gets a row in `runs`, a local trace file, and
(when configured) a Langfuse trace."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from langgraph.types import Command

from casefile import baselines, db
from casefile.config import config_hash, git_sha, settings
from casefile.contracts import Approval
from casefile.graph import compiled, recursion_limit
from casefile.runtime import Ledger, RunContext, use
from casefile.telemetry import Tracer, register

SYSTEMS = ("casefile", "casefile_no_reviewer", "single_prompt", "single_agent")
SHORT = {"casefile": "cf", "casefile_no_reviewer": "nr", "single_prompt": "sp", "single_agent": "sa"}


def new_run_id(claim_id: str, system: str) -> str:
    return f"{claim_id}-{SHORT[system]}-{uuid.uuid4().hex[:6]}"


def _limits(overrides: dict | None = None) -> dict:
    lim = dict(settings()["limits"])
    lim.update(overrides or {})
    return lim


def _initial_state(claim_id: str, system: str, lim: dict) -> dict:
    with db.conn() as c:
        row = c.execute("SELECT documents FROM claims WHERE claim_id=%s", (claim_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown claim {claim_id} (run `casefile db seed`?)")
    return {
        "claim_id": claim_id, "system": system, "documents": list(row["documents"]),
        # The wall clock is a production SLA guard. In pinned-model eval runs it would measure provider
        # outages (503 back-off), not the system, so eval mode turns it off; steps and budget still bind.
        "limits": {"max_steps": lim["max_steps"], "max_revisions": lim["max_revisions"],
                   "wall_clock_s": None if os.environ.get("CASEFILE_NO_FALLBACK") == "1" else lim["wall_clock_s"]},
        "reviewer_enabled": system != "casefile_no_reviewer", "started_at": time.time(),
        "extracted": None, "findings": None, "recommendation": None, "reviews": [], "reviews_consumed": 0, "rework": None,
        "revisions": {}, "issue_history": [], "route_log": [], "step": 0, "path": [],
        "ledger": Ledger(max_tokens=lim["max_tokens"], max_usd=lim["max_usd"]).to_dict(),
        "escalation": None, "terminal": None, "approval": None, "write_back": None,
    }


def _insert_run(run_id, claim_id, system, mode, batch, lim, tracer, replay_of=None) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO runs (run_id, claim_id, system, batch, replay_of, llm_mode, status, ceiling_usd, "
            "ceiling_tokens, trace_id, trace_url, config_hash, git_sha) "
            "VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s,%s,%s,%s)",
            (run_id, claim_id, system, batch, replay_of, mode, lim["max_usd"], lim["max_tokens"],
             tracer.trace_id, tracer.url(), config_hash(), git_sha()))


def _config(run_id: str, mode: str, cassette_run: str | None) -> dict:
    return {"configurable": {"thread_id": run_id, "llm_mode": mode, "cassette_run": cassette_run},
            "recursion_limit": recursion_limit()}


def _status(values: dict, interrupted: bool) -> str:
    t = values.get("terminal") or {}
    if interrupted:
        return "awaiting_approval"
    kind = t.get("kind", "")
    if kind == "DECIDED":
        return "decided"
    if kind.startswith("ESCALATED"):
        return "escalated"
    if kind in ("REJECTED_BY_HUMAN", "RETURNED_BY_HUMAN"):
        return "closed_by_human"
    if kind == "AWAITING_APPROVAL":
        return "awaiting_approval"
    return "unknown"


def summarize(values: dict, interrupted: bool) -> dict:
    rec = values.get("recommendation") or {}
    t = values.get("terminal") or ({"kind": "AWAITING_APPROVAL", "detail": "paused at human gate"}
                                   if interrupted else {})
    led = values.get("ledger") or {}
    return {
        "status": _status(values, interrupted),
        "terminal_kind": t.get("kind"),
        "terminal_detail": t.get("detail"),
        "decision": rec.get("decision") if t.get("kind") in ("AWAITING_APPROVAL", "DECIDED") or interrupted else None,
        "recommended_decision": rec.get("decision"),
        "payout_c": rec.get("payout_c"),
        "reason_codes": rec.get("reason_codes"),
        "rec_hash": rec.get("hash"),
        "path": values.get("path", []),
        "steps": values.get("step", len(values.get("path", []))),
        "revisions": sum((values.get("revisions") or {}).values()),
        "llm_calls": led.get("llm_calls", 0),
        "tokens_in": led.get("tokens_in", 0),
        "tokens_out": led.get("tokens_out", 0),
        "usd": led.get("usd", 0.0),
    }


def _finalize(run_id: str, values: dict, interrupted: bool, t0: float, error: str | None = None) -> dict:
    s = summarize(values, interrupted)
    if error:
        s["status"] = "error"
    with db.conn() as c:
        c.execute(
            "UPDATE runs SET status=%s, terminal_kind=%s, decision=%s, payout_c=%s, reason_codes=%s, steps=%s, "
            "revisions=%s, llm_calls=%s, tokens_in=%s, tokens_out=%s, usd=%s, path=%s, final_state=%s, error=%s, "
            "latency_ms=COALESCE(latency_ms, 0) + %s, ended_at=now() WHERE run_id=%s",
            (s["status"], s["terminal_kind"], s["decision"], s["payout_c"], s["reason_codes"], s["steps"],
             s["revisions"], s["llm_calls"], s["tokens_in"], s["tokens_out"], s["usd"], s["path"],
             db.jsonb(values), error, int((time.perf_counter() - t0) * 1000), run_id))
    return s | {"run_id": run_id}


def _invoke_graph(run_id: str, inp: Any, mode: str, cassette_run: str | None, tracer: Tracer, label: str) -> dict:
    g = compiled()
    cfg = _config(run_id, mode, cassette_run)
    t0 = time.perf_counter()
    err = None
    with tracer.span(label, kind="chain", root=True, input={"run_id": run_id, "mode": mode}) as root:
        try:
            g.invoke(inp, cfg, durability="sync")
        except Exception as e:  # surfaced in the run row; CassetteMiss lands here in replays
            err = f"{type(e).__name__}: {e}"[:1000]
            root.update(level="ERROR", status=err)
        snap = g.get_state(cfg)
        interrupted = bool(snap.next) and "human_gate" in snap.next
        root.update(output=summarize(snap.values, interrupted))
    tracer.flush()
    out = _finalize(run_id, snap.values, interrupted, t0, err)
    if err:
        out["error"] = err
    return out


def run_claim(claim_id: str, system: str = "casefile", *, mode: str = "live", batch: str | None = None,
              limit_overrides: dict | None = None, run_id: str | None = None,
              cassette_run: str | None = None) -> dict:
    if system not in SYSTEMS:
        raise ValueError(f"system must be one of {SYSTEMS}")
    lim = _limits(limit_overrides)
    run_id = run_id or new_run_id(claim_id, system)
    tracer = register(Tracer(run_id, claim_id=claim_id, system=system, tags=[batch] if batch else []))
    _insert_run(run_id, claim_id, system, mode, batch, lim, tracer, replay_of=cassette_run)
    if system.startswith("casefile"):
        return _invoke_graph(run_id, _initial_state(claim_id, system, lim), mode, cassette_run, tracer, "claim.run")
    return _run_baseline(run_id, claim_id, system, lim, tracer)


def _run_baseline(run_id: str, claim_id: str, system: str, lim: dict, tracer: Tracer) -> dict:
    ledger = Ledger(max_tokens=lim["max_tokens"], max_usd=lim["max_usd"])
    ctx = RunContext(run_id, claim_id, system, "live", None, ledger, tracer, node=system)
    t0 = time.perf_counter()
    with tracer.span("claim.run", kind="chain", root=True, input={"run_id": run_id, "system": system}) as root, \
            use(ctx):
        fn = baselines.single_prompt if system == "single_prompt" else baselines.single_agent
        try:
            values = fn(ctx, claim_id)
        finally:
            ctx.close()
        values |= {"claim_id": claim_id, "system": system, "ledger": ledger.to_dict(),
                   "step": len(values.get("path", []))}
        root.update(output=summarize(values, False))
    tracer.flush()
    return _finalize(run_id, values, False, t0)


def resume(run_id: str) -> dict:
    """Continue a run from its last Postgres checkpoint (e.g. after the process died mid-run)."""
    with db.conn() as c:
        r = c.execute("SELECT claim_id, system, llm_mode, replay_of FROM runs WHERE run_id=%s", (run_id,)).fetchone()
    tracer = register(Tracer(run_id, claim_id=r["claim_id"], system=r["system"], tags=["resume"]))
    cassette = r["replay_of"] if r["llm_mode"] == "replay" else None
    return _invoke_graph(run_id, None, r["llm_mode"], cassette, tracer, "claim.resume")


def approve(run_id: str, action: str = "APPROVE", approver: str = "adjuster@demo", note: str = "") -> dict:
    """The human gate. Writes the approval row, then resumes the paused graph with it."""
    g = compiled()
    with db.conn() as c:
        r = c.execute("SELECT claim_id, system, llm_mode FROM runs WHERE run_id=%s", (run_id,)).fetchone()
    cfg = _config(run_id, r["llm_mode"], None)
    snap = g.get_state(cfg)
    if not snap.next or "human_gate" not in snap.next:
        raise RuntimeError(f"run {run_id} is not waiting at the human gate")
    rec = snap.values["recommendation"]
    appr = Approval(approval_id=f"AP-{uuid.uuid4().hex[:10]}", action=action, approver=approver,
                    recommendation_hash=rec["hash"], note=note)
    with db.conn() as c:
        c.execute("INSERT INTO approvals VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (appr.approval_id, run_id, r["claim_id"], rec["hash"], action, approver, note))
    tracer = register(Tracer(run_id, claim_id=r["claim_id"], system=r["system"], tags=["approval"]))
    return _invoke_graph(run_id, Command(resume=appr.model_dump()), r["llm_mode"], None, tracer, "claim.approval")


def checkpoints(run_id: str) -> list[dict]:
    """Specialist-boundary snapshots of a run, oldest first."""
    g = compiled()
    out = []
    for snap in g.get_state_history({"configurable": {"thread_id": run_id}}):
        v = snap.values
        if snap.next == ("supervisor",) and v.get("path"):
            out.append({"step": v["step"], "last_node": v["path"][-1], "checkpoint_id":
                        snap.config["configurable"]["checkpoint_id"], "values": v})
    return sorted(out, key=lambda x: x["step"])


def replay(run_id: str, from_step: int | None = None, mode: str = "replay") -> dict:
    """Copy the snapshot taken after `from_step` into a new thread and re-execute the rest.

    mode="replay": LLM responses are served from the original run's recordings, so the outcome must be
    identical - if the code changed in a way that alters any prompt, the cassette refuses (request hash).
    mode="live":   the same snapshot re-executed against live models - measures model variance.
    """
    with db.conn() as c:
        orig = c.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,)).fetchone()
    snaps = checkpoints(run_id)
    if not snaps:
        raise RuntimeError(f"no checkpoints for {run_id}")
    snap = snaps[0] if from_step is None else next(s for s in snaps if s["step"] == from_step)
    new_id = f"{run_id}-rp{snap['step']}-{uuid.uuid4().hex[:4]}"
    lim = _limits()
    tracer = register(Tracer(new_id, claim_id=orig["claim_id"], system=orig["system"], tags=["replay", mode]))
    _insert_run(new_id, orig["claim_id"], orig["system"], mode, "replay", lim, tracer, replay_of=run_id)
    g = compiled()
    cfg = _config(new_id, mode, run_id if mode == "replay" else None)
    g.update_state(cfg, dict(snap["values"], started_at=time.time()), as_node=snap["last_node"])
    out = _invoke_graph(new_id, None, mode, run_id if mode == "replay" else None, tracer, "claim.replay")
    original = summarize(orig["final_state"] or {}, orig["status"] == "awaiting_approval")
    keys = ("terminal_kind", "recommended_decision", "payout_c", "rec_hash", "path")
    same = {k: original.get(k) == out.get(k) for k in keys}
    return {"original": {k: original.get(k) for k in keys}, "replay": {k: out.get(k) for k in keys},
            "from_step": snap["step"], "replay_run_id": new_id, "identical": all(same.values()), "fields": same}


def counterfactual(run_id: str, max_usd: float, batch: str) -> dict:
    """Re-run a recorded claim from the start with a different ceiling, serving every LLM response from
    the recording. The budget guard projects and blocks exactly as it would live, so this measures what a
    lower ceiling would have done - with zero API calls."""
    with db.conn() as c:
        orig = c.execute("SELECT claim_id, system FROM runs WHERE run_id=%s", (run_id,)).fetchone()
    return run_claim(orig["claim_id"], orig["system"], mode="replay", batch=batch, cassette_run=run_id,
                     limit_overrides={"max_usd": max_usd}, run_id=f"{run_id}-cf{int(max_usd * 1e5)}")
