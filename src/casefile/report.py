"""Build eval/results/*.json + charts from the runs table. No API calls: everything is recorded."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from casefile.evaluate import evaluate  # noqa: E402
from casefile.paths import RESULTS_DIR  # noqa: E402

# One fixed colour per system so it looks the same in every chart.
SYSTEM_ORDER = ["casefile", "casefile_no_reviewer", "single_agent", "single_prompt"]
SYSTEM_LABEL = {"casefile": "Casefile graph", "casefile_no_reviewer": "Graph, no reviewer",
                "single_agent": "Single agent + tools", "single_prompt": "Single prompt"}
COLOR = {"casefile": "#2a78d6", "casefile_no_reviewer": "#eb6834", "single_agent": "#1baf7a",
         "single_prompt": "#eda100"}
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def cost_chart(res: dict, path, title: str) -> None:
    systems = [s for s in SYSTEM_ORDER if s in res["systems"]]
    fig, ax = plt.subplots(figsize=(8.2, 4.4), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    rng = random.Random(0)
    ceilings = set()
    for i, s in enumerate(systems):
        runs = res["systems"][s]["runs"]
        ys = [r["usd"] * 100 for r in runs]  # cents: easier to read than fractions of a dollar
        xs = [i + rng.uniform(-0.18, 0.18) for _ in ys]
        esc = [r["escalated"] for r in runs]
        ax.scatter([x for x, e in zip(xs, esc) if not e], [y for y, e in zip(ys, esc) if not e], s=22,
                   color=COLOR[s], edgecolors=SURFACE, linewidths=1.2, zorder=3)
        ax.scatter([x for x, e in zip(xs, esc) if e], [y for y, e in zip(ys, esc) if e], s=26, marker="x",
                   color=COLOR[s], linewidths=1.6, zorder=3)
        mean = sum(ys) / len(ys)
        ax.hlines(mean, i - 0.3, i + 0.3, color=INK, linewidth=2, zorder=4)
        ax.annotate(f"mean {mean:.3f}¢", (i + 0.32, mean), color=INK2, fontsize=8, va="center")
        ceilings |= {r["ceiling_usd"] for r in runs if r["ceiling_usd"]}
    for c in ceilings:
        ax.axhline(c * 100, color=INK, linestyle=(0, (4, 3)), linewidth=1.2, zorder=2)
        ax.annotate(f"per-claim ceiling {c * 100:.2f}¢ (enforced before each call)", (len(systems) - 0.5, c * 100),
                    color=INK, fontsize=8, ha="right", va="bottom")
    ax.set_xticks(range(len(systems)), [SYSTEM_LABEL[s] for s in systems], color=INK, fontsize=9)
    ax.set_xlim(-0.6, len(systems) - 0.4)
    ax.set_ylim(bottom=0)
    ax.set_ylabel("cost per claim at list price (US cents)", color=INK2, fontsize=9)
    ax.set_title(title, color=INK, fontsize=11, loc="left", pad=12)
    fig.text(0.01, 0.01, "dots = claims, x = escalated to a human, bar = mean. "
                         "Dollars are tokens x pinned list price.", color=MUTED, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def write(batch: str, low_ceiling_batch: str | None = None) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    res = evaluate(batch)
    slim = {"batch": batch, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "systems": {s: {"overall": v["overall"], "by_scenario": v["by_scenario"]} for s, v in res["systems"].items()}}
    (RESULTS_DIR / f"{batch}.json").write_text(json.dumps(slim, indent=1, default=str))
    (RESULTS_DIR / f"{batch}_runs.jsonl").write_text(
        "\n".join(json.dumps({k: v for k, v in r.items() if k != "extraction"} | {"extraction_correct": sum(
            r["extraction"].values())}, default=str) for v in res["systems"].values() for r in v["runs"]) + "\n")
    cost_chart(res, RESULTS_DIR / f"{batch}_cost_per_claim.png", "Cost per claim, by system")
    if low_ceiling_batch:
        low = evaluate(low_ceiling_batch, include_replays=True, derive_ablation=False)
        (RESULTS_DIR / f"{low_ceiling_batch}.json").write_text(json.dumps(
            {"batch": low_ceiling_batch, "systems": {s: {"overall": v["overall"]} for s, v in low["systems"].items()}},
            indent=1, default=str))
        cost_chart(low, RESULTS_DIR / f"{low_ceiling_batch}_cost_per_claim.png",
                   "Counterfactual: the same recorded test runs replayed under a lower ceiling")
    push_scores(res)
    return slim


def push_scores(res: dict) -> int:
    """Attach eval outcomes to each run's Langfuse trace, so traces can be filtered by correctness."""
    from casefile import db
    from casefile.telemetry import langfuse

    lf = langfuse()
    if lf is None:
        return 0
    with db.conn() as c:
        tid = {r["run_id"]: r["trace_id"] for r in c.execute("SELECT run_id, trace_id FROM runs")}
    n = 0
    for v in res["systems"].values():
        for r in v["runs"]:
            t = tid.get(r["run_id"])
            if not t:
                continue
            lf.create_score(name="decision_correct", value=1.0 if r["correct"] else 0.0, trace_id=t,
                            data_type="BOOLEAN", comment=f"truth={r['truth']} pred={r['pred']}")
            lf.create_score(name="unsafe_approval", value=1.0 if r["unsafe_approval"] else 0.0, trace_id=t,
                            data_type="BOOLEAN")
            lf.create_score(name="scenario", value=r["scenario"], trace_id=t, data_type="CATEGORICAL")
            n += 1
    lf.flush()
    return n


SCENARIO_NAMES = {
    "S01": "clean collision", "S02": "clean comprehensive", "S03": "below deductible", "S04": "no coverage for cause",
    "S05": "policy not in force", "S06": "inflated estimate", "S07": "comp-only policy", "S08": "total loss",
    "S09": "supplement estimate", "S10": "unrepaired prior damage", "S11": "damage mismatch",
    "S12": "date conflict", "S13": "unlisted driver", "S14": "missing document", "S15": "VIN typo",
}


def _pct(x) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def tables_md(batch: str) -> str:
    """Markdown tables straight from eval/results/<batch>.json, so docs never hand-copy a number."""
    res = json.loads((RESULTS_DIR / f"{batch}.json").read_text())
    systems = [s for s in SYSTEM_ORDER if s in res["systems"]]
    o = {s: res["systems"][s]["overall"] for s in systems}
    rows = [
        ("Claims scored", lambda a: str(a["n"])),
        ("Decision accuracy", lambda a: _pct(a["accuracy"])),
        ("Macro-F1 (5 decisions)", lambda a: f"{a['macro_f1']:.2f}"),
        ("Unsafe approvals (pays a claim it shouldn't)", lambda a: f"{a['unsafe_approvals']} ({_pct(a['unsafe_approval_rate'])})"),
        ("Missed payments (holds a claim that should pay)", lambda a: _pct(a["missed_payment_rate"])),
        ("Exact payout, when both say pay", lambda a: _pct(a["payout_exact_rate"])),
        ("SIU-flag recall", lambda a: _pct(a["flag_recall"])),
        ("Escalated to a human", lambda a: _pct(a["escalation_rate"])),
        ("Extraction accuracy (10 fields)", lambda a: _pct(a["extraction_accuracy"])),
        ("LLM calls per claim", lambda a: f"{a['llm_calls_mean']:.1f}"),
        ("Cost per claim at list price (mean / p95)", lambda a: f"${a['usd_mean']:.4f} / ${a['usd_p95']:.4f}"),
        ("Runs with a reviewer send-back", lambda a: str(a["runs_with_sendback"])),
        ("Ceiling overruns", lambda a: str(a["ceiling_overruns"])),
    ]
    out = ["| | " + " | ".join(SYSTEM_LABEL[s] for s in systems) + " |",
           "|---|" + "---|" * len(systems)]
    for label, fn in rows:
        out.append(f"| {label} | " + " | ".join(fn(o[s]) for s in systems) + " |")
    out += ["", "Accuracy by scenario (correct / claims):", "",
            "| Scenario | " + " | ".join(SYSTEM_LABEL[s] for s in systems) + " |",
            "|---|" + "---|" * len(systems)]
    for sc in sorted(SCENARIO_NAMES):
        cells = []
        for s in systems:
            v = res["systems"][s]["by_scenario"].get(sc)
            cells.append("–" if not v else f"{round(v['accuracy'] * v['n'])}/{v['n']}")
        out.append(f"| {sc} {SCENARIO_NAMES[sc]} | " + " | ".join(cells) + " |")
    return "\n".join(out)
