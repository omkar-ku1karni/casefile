from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("CASEFILE_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
WORLD_DIR = DATA / "world"
CLAIMS_DIR = DATA / "claims"
EVAL_DIR = DATA / "eval"
SPEC_DIR = DATA / "spec"
CONFIG_DIR = ROOT / "config"
VAR = ROOT / "var"  # local runtime state: embedded Postgres, traces (gitignored)
TRACES_DIR = VAR / "traces"
RUNS_DIR = ROOT / "runs"  # committed ship-gate artifacts
RESULTS_DIR = ROOT / "eval" / "results"
