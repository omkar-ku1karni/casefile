import json

import pytest

from casefile import llm
from casefile.contracts import ExtractedClaim, LLMReview
from casefile.runtime import BudgetExceeded, Ledger, RunContext, use


class NullTracer:
    root_id = None
    trace_id = "t"

    def span(self, *a, **k):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            class H:
                id = "x"

                def update(self, **kw):
                    pass
            yield H()
        return cm()

    def event(self, *a, **k):
        pass


class FakeConn:
    closed = False

    def execute(self, *a, **k):
        return self


def ctx(max_usd=1.0, max_tokens=10**9):
    c = RunContext("r", "CLM-0001", "casefile", "live", None, Ledger(max_tokens, max_usd), NullTracer(), node="n")
    c._conn = FakeConn()
    return c


def test_ledger_blocks_before_crossing():
    led = Ledger(max_tokens=1000, max_usd=0.01)
    led.charge(500, 0, 0.004, 500)
    led.check(400, 0.005, "ok")
    with pytest.raises(BudgetExceeded):
        led.check(501, 0.0, "tokens")
    with pytest.raises(BudgetExceeded):
        led.check(10, 0.0061, "usd")


def test_llm_schema_is_closed_and_inlined():
    s = json.dumps(llm.llm_schema(ExtractedClaim))
    assert "$ref" not in s and '"title"' not in s

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object" and "properties" in n:
                assert n["additionalProperties"] is False and set(n["required"]) == set(n["properties"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(llm.llm_schema(ExtractedClaim))


def test_call_is_never_sent_when_projection_exceeds_ceiling(monkeypatch):
    sent = []
    monkeypatch.setenv("CASEFILE_MODELS", "fake:x")
    llm.set_fake(lambda *a: sent.append(1) or json.dumps({"verdict": "ACCEPT", "issues": []}))
    try:
        c = ctx(max_usd=0.00001)
        with use(c), pytest.raises(BudgetExceeded):
            llm.complete("reviewer", "sys", "user", LLMReview)
        assert sent == [] and c.ledger.usd == 0
    finally:
        llm.set_fake(None)


def test_spend_never_exceeds_ceiling(monkeypatch):
    monkeypatch.setenv("CASEFILE_MODELS", "fake:x")
    llm.set_fake(lambda *a: json.dumps({"verdict": "ACCEPT", "issues": []}))
    try:
        c = ctx(max_usd=0.004)
        with use(c):
            with pytest.raises(BudgetExceeded):
                for _ in range(100):
                    llm.complete("reviewer", "sys " * 200, "user " * 500, LLMReview)
        assert 0 < c.ledger.usd <= 0.004
    finally:
        llm.set_fake(None)


def test_invalid_output_gets_one_repair(monkeypatch):
    monkeypatch.setenv("CASEFILE_MODELS", "fake:x")
    answers = iter(["not json", json.dumps({"verdict": "ACCEPT", "issues": []})])
    llm.set_fake(lambda *a: next(answers))
    try:
        c = ctx()
        with use(c):
            out = llm.complete("reviewer", "s", "u", LLMReview)
        assert out.verdict == "ACCEPT" and c.ledger.llm_calls == 2
    finally:
        llm.set_fake(None)


def test_circuit_breaker_skips_cooling_provider(monkeypatch):
    monkeypatch.setenv("CASEFILE_MODELS", "fake:a,fake:b")
    llm._cool_down("fake:a", 60)
    llm.set_fake(lambda *a: json.dumps({"verdict": "ACCEPT", "issues": []}))
    try:
        c = ctx()
        with use(c):
            llm.complete("reviewer", "s", "u", LLMReview)
    finally:
        llm.set_fake(None)
        llm._cool.clear()
