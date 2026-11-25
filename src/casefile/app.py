"""Adjuster UI: claim queue, live run timeline, recommendation, and the human approval gate."""

from __future__ import annotations

import json
import threading
from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from casefile import db, runner
from casefile.agents import findings_brief
from casefile.contracts import Findings
from casefile.paths import CLAIMS_DIR, RESULTS_DIR
from casefile.tracefmt import node_steps

app = FastAPI(title="Casefile")
_running: dict[str, str] = {}  # run_id -> error message if the background run crashed


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return resources.files("casefile").joinpath("web/index.html").read_text()


@app.get("/api/claims")
def claims(split: str | None = None):
    with db.conn() as c:
        rows = c.execute(
            """SELECT cl.claim_id, cl.split, cl.documents, r.run_id, r.system, r.status, r.decision, r.payout_c,
                      r.terminal_kind, r.started_at
               FROM claims cl LEFT JOIN LATERAL (
                   SELECT * FROM runs WHERE runs.claim_id = cl.claim_id AND replay_of IS NULL
                   ORDER BY started_at DESC LIMIT 1) r ON true
               WHERE %s::text IS NULL OR cl.split = %s ORDER BY cl.claim_id""", (split, split)).fetchall()
    return rows


class RunRequest(BaseModel):
    system: str = "casefile"


@app.post("/api/claims/{claim_id}/run")
def start_run(claim_id: str, req: RunRequest):
    if req.system not in runner.SYSTEMS:
        raise HTTPException(400, "unknown system")
    run_id = runner.new_run_id(claim_id, req.system)

    def work():
        try:
            runner.run_claim(claim_id, req.system, batch="ui", run_id=run_id)
        except Exception as e:  # shown in the UI instead of a silent spinner
            _running[run_id] = f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/claims/{claim_id}/runs")
def claim_runs(claim_id: str):
    with db.conn() as c:
        return c.execute("SELECT run_id, system, status, decision, terminal_kind, usd, started_at FROM runs "
                         "WHERE claim_id=%s ORDER BY started_at DESC LIMIT 20", (claim_id,)).fetchall()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    with db.conn() as c:
        r = c.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,)).fetchone()
    if r is None:
        if run_id in _running:
            return {"run_id": run_id, "status": "error", "error": _running[run_id]}
        return {"run_id": run_id, "status": "starting", "steps": []}
    fs = r["final_state"] or {}
    if r["status"] == "running":
        try:
            snap = runner.compiled().get_state({"configurable": {"thread_id": run_id}})
            fs = snap.values or {}
        except Exception:
            fs = {}
    findings = None
    if fs.get("findings"):
        findings = findings_brief(Findings.model_validate(fs["findings"]))
    return {
        "run": {k: r[k] for k in r if k not in ("final_state",)},
        "steps": node_steps(run_id),
        "recommendation": fs.get("recommendation"),
        "findings": findings,
        "extracted": fs.get("extracted"),
        "reviews": fs.get("reviews", []),
        "terminal": fs.get("terminal"),
        "ledger": fs.get("ledger"),
        "approval": fs.get("approval"),
        "error": _running.get(run_id) or r["error"],
    }


class ApprovalRequest(BaseModel):
    action: str
    note: str = ""
    approver: str = "adjuster@demo"


@app.post("/api/runs/{run_id}/approval")
def approval(run_id: str, req: ApprovalRequest):
    if req.action not in ("APPROVE", "REJECT", "RETURN"):
        raise HTTPException(400, "action must be APPROVE, REJECT or RETURN")
    try:
        return runner.approve(run_id, req.action, req.approver, req.note)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/docs/{claim_id}/{doc_id}")
def document(claim_id: str, doc_id: str):
    p = (CLAIMS_DIR / claim_id / f"{doc_id}.pdf").resolve()
    if CLAIMS_DIR.resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/results")
def results():
    out = {}
    for p in sorted(RESULTS_DIR.glob("*.json")):
        out[p.stem] = json.loads(p.read_text())
    return out
