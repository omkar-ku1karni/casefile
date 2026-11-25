"""Human-readable views of a run's local trace: node table and Mermaid sequence diagram."""

from __future__ import annotations

from casefile.paths import RUNS_DIR
from casefile.telemetry import read_trace


def _spans(run_id: str) -> list[dict]:
    spans = read_trace(run_id)
    if not spans:
        for p in RUNS_DIR.glob(f"*/{run_id}/trace.jsonl"):
            import json

            spans = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return sorted(spans, key=lambda s: s.get("start", ""))


def node_steps(run_id: str) -> list[dict]:
    spans = _spans(run_id)
    by_parent: dict[str, list[dict]] = {}
    for s in spans:
        by_parent.setdefault(s.get("parent_id"), []).append(s)

    def descendants(sid):
        out = []
        for ch in by_parent.get(sid, []):
            out.append(ch)
            out += descendants(ch["span_id"])
        return out

    steps = []
    for s in spans:
        if s["name"].startswith("node.") or s["name"] in ("human_gate", "escalate", "tool.record_decision"):
            d = descendants(s["span_id"])
            gens = [x for x in d if x["kind"] == "generation"]
            steps.append({
                "node": s["name"].removeprefix("node."), "step": (s.get("metadata") or {}).get("step"),
                "ms": s.get("duration_ms"), "llm_calls": len(gens),
                "tokens": sum((g.get("usage") or {}).get("input", 0) + (g.get("usage") or {}).get("output", 0) for g in gens),
                "usd": sum(g.get("cost_usd") or 0 for g in gens),
                "tools": [x["name"].removeprefix("tool.") for x in d if x["kind"] == "tool"],
                "output": s.get("output"), "level": s.get("level"),
            })
        elif s["name"] == "supervisor.route":
            out = s.get("output") or {}
            steps.append({"node": "supervisor", "route": out.get("next"), "reason": out.get("reason")})
    return steps


def node_table(run_id: str) -> str:
    lines = [f"run {run_id}", f"{'step':>4}  {'node':13s} {'calls':>5} {'tokens':>7} {'usd':>9} {'ms':>8}  detail"]
    for s in node_steps(run_id):
        if s["node"] == "supervisor":
            lines.append(f"{'':>4}  {'  route ->':13s} {s['route']:<32s} ({s['reason']})")
            continue
        detail = ""
        out = s.get("output") or {}
        if s["node"] == "reviewer" and isinstance(out, dict) and out.get("review"):
            rv = out["review"]
            detail = rv["verdict"] + "".join(f" [{i['code']}->{i['target']}]" for i in rv["issues"])
        elif s["node"] == "recommend" and isinstance(out, dict) and out.get("recommendation"):
            r = out["recommendation"]
            detail = f"{r['decision']} payout=${(r['payout_c'] or 0) / 100:,.2f}"
        elif s["tools"]:
            detail = "tools: " + ", ".join(s["tools"])
        if isinstance(out, dict) and out.get("terminal"):
            detail += f" TERMINAL {out['terminal']['kind']}"
        lines.append(f"{s['step'] or '':>4}  {s['node']:13s} {s['llm_calls']:>5} {s['tokens']:>7} "
                     f"{s['usd']:>9.5f} {s['ms'] or 0:>8.0f}  {detail}")
    return "\n".join(lines)


def sequence_diagram(run_id: str) -> str:
    out = ["sequenceDiagram", "    participant S as supervisor"]
    seen = set()
    body = []
    for s in node_steps(run_id):
        n = s["node"]
        if n == "supervisor" or n == "record_decision":
            continue
        if n not in seen:
            seen.add(n)
            out.append(f"    participant {n}")
        o = s.get("output") or {}
        note = ""
        if n == "reviewer" and isinstance(o, dict) and o.get("review"):
            rv = o["review"]
            note = rv["verdict"] + ("" if not rv["issues"] else ": " + ", ".join(
                f"{i['code']} to {i['target']}" for i in rv["issues"]))
        elif n == "recommend" and isinstance(o, dict) and o.get("recommendation"):
            note = o["recommendation"]["decision"]
        body.append(f"    S->>{n}: step {s['step'] or ''}")
        body.append(f"    {n}-->>S: {note or 'done'} ({s['llm_calls']} calls, ${s['usd']:.5f})")
    return "\n".join(out + body)
