"""One structured-output LLM call, three free backends, and the controls around it.

``complete(agent, system, user, Schema)`` returns a validated Pydantic object. Around every call:

1. Budget guard: projected cost (local token estimate x safety + reserved max output, priced for
   the exact model about to be called) is checked against the per-claim ceiling before sending.
   Because output is reserved at its maximum, actual spend can never exceed the ceiling.
2. Cassette: every request/response is stored in ``llm_calls``; in replay mode responses are
   served from a recorded run, keyed by (node, visit, call index) and checked by request hash.
3. Provider chain + circuit breaker: Gemini -> Groq -> Ollama by default; providers without a
   key are skipped; long rate limits put a provider on cooldown so later calls skip it instantly.
4. Repair: invalid output gets exactly one repair round trip (budgeted), then SchemaViolation.
5. Tracing: one ``generation`` span per call with tokens, cost and model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError

from casefile import db
from casefile.config import price_for, settings
from casefile.runtime import BudgetExceeded, CassetteMiss, LLMUnavailable, RunContext, SchemaViolation, current

log = logging.getLogger("casefile.llm")


@dataclass
class Reply:
    text: str
    tokens_in: int
    tokens_out: int
    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class RateLimited(Exception):
    def __init__(self, msg: str, retry_after: float | None):
        super().__init__(msg)
        self.retry_after = retry_after


class ProviderError(Exception):
    pass


# providers
class Gemini:
    name = "gemini"

    def __init__(self):
        self._client = None

    def available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def generate(self, model, system, user, schema, max_tokens, params) -> Reply:
        from google import genai
        from google.genai import errors, types

        if self._client is None:
            self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
                                        http_options=types.HttpOptions(
                                            timeout=REQUEST_TIMEOUT_S * 1000,
                                            # one attempt per call: retries and back-off are ours (traced, bounded)
                                            retry_options=types.HttpRetryOptions(attempts=1)))
        cfg = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            max_output_tokens=max_tokens, temperature=params.get("temperature", 0))
        if params.get("thinking_level"):
            cfg.thinking_config = types.ThinkingConfig(thinking_level=params["thinking_level"].upper())
        elif "thinking_budget" in params:
            cfg.thinking_config = types.ThinkingConfig(thinking_budget=params["thinking_budget"])
        try:
            r = self._client.models.generate_content(model=model, contents=user, config=cfg)
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                msg = str(e)
                m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", msg)
                delay = float(m.group(1)) if m else None
                if "PerDay" in msg:
                    delay = max(delay or 0, 3600.0)  # daily quota: cool down, fail over / re-run later
                elif delay is None or "PerMinute" in msg:
                    delay = min(delay or 30.0, 60.0)  # per-minute window: wait it out
                raise RateLimited(msg[:300], delay) from e
            raise ProviderError(f"gemini {getattr(e, 'code', '')}: {str(e)[:300]}") from e
        except errors.ServerError as e:
            raise ProviderError(f"gemini server: {str(e)[:300]}") from e
        except Exception as e:  # httpx / httpcore transport errors, timeouts: an outage, not an answer
            raise ProviderError(f"gemini connection: {type(e).__name__}: {str(e)[:200]}") from e
        um = r.usage_metadata
        out = (um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        return Reply(r.text or "", um.prompt_token_count or 0, out, self.name, model)


class Groq:
    name = "groq"

    def __init__(self):
        self._client = None

    def available(self) -> bool:
        return bool(os.environ.get("GROQ_API_KEY"))

    def generate(self, model, system, user, schema, max_tokens, params) -> Reply:
        import groq

        if self._client is None:
            self._client = groq.Groq(max_retries=0, timeout=REQUEST_TIMEOUT_S)
        kw = dict(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "output", "schema": schema}},
            max_completion_tokens=max_tokens, temperature=params.get("temperature", 0),
        )
        if "gpt-oss" in model and params.get("reasoning_effort"):
            kw["reasoning_effort"] = params["reasoning_effort"]
        try:
            r = self._client.chat.completions.create(**kw)
        except groq.RateLimitError as e:
            ra = e.response.headers.get("retry-after") if getattr(e, "response", None) is not None else None
            raise RateLimited(str(e)[:300], float(ra) if ra else None) from e
        except groq.APIStatusError as e:
            raise ProviderError(f"groq {e.status_code}: {str(e)[:300]}") from e
        except groq.APIConnectionError as e:
            raise ProviderError(f"groq connection: {e}") from e
        u = r.usage
        return Reply(r.choices[0].message.content or "", u.prompt_tokens, u.completion_tokens, self.name, model)


class Ollama:
    name = "ollama"

    def __init__(self):
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self._up: bool | None = None

    def available(self) -> bool:
        if os.environ.get("CASEFILE_NO_OLLAMA") == "1":
            return False
        if self._up is None:
            try:
                self._up = httpx.get(f"{self.host}/api/tags", timeout=1.5).status_code == 200
            except httpx.HTTPError:
                self._up = False
        return self._up

    def generate(self, model, system, user, schema, max_tokens, params) -> Reply:
        body = {
            "model": model, "stream": False, "format": schema, "think": False, "keep_alive": "2m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": params.get("temperature", 0), "num_predict": max_tokens,
                        "num_ctx": params.get("num_ctx", 16384)},
        }
        try:
            r = httpx.post(f"{self.host}/api/chat", json=body, timeout=600)
        except httpx.HTTPError as e:
            raise ProviderError(f"ollama: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"ollama {r.status_code}: {r.text[:300]}")
        d = r.json()
        return Reply(d["message"]["content"], d.get("prompt_eval_count", 0), d.get("eval_count", 0), self.name, model)


class Fake:
    """Scripted provider for tests and offline demos: ``set_fake(fn)``; fn(agent, system, user, schema_name) -> str."""

    name = "fake"
    script: Callable[[str, str, str, str], str] | None = None

    def available(self) -> bool:
        return self.script is not None

    def generate(self, model, system, user, schema, max_tokens, params) -> Reply:
        assert self.script is not None
        text = self.script(params["_agent"], system, user, params["_schema_name"])
        return Reply(text, _estimate(system + user), _estimate(text), self.name, model)


PROVIDERS = {"gemini": Gemini(), "groq": Groq(), "ollama": Ollama(), "fake": Fake()}


def set_fake(fn: Callable[[str, str, str, str], str] | None) -> None:
    PROVIDERS["fake"].script = fn  # type: ignore[attr-defined]


# circuit breaker
_cool: dict[str, float] = {}
_cool_lock = threading.Lock()
REQUEST_TIMEOUT_S = 90  # no provider call may hang a claim
SHORT_WAIT_S = 65.0  # per-minute windows (Groq TPM/RPM) reset within a minute
MAX_ATTEMPTS = 3
MAX_SHORT_WAITS = 30  # per call: per-minute windows only; ~30 minutes worst case
BACKOFF_S = (4.0, 12.0)  # transient 5xx / "high demand"
COOLDOWN_S = 600.0


def _cooling(key: str) -> bool:
    with _cool_lock:
        return _cool.get(key, 0) > time.time()


def _cool_down(key: str, seconds: float) -> None:
    with _cool_lock:
        _cool[key] = time.time() + seconds
    log.warning("provider %s cooling down for %.0fs", key, seconds)


def chain_for(agent: str) -> list[str]:
    override = os.environ.get("CASEFILE_MODELS")
    if override:
        chain = [m.strip() for m in override.split(",") if m.strip()]
    else:
        models = settings()["models"]
        chain = list(models.get(agent) or models["default"])
    if os.environ.get("CASEFILE_NO_FALLBACK") == "1":
        # Eval mode: one pinned model per agent, so every answer in a batch comes from the same model.
        # Transient failures are retried patiently; a run that still fails is re-run, never re-routed.
        chain = chain[:1]
    return chain


def _attempts() -> tuple[int, tuple[float, ...]]:
    if os.environ.get("CASEFILE_NO_FALLBACK") == "1":
        return 6, (5.0, 15.0, 30.0, 60.0, 90.0)
    return MAX_ATTEMPTS, BACKOFF_S


# token estimate and schema prep
_enc = None


def _estimate(text: str) -> int:
    global _enc
    if _enc is None:
        try:
            import tiktoken

            _enc = tiktoken.get_encoding("o200k_base")
        except Exception:  # offline without the cached BPE file: a conservative char heuristic
            _enc = False
    if _enc:
        return len(_enc.encode(text, disallowed_special=()))
    return int(len(text) / 3.2) + 1


def llm_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema -> a self-contained schema the three providers accept: refs inlined,
    titles/defaults dropped, closed objects, every property listed as required."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {}
            for k, v in node.items():
                if k in ("title", "default", "maxLength", "minLength"):
                    continue
                out[k] = walk(v)
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(raw)


def _cost(tokens_in: float, tokens_out: float, key: str) -> float:
    pin, pout = price_for(key)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


# the call
def complete[T: BaseModel](agent: str, system: str, user: str, schema: type[T], *, max_tokens: int | None = None) -> T:
    ctx = current()
    max_tokens = max_tokens or settings()["max_output_tokens"].get(agent, 1500)
    sch = llm_schema(schema)
    text = _call(ctx, agent, system, user, schema.__name__, sch, max_tokens)
    try:
        return _parse(text, schema)
    except (ValidationError, ValueError) as e:
        err = _short_err(e)
        ctx.tracer.event("schema.repair", agent=agent, error=err)
        fix = (f"{user}\n\n---\nYour previous answer was not valid for the required JSON schema.\n"
               f"Errors:\n{err}\n\nPrevious answer (truncated):\n{text[:4000]}\n\n"
               "Return the complete corrected JSON object only.")
        text2 = _call(ctx, agent, system, fix, schema.__name__, sch, max_tokens)
        try:
            return _parse(text2, schema)
        except (ValidationError, ValueError) as e2:
            raise SchemaViolation(f"{agent}: {_short_err(e2)}") from e2


def _parse[T: BaseModel](text: str, schema: type[T]) -> T:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if not t:
        raise ValueError("empty response")
    return schema.model_validate_json(t)


def _short_err(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "\n".join(f"- {'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:12])
    return str(e)[:600]


def _call(ctx: RunContext, agent: str, system: str, user: str, schema_name: str, schema: dict,
          max_tokens: int) -> str:
    idx = ctx.call_idx
    ctx.call_idx += 1
    req_hash = hashlib.sha256(json.dumps([agent, system, user, schema_name]).encode()).hexdigest()[:16]
    est_in = _estimate(system + user + json.dumps(schema))
    proj_in = int(est_in * settings()["limits"]["estimate_safety"])
    t0 = time.perf_counter()
    meta = {"agent": agent, "node": ctx.node, "visit": ctx.visit, "call_idx": idx, "request_hash": req_hash,
            "est_input_tokens": est_in, "reserved_output_tokens": max_tokens}
    with ctx.tracer.span(f"llm.{agent}", kind="generation",
                         input={"system": system, "user": user}, metadata=meta) as sp:
        if ctx.mode == "replay":
            reply, served = _from_cassette(ctx, idx, req_hash), f"cassette:{ctx.cassette_run}"
            ctx.ledger.check(proj_in + max_tokens, _cost(proj_in, max_tokens, reply.key), f"{ctx.node}#{idx}")
        else:
            reply, served = _from_providers(ctx, agent, system, user, schema, schema_name, max_tokens, proj_in, idx)
        usd = _cost(reply.tokens_in, reply.tokens_out, reply.key)
        ctx.ledger.charge(reply.tokens_in, reply.tokens_out, usd, est_in)
        ms = int((time.perf_counter() - t0) * 1000)
        sp.update(output=reply.text, usage={"input": reply.tokens_in, "output": reply.tokens_out}, cost=usd,
                  model=reply.key, metadata={"served_from": served, "latency_ms": ms,
                                             "ledger_usd": ctx.ledger.usd, "ledger_tokens": ctx.ledger.tokens})
    ctx.conn.execute(
        "INSERT INTO llm_calls (run_id, node, visit, call_idx, provider, model, request_hash, request, response, "
        "tokens_in, tokens_out, est_tokens, usd, latency_ms, served_from) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (ctx.run_id, ctx.node, ctx.visit, idx, reply.provider, reply.model, req_hash,
         db.jsonb({"agent": agent, "system": system, "user": user, "schema": schema_name}),
         db.jsonb({"text": reply.text}), reply.tokens_in, reply.tokens_out, est_in, usd, ms, served))
    return reply.text


def _from_cassette(ctx: RunContext, idx: int, req_hash: str) -> Reply:
    row = ctx.conn.execute(
        "SELECT * FROM llm_calls WHERE run_id=%s AND node=%s AND visit=%s AND call_idx=%s ORDER BY id LIMIT 1",
        (ctx.cassette_run, ctx.node, ctx.visit, idx)).fetchone()
    if row is None:
        raise CassetteMiss(f"no recorded call for {ctx.cassette_run} {ctx.node} visit={ctx.visit} call={idx}")
    if row["request_hash"] != req_hash:
        raise CassetteMiss(f"request drift at {ctx.node} visit={ctx.visit} call={idx}: "
                           f"recorded {row['request_hash']} != now {req_hash}")
    return Reply(row["response"]["text"], row["tokens_in"], row["tokens_out"], row["provider"], row["model"])


def _from_providers(ctx, agent, system, user, schema, schema_name, max_tokens, proj_in, idx) -> tuple[Reply, str]:
    errors = []
    for spec in chain_for(agent):
        prov_name, model = spec.split(":", 1)
        prov = PROVIDERS.get(prov_name)
        if prov is None or not prov.available():
            continue
        key = f"{prov_name}:{model}"
        if _cooling(key):
            errors.append(f"{key}: cooling down")
            continue
        # The ceiling check is priced for the model we are about to call. Raises BudgetExceeded.
        ctx.ledger.check(proj_in + max_tokens, _cost(proj_in, max_tokens, key), f"{ctx.node}#{idx}")
        params = dict(settings()["provider_params"].get(prov_name, {}), _agent=agent, _schema_name=schema_name)
        n_attempts, backoff = _attempts()
        attempt = short_waits = 0
        while attempt < n_attempts:
            try:
                return prov.generate(model, system, user, schema, max_tokens, params), "provider"
            except RateLimited as e:
                # Per-minute token/request windows reset quickly: wait them out without spending an
                # attempt. Daily quotas (long or missing retry hints) put the provider on cooldown.
                if e.retry_after is not None and e.retry_after <= SHORT_WAIT_S and short_waits < MAX_SHORT_WAITS:
                    short_waits += 1
                    ctx.tracer.event("provider.rate_limited.wait", provider=key, seconds=e.retry_after)
                    time.sleep(e.retry_after + 0.5)
                    continue
                _cool_down(key, max(e.retry_after or 0, COOLDOWN_S))
                errors.append(f"{key}: rate limited")
                break
            except ProviderError as e:
                transient = any(t in str(e) for t in ("503", "500", "502", "504", "UNAVAILABLE", "overloaded",
                                                      "connection", "disconnected", "Timeout"))
                ctx.tracer.event("provider.error", provider=key, error=str(e)[:300], transient=transient)
                if transient and attempt < n_attempts - 1:
                    time.sleep(backoff[attempt])
                    attempt += 1
                    continue
                errors.append(f"{key}: {e}")
                break
    raise LLMUnavailable("; ".join(errors) or "no LLM provider configured (set GEMINI_API_KEY or GROQ_API_KEY, "
                                               "or run Ollama)")


__all__ = ["complete", "set_fake", "llm_schema", "BudgetExceeded", "LLMUnavailable", "SchemaViolation"]
