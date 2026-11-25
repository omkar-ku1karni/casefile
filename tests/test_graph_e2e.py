"""End-to-end through LangGraph + Postgres with the oracle LLM: mechanics, not model quality."""

import json

import psycopg
import pytest

from casefile import runner
from casefile.paths import EVAL_DIR

pytestmark = pytest.mark.db
ROWS = [json.loads(x) for x in (EVAL_DIR / "claims.jsonl").read_text().splitlines()]


def first(scenario):
    return next(r["claim_id"] for r in ROWS if r["scenario"] == scenario)


def test_run_pauses_at_human_gate_then_writes_back(seeded_db, oracle):
    out = runner.run_claim(first("S01"), batch="pytest")
    assert out["status"] == "awaiting_approval"
    assert out["path"] == ["extractor", "investigator", "recommend", "reviewer"]
    with seeded_db.conn() as c:
        assert c.execute("SELECT count(*) n FROM decisions WHERE run_id=%s", (out["run_id"],)).fetchone()["n"] == 0
    done = runner.approve(out["run_id"])
    assert done["status"] == "decided" and done["path"][-2:] == ["human_gate", "write_back"]
    with seeded_db.conn() as c:
        assert c.execute("SELECT count(*) n FROM decisions WHERE run_id=%s", (out["run_id"],)).fetchone()["n"] == 1


def test_reject_writes_nothing(seeded_db, oracle):
    out = runner.run_claim(first("S02"), batch="pytest")
    done = runner.approve(out["run_id"], action="REJECT", note="no")
    assert done["terminal_kind"] == "REJECTED_BY_HUMAN"
    with seeded_db.conn() as c:
        assert c.execute("SELECT count(*) n FROM decisions WHERE run_id=%s", (out["run_id"],)).fetchone()["n"] == 0


def test_db_refuses_decision_without_approval(seeded_db):
    with seeded_db.conn() as c, pytest.raises(psycopg.Error):
        c.execute("INSERT INTO decisions (claim_id, run_id, approval_id, recommendation_hash, decision, payout_c) "
                  "VALUES ('CLM-0001','x','missing','h','PAY',1)")


def test_sendback_then_terminates(seeded_db, oracle, monkeypatch):
    monkeypatch.setenv("CASEFILE_FAKE_FAULTS", "drop_supplement,wrong_decision")
    out = runner.run_claim(first("S09"), batch="pytest")
    assert out["status"] == "awaiting_approval" and out["revisions"] == 1
    assert out["path"].count("reviewer") == 2


def test_adversarial_reviewer_escalates(seeded_db, oracle, monkeypatch):
    monkeypatch.setenv("CASEFILE_FAKE_FAULTS", "always_revise")
    out = runner.run_claim(first("S01"), batch="pytest")
    assert out["terminal_kind"] == "ESCALATED_REVISION_LIMIT"
    assert out["steps"] <= 14


def test_replay_from_snapshot_is_identical(seeded_db, oracle, monkeypatch):
    monkeypatch.setenv("CASEFILE_FAKE_FAULTS", "misread_total")
    out = runner.run_claim(first("S06"), batch="pytest")
    monkeypatch.setenv("CASEFILE_FAKE_FAULTS", "")
    rep = runner.replay(out["run_id"], from_step=1)
    assert rep["identical"], rep


def test_budget_ceiling_escalates_without_calling(seeded_db, oracle):
    out = runner.run_claim(first("S01"), batch="pytest", limit_overrides={"max_usd": 0.0005})
    assert out["terminal_kind"] == "ESCALATED_BUDGET" and out["llm_calls"] == 0


def test_baselines_run(seeded_db, oracle):
    for system in ("single_prompt", "single_agent", "casefile_no_reviewer"):
        out = runner.run_claim(first("S07"), system, batch="pytest")
        assert out["status"] == "awaiting_approval", (system, out)
