"""The corpus is honest: every planted signal is in the documents, and nothing else trips the rules."""

import json
from collections import Counter

from casefile.paths import CLAIMS_DIR, EVAL_DIR, WORLD_DIR

ROWS = [json.loads(x) for x in (EVAL_DIR / "claims.jsonl").read_text().splitlines()]
WORLD = json.loads((WORLD_DIR / "world.json").read_text())
CLAIMS = {c["claim_id"]: c for c in WORLD["claims"]}
EXPECTED = {"S01": "PAY", "S02": "PAY", "S03": "DENY", "S04": "DENY", "S05": "DENY", "S06": "PAY_ADJUSTED",
            "S07": "PAY", "S08": "PAY", "S09": "PAY", "S10": "REFER_SIU", "S11": "REFER_SIU", "S12": "REFER_SIU",
            "S13": "REQUEST_INFO", "S14": "REQUEST_INFO", "S15": "PAY"}


def text(cid, doc):
    t = json.loads((CLAIMS_DIR / cid / "text.json").read_text())
    return "\n".join(t[doc]["pages"]) if doc in t else None


def test_balanced_split():
    c = Counter((r["scenario"], r["split"]) for r in ROWS)
    assert len(ROWS) == 120
    assert all(c[(s, "dev")] == 3 and c[(s, "test")] == 5 for s in EXPECTED)


def test_truth_matches_scenario():
    for r in ROWS:
        assert r["truth"]["decision"] == EXPECTED[r["scenario"]], r["claim_id"]


def test_only_intended_siu_flags():
    for r in ROWS:
        flags = set(r["truth"]["siu_flags"])
        want = {"S10": {"PRIOR_DAMAGE"}, "S11": {"DAMAGE_MISMATCH"}, "S12": {"DATE_CONFLICT"}}.get(r["scenario"], set())
        assert flags == want, (r["claim_id"], r["scenario"], flags)


def test_planted_signals_are_in_the_documents():
    for r in ROWS:
        c, cid = CLAIMS[r["claim_id"]], r["claim_id"]
        fnol = text(cid, "fnol")
        assert c["policy_number"] in fnol and c["fnol_vin"] in fnol
        if r["scenario"] == "S15":
            assert c["fnol_vin"] != c["vin"] and c["vin"] in text(cid, "estimate")
        if r["scenario"] == "S09":
            assert text(cid, "estimate_supplement") is not None
        if r["scenario"] == "S12":
            assert text(cid, "police_report") is not None
        if r["scenario"] == "S14":
            assert set(r["truth"]["missing_documents"])
        for e in c["estimates"]:
            doc = "estimate_supplement" if e["supplement"] else "estimate"
            for ln in e["lines"]:
                assert ln["part_number"] in text(cid, doc)


def test_policy_status_is_not_in_the_documents():
    """Cancellation lives only in the claims system, so S05 cannot be solved by reading the PDFs."""
    for r in ROWS:
        for doc in ("fnol", "police_report", "estimate"):
            t = text(r["claim_id"], doc)
            assert t is None or "cancel" not in t.lower()
