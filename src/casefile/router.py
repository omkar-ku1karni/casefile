"""The supervisor's routing policy: a pure function from state to (next node, state updates, reason).

No LLM is involved in routing. The termination guards run first, in a fixed order, on every hop.
That makes "does the graph always stop?" a question about this one function, which
tests/test_termination.py answers with exhaustive cases and a Hypothesis property test.
"""

from __future__ import annotations

END = "__end__"
SPECIALISTS = ("extractor", "investigator", "recommend", "reviewer")
TARGET_ORDER = ("extractor", "investigator", "recommend")


def issue_signature(issues: list[dict]) -> str:
    return "|".join(sorted(f"{i['code']}:{i['target']}:{i.get('field')}" for i in issues))


def _escalate(kind: str, detail: str) -> tuple[str, dict, str]:
    return "escalate", {"escalation": {"kind": kind, "detail": detail}}, kind


def decide_next(s: dict) -> tuple[str, dict, str]:
    if s.get("terminal"):
        return END, {}, "terminal state reached"
    lim = s["limits"]
    led = s["ledger"]

    # 1. guards, always first
    if led["tokens_in"] + led["tokens_out"] >= led["max_tokens"] or led["usd"] >= led["max_usd"]:
        return _escalate("ESCALATED_BUDGET", "budget ceiling reached")
    if s["step"] >= lim["max_steps"]:
        return _escalate("ESCALATED_STEP_LIMIT", f"{s['step']} specialist steps >= max_steps {lim['max_steps']}")

    # 2. pending rework goes to its target
    if s.get("rework"):
        return s["rework"]["target"], {}, f"rework requested for {s['rework']['target']}"

    # 3. the pipeline, by what is missing
    if s.get("extracted") is None:
        return "extractor", {}, "no extraction yet"
    if s.get("findings") is None:
        return "investigator", {}, "no findings yet"
    if s.get("recommendation") is None:
        return "recommend", {}, "no recommendation yet"
    if not s.get("reviewer_enabled", True):
        return "human_gate", {}, "reviewer disabled; recommendation goes to a human"

    rec_hash = s["recommendation"]["hash"]
    fresh = (s.get("reviews") or [])[s.get("reviews_consumed", 0):]  # reviews not yet acted on
    last = fresh[-1] if fresh else None
    if last is None or last["recommendation_hash"] != rec_hash:
        return "reviewer", {}, "current recommendation not reviewed"
    if last["verdict"] == "ACCEPT":
        return "human_gate", {}, "reviewer accepted"

    # 4. REVISE: bounded, and only if it is not the same complaint again
    open_issues = "; ".join(f"[{i['code']}] {i['detail']}" for i in last["issues"])[:600]
    if sum(s["revisions"].values()) >= lim["max_revisions"]:
        return _escalate("ESCALATED_REVISION_LIMIT", f"revision limit {lim['max_revisions']} reached; open: {open_issues}")
    sig = issue_signature(last["issues"])
    if sig in s.get("issue_history", []):
        return _escalate("ESCALATED_NO_PROGRESS", f"reviewer raised the same issues again: {open_issues}")
    target = min((i["target"] for i in last["issues"]), key=TARGET_ORDER.index)
    rework = {"target": target, "issues": [i for i in last["issues"] if i["target"] == target]}
    updates = {
        "rework": rework,
        "revisions": {**s["revisions"], target: s["revisions"].get(target, 0) + 1},
        "issue_history": [*s.get("issue_history", []), sig],
        "reviews_consumed": len(s["reviews"]),
    }
    if target == "extractor":
        updates |= {"findings": None, "recommendation": None}
    elif target == "investigator":
        updates |= {"recommendation": None}
    return target, updates, f"reviewer sent work back to {target}"
