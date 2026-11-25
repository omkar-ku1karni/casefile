"""Tracing: every run writes a local JSONL trace (always) and mirrors it to Langfuse (when keys are set).

Span kinds map onto Langfuse observation types so the Langfuse UI renders the graph properly:
``chain`` (run / supervisor), ``agent`` (specialist nodes), ``generation`` (LLM calls), ``tool``
(claims-system lookups), ``guardrail`` (budget guard, rule checks), ``evaluator`` (reviewer).

The local file is the source of truth for the committed ship-gate artifacts; Langfuse is the
browsable UI. Losing Langfuse (no keys, offline, quota) never breaks a run.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from casefile.paths import TRACES_DIR

log = logging.getLogger("casefile.telemetry")
_lf_lock = threading.Lock()
_lf_client = None
_lf_checked = False
_current_span: ContextVar[str | None] = ContextVar("casefile_span", default=None)


def langfuse():
    """The Langfuse client, or None when LANGFUSE_PUBLIC_KEY/SECRET_KEY are not configured."""
    global _lf_client, _lf_checked
    with _lf_lock:
        if _lf_checked:
            return _lf_client
        _lf_checked = True
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY") \
                and os.environ.get("CASEFILE_LANGFUSE", "1") != "0":
            try:
                from langfuse import Langfuse

                _lf_client = Langfuse(environment=os.environ.get("CASEFILE_ENV", "dev"))
            except Exception as e:  # pragma: no cover - network/config issues must not break runs
                log.warning("Langfuse disabled: %s", e)
        return _lf_client


def trace_id_for(run_id: str) -> str:
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=run_id)


def _clip(v: Any, n: int = 4000) -> Any:
    if v is None:
        return None
    if hasattr(v, "model_dump"):
        v = v.model_dump(mode="json")
    try:
        s = json.dumps(v, default=str)
    except (TypeError, ValueError):
        s = json.dumps(str(v))
    if len(s) <= n:
        return json.loads(s)
    return s[:n] + f"... [{len(s) - n} more chars]"


class SpanHandle:
    def __init__(self, rec: dict, lf_obs):
        self.rec = rec
        self._lf = lf_obs

    @property
    def id(self) -> str:
        return self.rec["span_id"]

    def update(self, *, output=None, metadata=None, usage=None, cost=None, model=None, level=None,
               status=None, input=None):
        if output is not None:
            self.rec["output"] = _clip(output)
        if input is not None:
            self.rec["input"] = _clip(input)
        if metadata:
            self.rec.setdefault("metadata", {}).update(metadata)
        if usage:
            self.rec["usage"] = usage
        if cost is not None:
            self.rec["cost_usd"] = cost
        if model:
            self.rec["model"] = model
        if level:
            self.rec["level"] = level
        if status:
            self.rec["status"] = status
        if self._lf is not None:
            try:
                kw: dict[str, Any] = {}
                if output is not None:
                    kw["output"] = _clip(output, 20000)
                if input is not None:
                    kw["input"] = _clip(input, 20000)
                if metadata:
                    kw["metadata"] = metadata
                if usage:
                    kw["usage_details"] = {"input": usage.get("input", 0), "output": usage.get("output", 0)}
                if cost is not None:
                    kw["cost_details"] = {"total": float(cost)}
                if model:
                    kw["model"] = model
                if level:
                    kw["level"] = level
                if status:
                    kw["status_message"] = status
                self._lf.update(**kw)
            except Exception as e:  # pragma: no cover
                log.debug("langfuse update failed: %s", e)


class Tracer:
    """One per run. Thread-safe append to var/traces/<run_id>.jsonl."""

    def __init__(self, run_id: str, *, claim_id: str, system: str, tags: list[str] | None = None,
                 trace_id: str | None = None):
        self.run_id = run_id
        self.claim_id = claim_id
        self.system = system
        self.tags = tags or []
        self.trace_id = trace_id or trace_id_for(run_id)
        self.root_id: str | None = None
        self.root_lf_id: str | None = None
        self.path = TRACES_DIR / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.lf = langfuse()

    # writing
    def _write(self, rec: dict) -> None:
        with self._lock, open(self.path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    @contextmanager
    def span(self, name: str, kind: str = "span", *, input=None, metadata=None, parent: str | None = None,
             root: bool = False):
        span_id = uuid.uuid4().hex[:16]
        parent_id = parent if parent is not None else _current_span.get()
        rec = {
            "trace_id": self.trace_id, "run_id": self.run_id, "span_id": span_id, "parent_id": parent_id,
            "name": name, "kind": kind, "start": datetime.now(UTC).isoformat(),
            "input": _clip(input), "metadata": metadata or {},
        }
        t0 = time.perf_counter()
        token = _current_span.set(span_id)
        lf_obs = None
        with ExitStack() as stack:
            if self.lf is not None:
                try:
                    kw: dict[str, Any] = dict(name=name, as_type=_lf_type(kind), input=_clip(input, 20000),
                                              metadata=metadata)
                    if root or parent is not None:
                        tc = {"trace_id": self.trace_id}
                        if not root and self.root_lf_id:
                            tc["parent_span_id"] = self.root_lf_id
                        kw["trace_context"] = tc
                    if root:
                        from langfuse import propagate_attributes

                        stack.enter_context(propagate_attributes(
                            session_id=self.claim_id, tags=[self.system, *self.tags],
                            trace_name=f"{self.claim_id} · {self.system}", metadata={"run_id": self.run_id}))
                    lf_obs = stack.enter_context(self.lf.start_as_current_observation(**kw))
                    if root:
                        self.root_lf_id = lf_obs.id
                except Exception as e:  # pragma: no cover
                    log.debug("langfuse span failed: %s", e)
                    lf_obs = None
            if root:
                self.root_id = span_id
            h = SpanHandle(rec, lf_obs)
            try:
                yield h
            except BaseException as e:
                if type(e).__name__ not in ("GraphInterrupt", "GraphBubbleUp"):
                    rec["level"] = "ERROR"
                    rec["status"] = f"{type(e).__name__}: {e}"[:500]
                    if lf_obs is not None:
                        try:
                            lf_obs.update(level="ERROR", status_message=rec["status"])
                        except Exception:  # pragma: no cover
                            pass
                raise
            finally:
                _current_span.reset(token)
                rec["end"] = datetime.now(UTC).isoformat()
                rec["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self._write(rec)

    def event(self, name: str, **metadata) -> None:
        self._write({"trace_id": self.trace_id, "run_id": self.run_id, "span_id": uuid.uuid4().hex[:16],
                     "parent_id": _current_span.get(), "name": name, "kind": "event",
                     "start": datetime.now(UTC).isoformat(), "metadata": metadata})

    def url(self) -> str | None:
        if self.lf is None:
            return None
        try:
            return self.lf.get_trace_url(trace_id=self.trace_id)
        except Exception:  # pragma: no cover
            return None

    def flush(self) -> None:
        if self.lf is not None:
            try:
                self.lf.flush()
            except Exception:  # pragma: no cover
                pass


def _lf_type(kind: str) -> str:
    return kind if kind in ("span", "agent", "tool", "chain", "generation", "guardrail", "evaluator",
                            "retriever", "embedding") else "span"


_tracers: dict[str, Tracer] = {}
_tracers_lock = threading.Lock()


def register(t: Tracer) -> Tracer:
    with _tracers_lock:
        _tracers[t.run_id] = t
    return t


def tracer_for(run_id: str) -> Tracer:
    with _tracers_lock:
        return _tracers[run_id]


def read_trace(run_id: str, base: Path | None = None) -> list[dict]:
    p = (base or TRACES_DIR) / f"{run_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
