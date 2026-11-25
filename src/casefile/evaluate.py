"""Score recorded runs against computed ground truth. Code-graded only: no LLM judge is needed because
every expected decision, payout and flag was computed by the claims-manual rules from world facts."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from functools import lru_cache

from casefile import db, rules
from casefile.paths import EVAL_DIR


@lru_cache
def truth() -> dict[str, dict]:
    return {r["claim_id"]: r for r in map(json.loads, (EVAL_DIR / "claims.jsonl").read_text().splitlines())}


def _norm_name(s: str | None) -> str:
    p = (s or "").replace(".", "").lower().split()
    return f"{p[0]} {p[-1]}" if len(p) >= 2 else " ".join(p)


def extraction_scores(ex: dict | None, gold: dict) -> dict[str, bool]:
    if not ex:
        return {k: False for k in gold}
    est_total = None
    try:
        from casefile.contracts import ExtractedEstimate
        from casefile.tools import to_rules

        est_total = sum(rules.estimate_totals(*to_rules(ExtractedEstimate.model_validate(e))).total_c
                        for e in ex.get("estimates", [])) if ex.get("estimates") else None
    except Exception:
        est_total = None
    pr = ex.get("police_report") or {}
    return {
        "policy_number": (ex.get("policy_number") or "").strip().upper() == gold["policy_number"],
        "vin": (ex.get("vin") or "").strip().upper() == gold["vin"],
        "loss_date": ex.get("loss_date") == gold["loss_date"],
        "loss_cause": ex.get("loss_cause") == gold["loss_cause"],
        "driver_name": _norm_name(ex.get("driver_name")) == _norm_name(gold["driver_name"]),
        "other_vehicle": ex.get("other_vehicle_involved") == gold["other_vehicle"],
        "police_report_number": (pr.get("report_number") if pr else None) == gold["police_report_number"],
        "police_incident_date": (pr.get("incident_date") if pr else None) == gold["police_incident_date"],
        "n_estimates": len(ex.get("estimates") or []) == gold["n_estimates"],
        "billed_total": (est_total is None and gold["billed_total_c"] is None)
                        or (est_total is not None and gold["billed_total_c"] is not None
                            and abs(est_total - gold["billed_total_c"]) <= 100),
    }


def score_run(run: dict) -> dict:
    gt = truth()[run["claim_id"]]
    t = gt["truth"]
    fs = run["final_state"] or {}
    rec = fs.get("recommendation") or {}
    term = (fs.get("terminal") or {}).get("kind") or run["terminal_kind"]
    escalated = (term or "").startswith("ESCALATED") or run["status"] in ("escalated", "error")
    pred = None if escalated else rec.get("decision")
    paying_truth = t["decision"] in rules.PAYING
    paying_pred = pred in rules.PAYING
    findings = fs.get("findings") or {}
    flagged = set(findings.get("siu_flags") or []) | set(rec.get("reason_codes") or [])
    planted = set(t["siu_flags"])
    ext = extraction_scores(fs.get("extracted"), gt["gold"])
    return {
        "run_id": run["run_id"], "claim_id": run["claim_id"], "system": run["system"], "batch": run["batch"],
        "scenario": gt["scenario"], "split": gt["split"],
        "truth": t["decision"], "pred": pred or ("ESCALATED" if escalated else None),
        "correct": pred == t["decision"],
        "escalated": escalated,
        "terminal": term,
        "unsafe_approval": paying_pred and not paying_truth,
        "missed_payment": paying_truth and pred is not None and not paying_pred,
        "payout_exact": paying_truth and paying_pred and abs((rec.get("payout_c") or 0) - t["payout_c"]) <= 100,
        "payout_abs_err_c": abs((rec.get("payout_c") or 0) - t["payout_c"]) if paying_truth and paying_pred else None,
        "flags_found": len(planted & flagged), "flags_planted": len(planted),
        "extraction": ext,
        "usd": run["usd"] or 0.0, "tokens": (run["tokens_in"] or 0) + (run["tokens_out"] or 0),
        "llm_calls": run["llm_calls"] or 0, "steps": run["steps"] or 0, "revisions": run["revisions"] or 0,
        "latency_ms": run["latency_ms"] or 0, "ceiling_usd": run["ceiling_usd"],
        "path": run["path"] or [],
    }


def load_runs(batch: str | None = None, systems: list[str] | None = None, include_replays: bool = False) -> list[dict]:
    q = "SELECT * FROM runs WHERE status <> 'running'" + ("" if include_replays else " AND replay_of IS NULL")
    args: list = []
    if batch:
        q += " AND batch = %s"
        args.append(batch)
    if systems:
        q += " AND system = ANY(%s)"
        args.append(systems)
    with db.conn() as c:
        rows = c.execute(q + " ORDER BY started_at", args).fetchall()
    # One run per (system, claim): the latest that did not die on infrastructure (provider outage).
    # Infra failures are re-run, not scored; their count is reported separately.
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["system"], r["claim_id"])
        infra = r["status"] == "error" or (r["terminal_kind"] == "ESCALATED_ERROR"
                                           and "LLMUnavailable" in str((r["final_state"] or {}).get("terminal")))
        if key not in best or not infra:
            best[key] = r | {"_infra": infra}
    return list(best.values())


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def aggregate(scores: list[dict]) -> dict:
    n = len(scores)
    if not n:
        return {"n": 0}
    pay_truth = [s for s in scores if s["truth"] in rules.PAYING]
    nonpay_truth = [s for s in scores if s["truth"] not in rules.PAYING]
    pay_both = [s for s in pay_truth if s["pred"] in rules.PAYING]
    planted = sum(s["flags_planted"] for s in scores)
    ext_fields = defaultdict(list)
    for s in scores:
        for k, v in s["extraction"].items():
            ext_fields[k].append(v)
    lat = [s["latency_ms"] / 1000 for s in scores]
    return {
        "n": n,
        "accuracy": sum(s["correct"] for s in scores) / n,
        "macro_f1": macro_f1(scores),
        "unsafe_approval_rate": sum(s["unsafe_approval"] for s in nonpay_truth) / max(len(nonpay_truth), 1),
        "unsafe_approvals": sum(s["unsafe_approval"] for s in scores),
        "missed_payment_rate": sum(s["missed_payment"] for s in pay_truth) / max(len(pay_truth), 1),
        "payout_exact_rate": sum(s["payout_exact"] for s in pay_both) / max(len(pay_both), 1),
        "flag_recall": sum(s["flags_found"] for s in scores) / planted if planted else None,
        "escalation_rate": sum(s["escalated"] for s in scores) / n,
        "extraction_accuracy": statistics.mean(v for vs in ext_fields.values() for v in vs),
        "extraction_by_field": {k: sum(v) / len(v) for k, v in ext_fields.items()},
        "usd_mean": statistics.mean(s["usd"] for s in scores),
        "usd_p95": pct([s["usd"] for s in scores], 0.95),
        "usd_max": max(s["usd"] for s in scores),
        "tokens_mean": statistics.mean(s["tokens"] for s in scores),
        "llm_calls_mean": statistics.mean(s["llm_calls"] for s in scores),
        "revisions_total": sum(s["revisions"] for s in scores),
        "runs_with_sendback": sum(s["revisions"] > 0 for s in scores),
        "latency_p50_s": pct(lat, 0.5),
        "latency_p95_s": pct(lat, 0.95),
        "ceiling_overruns": sum(1 for s in scores if s["ceiling_usd"] and s["usd"] > s["ceiling_usd"] + 1e-12),
        "max_spend_vs_ceiling": max((s["usd"] / s["ceiling_usd"]) for s in scores if s["ceiling_usd"]),
    }


def macro_f1(scores: list[dict]) -> float:
    labels = sorted({s["truth"] for s in scores})
    f1s = []
    for lab in labels:
        tp = sum(1 for s in scores if s["pred"] == lab and s["truth"] == lab)
        fp = sum(1 for s in scores if s["pred"] == lab and s["truth"] != lab)
        fn = sum(1 for s in scores if s["pred"] != lab and s["truth"] == lab)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * p * r / (p + r) if p + r else 0.0)
    return statistics.mean(f1s)


def derive_no_reviewer(rows: list[dict]) -> list[dict]:
    """The no-reviewer ablation, read out of the full graph's own runs.

    Without the reviewer the graph is extractor -> investigator -> recommend -> human gate: exactly the
    first three nodes of every Casefile run (same prompts, same model, temperature 0). So its answer is
    the first recommendation in each run's checkpoint history, and its cost is those three nodes' calls.
    That makes the ablation paired - same extraction, same findings - and costs no extra API calls."""
    from casefile.runner import checkpoints

    out = []
    with db.conn() as c:
        for r in rows:
            if r["system"] != "casefile" or r.get("_infra"):
                continue
            first = next((s for s in checkpoints(r["run_id"]) if s["last_node"] == "recommend"), None)
            calls = c.execute(
                "SELECT count(*) n, coalesce(sum(usd),0) usd, coalesce(sum(tokens_in),0) ti, "
                "coalesce(sum(tokens_out),0) tout, coalesce(sum(latency_ms),0) ms FROM llm_calls "
                "WHERE run_id=%s AND visit=0 AND node IN ('extractor','investigator','recommend')",
                (r["run_id"],)).fetchone()
            if first is None:  # stopped before any recommendation: same outcome without a reviewer
                values = r["final_state"] or {}
            else:
                values = dict(first["values"], terminal={"kind": "AWAITING_APPROVAL", "detail": "derived"})
            out.append(r | {
                "run_id": r["run_id"] + "#no-reviewer", "system": "casefile_no_reviewer", "final_state": values,
                "status": "awaiting_approval" if first else r["status"],
                "terminal_kind": (values.get("terminal") or {}).get("kind"), "path": values.get("path", []),
                "steps": len(values.get("path", [])), "revisions": 0, "llm_calls": calls["n"],
                "usd": float(calls["usd"]), "tokens_in": calls["ti"], "tokens_out": calls["tout"],
                "latency_ms": int(calls["ms"]),
            })
    return out


def evaluate(batch: str | None = None, include_replays: bool = False, derive_ablation: bool = True) -> dict:
    rows = load_runs(batch, include_replays=include_replays)
    if derive_ablation and not any(r["system"] == "casefile_no_reviewer" for r in rows):
        rows = rows + derive_no_reviewer(rows)
    scores = [score_run(r) for r in rows if not r.get("_infra")]
    by_system: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        by_system[s["system"]].append(s)
    out = {"batch": batch, "systems": {}}
    for sysname, ss in by_system.items():
        per_sc = defaultdict(list)
        for s in ss:
            per_sc[s["scenario"]].append(s)
        out["systems"][sysname] = {
            "overall": aggregate(ss),
            "by_scenario": {sc: {"n": len(v), "accuracy": sum(x["correct"] for x in v) / len(v),
                                 "escalated": sum(x["escalated"] for x in v)} for sc, v in sorted(per_sc.items())},
            "runs": ss,
        }
    return out
