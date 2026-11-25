"""`casefile` command line."""

from __future__ import annotations

import json
import os
import shutil

import typer

from casefile import config  # noqa: F401  (loads .env)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Casefile: multi-agent claims triage.")
dbapp = typer.Typer(no_args_is_help=True, help="Embedded Postgres + claims-system data.")
app.add_typer(dbapp, name="db")


def _oracle(on: bool) -> None:
    if on:
        from casefile import oracle

        oracle.enable()
        typer.secho("oracle LLM enabled: mechanics demo, not model results", fg="yellow", err=True)


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=1, default=str))


@app.command("build-data")
def build_data():
    """Regenerate the synthetic world, claim PDFs and the eval set (deterministic, seed 20261124)."""
    from casefile.datagen import build

    build.main()


@dbapp.command("init")
def db_init():
    """Create the schema and load the claims-system tables from data/world/world.json."""
    from casefile import db

    db.init_schema()
    _print(db.seed())
    typer.echo(f"DATABASE: {db.dsn()}")


@dbapp.command("reset-runs")
def db_reset_runs(yes: bool = typer.Option(False, "--yes")):
    """Delete all runs, LLM calls, approvals, decisions and LangGraph checkpoints."""
    from casefile import db

    if not yes:
        raise typer.Abort()
    with db.conn() as c:
        c.execute("TRUNCATE runs, llm_calls, tool_calls, decisions, approvals")
        for t in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        c.execute("DROP TABLE IF EXISTS checkpoint_migrations")
    typer.echo("runs cleared")


@dbapp.command("stop")
def db_stop():
    """Stop the embedded Postgres server."""
    import subprocess

    import pgserver

    from casefile.paths import VAR

    pg_ctl = os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin", "pg_ctl")
    subprocess.run([pg_ctl, "-D", str(VAR / "pg"), "stop", "-m", "fast"], check=False)


@app.command()
def doctor():
    """Check keys, providers, model names, Langfuse and the database."""
    from casefile import db, llm
    from casefile.config import settings
    from casefile.telemetry import langfuse

    rows = []
    for name, p in llm.PROVIDERS.items():
        if name == "fake":
            continue
        rows.append((name, "ok" if p.available() else "not configured"))
    for name, st in rows:
        typer.echo(f"provider {name:7s} {st}")
    want = {m for chain in settings()["models"].values() for m in chain}
    if llm.PROVIDERS["gemini"].available():
        try:
            from google import genai

            c = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
            names = {m.name.removeprefix("models/") for m in c.models.list()}
            for m in sorted(x for x in want if x.startswith("gemini:")):
                typer.echo(f"  {m:40s} {'available' if m.split(':', 1)[1] in names else 'NOT FOUND'}")
        except Exception as e:
            typer.echo(f"  gemini list failed: {e}")
    if llm.PROVIDERS["groq"].available():
        try:
            import groq

            names = {m.id for m in groq.Groq().models.list().data}
            for m in sorted(x for x in want if x.startswith("groq:")):
                typer.echo(f"  {m:40s} {'available' if m.split(':', 1)[1] in names else 'NOT FOUND'}")
        except Exception as e:
            typer.echo(f"  groq list failed: {e}")
    lf = langfuse()
    if lf is None:
        typer.echo("langfuse  not configured (traces go to var/traces/*.jsonl only)")
    else:
        try:
            ok = lf.auth_check()
        except Exception as e:
            ok = False
            typer.echo(f"langfuse  auth error: {str(e).splitlines()[-1][:200]}")
        typer.echo(f"langfuse  {'ok' if ok else 'AUTH FAILED (check keys and LANGFUSE_HOST region: EU cloud.langfuse.com / US us.cloud.langfuse.com)'} "
                   f"host={os.environ.get('LANGFUSE_HOST') or os.environ.get('LANGFUSE_BASE_URL')}")
    try:
        with db.conn() as c:
            n = c.execute("SELECT count(*) AS n FROM claims").fetchone()["n"]
        typer.echo(f"database  ok ({n} claims) {db.dsn()}")
    except Exception as e:
        typer.echo(f"database  not ready: {e}  -> run `casefile db init`")


@app.command()
def run(claim_id: str, system: str = typer.Option("casefile", "--system", "-s"),
        oracle: bool = typer.Option(False, help="Use the oracle fake LLM (no keys)."),
        approve: bool = typer.Option(False, help="Approve at the human gate right away (demo)."),
        batch: str | None = None):
    """Run one claim."""
    from casefile import runner

    _oracle(oracle)
    out = runner.run_claim(claim_id, system, batch=batch)
    _print(out)
    if approve and out["status"] == "awaiting_approval":
        _print(runner.approve(out["run_id"], note="approved from CLI"))


@app.command()
def batch(name: str = typer.Argument(..., help="batch label, e.g. test-v1"),
          split: str = typer.Option("dev", help="dev | test | all"),
          system: list[str] = typer.Option(["casefile"], "--system", "-s"),
          limit: int = typer.Option(0, help="max claims (0 = all)"),
          scenario: str | None = None, oracle: bool = False,
          skip_done: bool = typer.Option(True, help="skip claims already run in this batch"),
          max_usd: float | None = typer.Option(None, help="override the per-claim ceiling"),
          shard: str = typer.Option("0/1", help="i/n: this process runs every n-th claim from i")):
    """Run many claims through one or more systems (sequential per process; shard across processes)."""
    from casefile import db, runner
    from casefile.evaluate import truth

    _oracle(oracle)
    claims = [cid for cid, r in sorted(truth().items())
              if (split == "all" or r["split"] == split) and (not scenario or r["scenario"] == scenario)]
    if limit:
        claims = claims[:limit]
    si, sn = (int(x) for x in shard.split("/"))
    claims = claims[si::sn]
    for s in system:
        done = set()
        if skip_done:
            with db.conn() as c:
                done = {r["claim_id"] for r in c.execute(
                    "SELECT claim_id FROM runs WHERE batch=%s AND system=%s AND status NOT IN ('running','error') "
                    "AND coalesce(final_state->'terminal'->>'detail', '') NOT LIKE 'LLMUnavailable%%'", (name, s))}
        infra_streak = 0
        for i, cid in enumerate(claims, 1):
            if cid in done:
                continue
            lim = {"max_usd": max_usd} if max_usd is not None else None
            out = runner.run_claim(cid, s, batch=name, limit_overrides=lim)
            typer.echo(f"[{s} {i}/{len(claims)}] {cid} {out['status']:18s} {out.get('recommended_decision') or '-':13s} "
                       f"{' > '.join(out['path'])}  ${out['usd']:.5f} {out['llm_calls']} calls"
                       + (f"  {(out.get('terminal_detail') or '')[:120]}" if out["status"] in ("escalated", "error") else ""))
            infra = str(out.get("terminal_detail") or "").startswith("LLMUnavailable")
            infra_streak = infra_streak + 1 if infra else 0
            if infra_streak >= 3:
                typer.secho("3 provider failures in a row (quota or outage): stopping. Re-run the same command later; "
                            "finished claims are skipped.", fg="red")
                raise typer.Exit(2)


@app.command()
def remaining(name: str, split: str = "test", system: str = "casefile"):
    """How many claims of a split still lack a scored (non-outage) run in this batch. Prints a number."""
    from casefile import db
    from casefile.evaluate import truth

    want = {cid for cid, r in truth().items() if split == "all" or r["split"] == split}
    with db.conn() as c:
        done = {r["claim_id"] for r in c.execute(
            "SELECT claim_id FROM runs WHERE batch=%s AND system=%s AND status NOT IN ('running','error') "
            "AND coalesce(final_state->'terminal'->>'detail','') NOT LIKE 'LLMUnavailable%%'", (name, system))}
    typer.echo(len(want - done))


@app.command()
def approve(run_id: str, action: str = typer.Option("APPROVE", help="APPROVE | REJECT | RETURN"),
            note: str = "", approver: str = "adjuster@demo"):
    """Act at the human gate for a paused run."""
    from casefile import runner

    _print(runner.approve(run_id, action, approver, note))


@app.command()
def resume(run_id: str, oracle: bool = False):
    """Resume a run from its last checkpoint (e.g. after a crash)."""
    from casefile import runner

    _oracle(oracle)
    _print(runner.resume(run_id))


@app.command("resume-latest")
def resume_latest(batch_name: str, oracle: bool = False):
    """Resume the most recent run of a batch (used by `make resume-demo`)."""
    from casefile import db, runner

    _oracle(oracle)
    with db.conn() as c:
        r = c.execute("SELECT run_id FROM runs WHERE batch=%s ORDER BY started_at DESC LIMIT 1",
                      (batch_name,)).fetchone()
    if r is None:
        raise typer.Exit(f"no runs in batch {batch_name}")
    typer.echo(f"resuming {r['run_id']} from its last checkpoint", err=True)
    _print(runner.resume(r["run_id"]))


@app.command()
def replay(run_id: str, from_step: int | None = typer.Option(None, "--from-step"),
           live: bool = typer.Option(False, help="re-run against live models instead of recordings"),
           oracle: bool = False):
    """Replay a run from a stored snapshot and compare the terminal state with the original."""
    from casefile import runner

    _oracle(oracle)
    _print(runner.replay(run_id, from_step, mode="live" if live else "replay"))


@app.command("replay-study")
def replay_study(batch_name: str, n: int = 10, from_step: int = 2, system: str = "casefile"):
    """Replay N runs of a batch from a snapshot twice: with recordings (must be identical) and live
    (measures model variance). Writes eval/results/<batch>_replay_study.json."""
    from casefile import db, runner
    from casefile.paths import RESULTS_DIR

    with db.conn() as c:
        rows = c.execute("SELECT run_id FROM runs WHERE batch=%s AND system=%s AND replay_of IS NULL "
                         "AND status='awaiting_approval' AND steps > %s ORDER BY claim_id LIMIT %s",
                         (batch_name, system, from_step, n)).fetchall()
    out = []
    for r in rows:
        rec = runner.replay(r["run_id"], from_step, mode="replay")
        live = runner.replay(r["run_id"], from_step, mode="live")
        same_decision = live["original"]["recommended_decision"] == live["replay"]["recommended_decision"]
        out.append({"run_id": r["run_id"], "recorded_identical": rec["identical"], "recorded_fields": rec["fields"],
                    "live_same_decision": same_decision, "live_same_payout": live["fields"]["payout_c"],
                    "live_same_path": live["fields"]["path"], "original": rec["original"],
                    "live": live["replay"]})
        typer.echo(f"{r['run_id']}: recorded identical={rec['identical']}  live same decision={same_decision}")
    summary = {"from_step": from_step, "n": len(out),
               "recorded_identical": sum(o["recorded_identical"] for o in out),
               "live_same_decision": sum(o["live_same_decision"] for o in out),
               "live_same_payout": sum(o["live_same_payout"] for o in out),
               "live_same_path": sum(o["live_same_path"] for o in out), "runs": out}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{batch_name}_replay_study.json").write_text(json.dumps(summary, indent=1, default=str))
    _print({k: v for k, v in summary.items() if k != "runs"})


@app.command()
def checkpoints(run_id: str):
    """List the specialist-boundary snapshots of a run."""
    from casefile import runner

    for c in runner.checkpoints(run_id):
        typer.echo(f"step {c['step']:2d}  after {c['last_node']:13s} {c['checkpoint_id']}")


@app.command()
def trace(run_id: str, mermaid: bool = False):
    """Print a run's node path with per-node cost from its local trace."""
    from casefile.tracefmt import node_table, sequence_diagram

    typer.echo(sequence_diagram(run_id) if mermaid else node_table(run_id))


@app.command()
def graph():
    """Print the compiled LangGraph as Mermaid."""
    from casefile.graph import mermaid

    typer.echo(mermaid())


@app.command("eval")
def eval_cmd(batch_name: str, low_ceiling_batch: str | None = None):
    """Score a batch and write eval/results/<batch>.json and charts."""
    from casefile import report

    res = report.write(batch_name, low_ceiling_batch)
    for s, v in res["systems"].items():
        o = v["overall"]
        typer.echo(f"{s:22s} n={o['n']:3d} acc={o['accuracy']:.3f} unsafe={o['unsafe_approval_rate']:.3f} "
                   f"esc={o['escalation_rate']:.3f} ext={o['extraction_accuracy']:.3f} "
                   f"usd={o['usd_mean']:.5f} p95={o['latency_p95_s']:.1f}s sendbacks={o['runs_with_sendback']}")


@app.command("ceiling-replay")
def ceiling_replay(batch_name: str, max_usd: float, system: str = "casefile"):
    """Replay every recorded run of a batch from the start under a different ceiling (zero API calls)."""
    from casefile import db, runner

    label = f"{batch_name}-ceiling-{max_usd:g}"
    with db.conn() as c:
        rows = c.execute("SELECT run_id FROM runs WHERE batch=%s AND system=%s AND replay_of IS NULL "
                         "AND status NOT IN ('running','error') AND llm_calls > 0 "
                         "AND coalesce(final_state->'terminal'->>'detail','') NOT LIKE 'LLMUnavailable%%' "
                         "ORDER BY claim_id", (batch_name, system)).fetchall()
    for r in rows:
        out = runner.counterfactual(r["run_id"], max_usd, label)
        typer.echo(f"{r['run_id']}: {out['status']:18s} {out.get('terminal_kind') or '':24s} "
                   f"{' > '.join(out['path'])}  ${out['usd']:.5f}" + (f"  {out.get('error')}" if out.get("error") else ""))
    typer.echo(f"batch: {label}")


@app.command("results-md")
def results_md(batch_name: str):
    """Print the results tables for a scored batch as Markdown."""
    from casefile.report import tables_md

    typer.echo(tables_md(batch_name))


@app.command()
def export(batch_name: str, n: int = 30, dest: str = "runs/recorded", system: str = "casefile"):
    """Export ship-gate artifacts (trace, node path, final state, ledger) for N runs of a batch."""
    from casefile import db
    from casefile.paths import ROOT, TRACES_DIR
    from casefile.tracefmt import node_table, sequence_diagram

    out_dir = ROOT / dest
    with db.conn() as c:
        rows = c.execute("SELECT * FROM runs WHERE batch=%s AND system=%s AND replay_of IS NULL ORDER BY claim_id",
                         (batch_name, system)).fetchall()
    rows = sorted(rows, key=lambda r: (-(r["revisions"] or 0), r["claim_id"]))[:n]
    index = []
    for r in sorted(rows, key=lambda r: r["claim_id"]):
        d = out_dir / r["run_id"]
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(TRACES_DIR / f"{r['run_id']}.jsonl", d / "trace.jsonl")
        (d / "path.txt").write_text(" > ".join(r["path"] or []) + "\n")
        (d / "node_table.txt").write_text(node_table(r["run_id"]) + "\n")
        (d / "sequence.mmd").write_text(sequence_diagram(r["run_id"]) + "\n")
        (d / "final_state.json").write_text(json.dumps(r["final_state"], indent=1, default=str))
        (d / "ledger.json").write_text(json.dumps((r["final_state"] or {}).get("ledger"), indent=1))
        index.append({k: r[k] for k in ("run_id", "claim_id", "status", "terminal_kind", "decision", "payout_c",
                                        "steps", "revisions", "llm_calls", "usd", "ceiling_usd", "path", "trace_url")})
    (out_dir / "index.json").write_text(json.dumps(index, indent=1, default=str))
    typer.echo(f"exported {len(index)} runs to {out_dir}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, oracle: bool = False):
    """Start the adjuster UI (queue, run timeline, approval gate)."""
    import uvicorn

    _oracle(oracle)
    from casefile.app import app as web

    uvicorn.run(web, host=host, port=port)


if __name__ == "__main__":
    app()
