"""Termination is a property of the router, tested for ANY sequence of agent outputs."""

from hypothesis import given, settings
from hypothesis import strategies as st

from casefile.router import END, decide_next

MAX_STEPS, MAX_REVISIONS = 14, 2
TARGETS = ["extractor", "investigator", "recommend"]
issue = st.builds(lambda c, t, f: {"code": c, "target": t, "field": f, "detail": "-"},
                  st.sampled_from(["FIELD_CONFLICT", "MISSED_RED_FLAG", "DECISION_RULE_VIOLATION"]),
                  st.sampled_from(TARGETS), st.sampled_from(["a", "b", "c", None]))
outcome = st.fixed_dictionaries({
    "verdict": st.sampled_from(["ACCEPT", "REVISE"]),
    "issues": st.lists(issue, min_size=1, max_size=3),
    "rec_hash": st.sampled_from(["h1", "h2", "h3"]),
    "usd": st.floats(0, 0.006),
    "budget_error": st.booleans(),
})


def simulate(outcomes, reviewer_enabled=True):
    s = {"limits": {"max_steps": MAX_STEPS, "max_revisions": MAX_REVISIONS}, "step": 0, "path": [],
         "terminal": None, "extracted": None, "findings": None, "recommendation": None, "reviews": [],
         "reviews_consumed": 0, "rework": None, "revisions": {}, "issue_history": [],
         "reviewer_enabled": reviewer_enabled,
         "ledger": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "max_tokens": 60000, "max_usd": 0.02}}
    hops = 0
    i = 0
    while True:
        hops += 1
        assert hops < 200, "router did not terminate"
        nxt, upd, _ = decide_next(s)
        s |= upd
        if nxt == END:
            return s
        if nxt in ("escalate", "human_gate"):
            s["terminal"] = upd.get("escalation") or {"kind": "AWAITING_APPROVAL"}
            s["path"] = s["path"] + [nxt]
            return s
        o = outcomes[i % len(outcomes)]
        i += 1
        s["step"] += 1
        s["path"] = s["path"] + [nxt]
        s["ledger"] = dict(s["ledger"], usd=s["ledger"]["usd"] + o["usd"])
        if o["budget_error"] and i % 5 == 0:  # a node raising BudgetExceeded sets terminal itself
            s["terminal"] = {"kind": "ESCALATED_BUDGET"}
            continue
        if nxt == "extractor":
            s |= {"extracted": {}, "rework": None}
        elif nxt == "investigator":
            s |= {"findings": {}, "rework": None}
        elif nxt == "recommend":
            s |= {"recommendation": {"hash": o["rec_hash"]}, "rework": None}
        elif nxt == "reviewer":
            s["reviews"] = s["reviews"] + [{"verdict": o["verdict"], "issues": o["issues"] if o["verdict"] == "REVISE"
                                            else [], "recommendation_hash": s["recommendation"]["hash"]}]


@settings(max_examples=3000, deadline=None)
@given(st.lists(outcome, min_size=1, max_size=20), st.booleans())
def test_router_always_terminates_within_max_steps(outcomes, reviewer_enabled):
    s = simulate(outcomes, reviewer_enabled)
    assert s["terminal"] is not None
    specialist_steps = sum(1 for n in s["path"] if n not in ("escalate", "human_gate"))
    assert specialist_steps <= MAX_STEPS
    assert sum(s["revisions"].values()) <= MAX_REVISIONS


def test_adversarial_reviewer_hits_revision_limit():
    always = [{"verdict": "REVISE", "issues": [{"code": "MISSED_RED_FLAG", "target": "investigator",
                                                 "field": f"f{k}", "detail": "-"}], "rec_hash": "h1", "usd": 0.0,
               "budget_error": False} for k in range(20)]
    s = simulate(always)
    assert s["terminal"]["kind"] == "ESCALATED_REVISION_LIMIT"
    assert s["path"].count("reviewer") == MAX_REVISIONS + 1
