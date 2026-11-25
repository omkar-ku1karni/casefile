from casefile.router import END, decide_next


def state(**kw):
    s = {"limits": {"max_steps": 14, "max_revisions": 2}, "step": 0, "path": [], "terminal": None,
         "ledger": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "max_tokens": 60000, "max_usd": 0.02},
         "extracted": None, "findings": None, "recommendation": None, "reviews": [], "reviews_consumed": 0,
         "rework": None, "revisions": {}, "issue_history": [], "reviewer_enabled": True}
    return s | kw


REC = {"hash": "h1"}
ISSUE = {"code": "FIELD_CONFLICT", "target": "extractor", "field": "estimates[estimate]", "detail": "x"}


def review(verdict, issues=(), h="h1"):
    return {"verdict": verdict, "issues": list(issues), "recommendation_hash": h}


def test_pipeline_order():
    assert decide_next(state())[0] == "extractor"
    assert decide_next(state(extracted={}))[0] == "investigator"
    assert decide_next(state(extracted={}, findings={}))[0] == "recommend"
    assert decide_next(state(extracted={}, findings={}, recommendation=REC))[0] == "reviewer"
    s = state(extracted={}, findings={}, recommendation=REC, reviews=[review("ACCEPT")])
    assert decide_next(s)[0] == "human_gate"


def test_reviewer_disabled_goes_to_human():
    s = state(extracted={}, findings={}, recommendation=REC, reviewer_enabled=False)
    assert decide_next(s)[0] == "human_gate"


def test_terminal_ends():
    assert decide_next(state(terminal={"kind": "DECIDED"}))[0] == END


def test_guards_come_first():
    s = state(step=14, rework={"target": "extractor", "issues": []})
    nxt, upd, _ = decide_next(s)
    assert nxt == "escalate" and upd["escalation"]["kind"] == "ESCALATED_STEP_LIMIT"
    led = {"tokens_in": 0, "tokens_out": 0, "usd": 0.02, "max_tokens": 60000, "max_usd": 0.02}
    assert decide_next(state(ledger=led))[1]["escalation"]["kind"] == "ESCALATED_BUDGET"


def test_revise_routes_to_earliest_target_and_invalidates_downstream():
    rec_issue = ISSUE | {"target": "recommend", "code": "DECISION_RULE_VIOLATION", "field": "decision"}
    s = state(extracted={"a": 1}, findings={}, recommendation=REC, reviews=[review("REVISE", [rec_issue, ISSUE])])
    nxt, upd, _ = decide_next(s)
    assert nxt == "extractor"
    assert upd["findings"] is None and upd["recommendation"] is None
    assert upd["rework"]["issues"] == [ISSUE] and upd["revisions"] == {"extractor": 1}
    assert upd["reviews_consumed"] == 1


def test_identical_recommendation_after_rework_is_reviewed_again():
    s = state(extracted={}, findings={}, recommendation=REC, reviews=[review("REVISE", [ISSUE])],
              reviews_consumed=1, revisions={"extractor": 1}, issue_history=["x"])
    assert decide_next(s)[0] == "reviewer"


def test_revision_limit_and_no_progress():
    s = state(extracted={}, findings={}, recommendation=REC, reviews=[review("REVISE", [ISSUE])],
              revisions={"extractor": 2})
    assert decide_next(s)[1]["escalation"]["kind"] == "ESCALATED_REVISION_LIMIT"
    sig = "FIELD_CONFLICT:extractor:estimates[estimate]"
    s = state(extracted={}, findings={}, recommendation=REC, reviews=[review("REVISE", [ISSUE])],
              revisions={"extractor": 1}, issue_history=[sig])
    assert decide_next(s)[1]["escalation"]["kind"] == "ESCALATED_NO_PROGRESS"
