from __future__ import annotations

import hashlib
import json
import os
import subprocess
from functools import lru_cache

import yaml
from dotenv import load_dotenv

from casefile.paths import CONFIG_DIR, ROOT

load_dotenv(ROOT / ".env", override=False)


@lru_cache
def settings() -> dict:
    cfg = yaml.safe_load((CONFIG_DIR / "casefile.yaml").read_text())
    for key in ("max_usd", "max_tokens", "max_steps", "max_revisions"):
        env = os.environ.get(f"CASEFILE_{key.upper()}")
        if env:
            cfg["limits"][key] = type(cfg["limits"][key])(env)
    return cfg


@lru_cache
def pricing() -> dict:
    return yaml.safe_load((CONFIG_DIR / "pricing.yaml").read_text())


def price_for(provider_model: str) -> tuple[float, float]:
    p = pricing()["models"]
    if provider_model in p:
        e = p[provider_model]
    else:
        prov = provider_model.split(":", 1)[0]
        e = p.get(f"{prov}:*", pricing()["default"])
    return float(e["input"]), float(e["output"])


@lru_cache
def config_hash() -> str:
    """Hash of everything that can change a run's behaviour: config, prices, source code and the claims
    manual. Stored on every run, so 'the test set ran on frozen code' is checkable without git."""
    h = hashlib.sha256(json.dumps({"settings": settings(), "pricing": pricing()}, sort_keys=True, default=str).encode())
    for p in sorted((ROOT / "src" / "casefile").rglob("*")):
        if p.suffix in (".py", ".sql") and "__pycache__" not in p.parts:
            h.update(p.relative_to(ROOT).as_posix().encode())
            h.update(p.read_bytes())
    h.update((ROOT / "data" / "spec" / "claims_manual.md").read_bytes())
    return h.hexdigest()[:12]


@lru_cache
def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:
        return None
