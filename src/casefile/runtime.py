"""Per-run context: budget ledger, LLM mode, tracer, DB connection, and the typed tool invoker."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

from casefile import db
from casefile.telemetry import Tracer


class BudgetExceeded(Exception):
    """Raised *before* an LLM call whose projected cost would cross the per-claim ceiling."""


class LLMUnavailable(Exception):
    """Every provider in the chain failed or is cooling down."""


class SchemaViolation(Exception):
    """The model's output failed validation, including after the repair round trip."""


class CassetteMiss(Exception):
    """Replay asked for an LLM response that was not recorded, or whose request changed."""


@dataclass
class Ledger:
    max_tokens: int
    max_usd: float
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    llm_calls: int = 0
    blocked: int = 0
    projection_error: list[float] = field(default_factory=list)  # actual/estimated input tokens, per call

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def check(self, projected_tokens: int, projected_usd: float, label: str) -> None:
        if self.tokens + projected_tokens > self.max_tokens:
            self.blocked += 1
            raise BudgetExceeded(
                f"{label}: projected {self.tokens + projected_tokens} tokens > ceiling {self.max_tokens}")
        if self.usd + projected_usd > self.max_usd + 1e-12:
            self.blocked += 1
            raise BudgetExceeded(
                f"{label}: projected ${self.usd + projected_usd:.5f} > ceiling ${self.max_usd:.5f}")

    def charge(self, tokens_in: int, tokens_out: int, usd: float, est_in: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usd = round(self.usd + usd, 8)
        self.llm_calls += 1
        if est_in:
            self.projection_error.append(round(tokens_in / est_in, 3))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Ledger:
        return cls(**d)


@dataclass
class RunContext:
    run_id: str
    claim_id: str
    system: str
    mode: str  # live | replay
    cassette_run: str | None
    ledger: Ledger
    tracer: Tracer
    node: str = "run"
    visit: int = 0
    call_idx: int = 0
    _conn: psycopg.Connection | None = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = db.connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def tool(self, name: str, fn: Callable[..., Any], **args) -> Any:
        """Invoke a claims-system tool: typed args in, JSON-able result out, traced and logged."""
        t0 = time.perf_counter()
        with self.tracer.span(f"tool.{name}", kind="tool", input=args) as sp:
            result = fn(self.conn, **args)
            sp.update(output=result)
        ms = int((time.perf_counter() - t0) * 1000)
        self.conn.execute(
            "INSERT INTO tool_calls (run_id, node, visit, tool, args, result, latency_ms) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (self.run_id, self.node, self.visit, name, db.jsonb(args), db.jsonb(result), ms))
        return result


_ctx: ContextVar[RunContext | None] = ContextVar("casefile_run_ctx", default=None)


def current() -> RunContext:
    c = _ctx.get()
    if c is None:
        raise RuntimeError("no active RunContext")
    return c


@contextmanager
def use(ctx: RunContext):
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)
