"""Postgres access. Uses DATABASE_URL if set (e.g. docker compose), else an embedded Postgres 16
(pgserver) whose data lives in var/pg - no Docker, no Homebrew, nothing to install."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import date
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from casefile.paths import VAR, WORLD_DIR

_lock = threading.Lock()
_dsn: str | None = None


def dsn() -> str:
    global _dsn
    with _lock:
        if _dsn:
            return _dsn
        url = os.environ.get("DATABASE_URL")
        if url:
            _dsn = url
        else:
            import pgserver  # embedded Postgres; the server keeps running between commands

            pgdata = VAR / "pg"
            pgdata.mkdir(parents=True, exist_ok=True)
            srv = pgserver.get_server(str(pgdata), cleanup_mode=None)
            _dsn = srv.get_uri()
        return _dsn


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(dsn(), autocommit=autocommit, row_factory=dict_row, prepare_threshold=None)


@contextmanager
def conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def init_schema() -> None:
    sql = resources.files("casefile").joinpath("schema.sql").read_text()
    with conn() as c:
        c.execute(sql)


def seed() -> dict:
    """Load data/world/world.json into the claims-system tables (replacing what is there)."""
    w = json.loads((WORLD_DIR / "world.json").read_text())
    with conn() as c, c.transaction():
        c.execute("TRUNCATE policies, policy_drivers, policy_coverages, vehicles, prior_claims, parts_catalog, "
                  "state_rates, claims CASCADE")
        for p in w["policies"]:
            c.execute("INSERT INTO policies VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (p["policy_number"], p["state"], p["named_insured"], p["effective_date"], p["expiry_date"],
                       p["cancel_date"], p["cancel_reason"]))
            for d in p["drivers"]:
                c.execute("INSERT INTO policy_drivers VALUES (%s,%s)", (p["policy_number"], d))
            for cov in p["coverages"]:
                c.execute("INSERT INTO policy_coverages VALUES (%s,%s,%s)",
                          (p["policy_number"], cov["code"], cov["deductible_c"]))
            v = p["vehicle"]
            c.execute("INSERT INTO vehicles VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (v["vin"], p["policy_number"], v["year"], v["make"], v["model"], v["vehicle_class"],
                       w["valuations"][v["vin"]]))
        for pc in w["prior_claims"]:
            c.execute("INSERT INTO prior_claims VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (pc["prior_claim_number"], pc["policy_number"], pc["vin"], pc["loss_date"],
                       pc["damage_areas"], pc["paid_c"], pc["repair_verified"]))
        for part in w["catalog"].values():
            c.execute("INSERT INTO parts_catalog VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                      (part["part_number"], part["vehicle_class"], part["code"], part["description"],
                       part["areas"], part["price_c"], part["labor_hours"], part["paint_hours"]))
        for st, r in w["state_rates"].items():
            c.execute("INSERT INTO state_rates VALUES (%s,%s,%s,%s,%s)",
                      (st, r["body_rate"], r["paint_rate"], r["materials_rate"], r["tax_bp"]))
        for cl in w["claims"]:
            c.execute("INSERT INTO claims VALUES (%s,%s,%s,%s)",
                      (cl["claim_id"], cl["split"], cl["reported_date"], [d["doc_id"] for d in cl["documents"]]))
        counts = {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                  for t in ("policies", "vehicles", "prior_claims", "parts_catalog", "claims")}
    return counts


def jsonb(obj) -> Jsonb:
    return Jsonb(obj, dumps=lambda o: json.dumps(o, default=_default))


def _default(o):
    if isinstance(o, date):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(type(o))
