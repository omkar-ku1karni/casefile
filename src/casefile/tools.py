"""Claims-system tools. Each takes a DB connection plus typed arguments and returns plain JSON.

In the Casefile graph these are called by *code* (the investigator always needs the policy, the
valuation, the prior claims and the price check, so letting an LLM decide whether to call them only
adds a failure mode). The single-agent baseline gets the same lookups as LLM-callable tools.
"""

from __future__ import annotations

from datetime import date

import psycopg

from casefile import rules
from casefile.contracts import ExtractedEstimate
from casefile.datagen.world import TODAY


def _norm(s: str | None) -> str:
    return (s or "").strip().upper().replace(" ", "")


def lookup_policy(conn: psycopg.Connection, policy_number: str) -> dict:
    p = conn.execute("SELECT * FROM policies WHERE policy_number=%s", (_norm(policy_number),)).fetchone()
    if p is None:
        return {"found": False, "policy_number": policy_number}
    drivers = [r["driver_name"] for r in conn.execute(
        "SELECT driver_name FROM policy_drivers WHERE policy_number=%s", (p["policy_number"],))]
    covs = [{"code": r["code"], "deductible": r["deductible_c"] / 100} for r in conn.execute(
        "SELECT code, deductible_c FROM policy_coverages WHERE policy_number=%s ORDER BY code", (p["policy_number"],))]
    veh = conn.execute("SELECT vin, year, make, model, vehicle_class FROM vehicles WHERE policy_number=%s",
                       (p["policy_number"],)).fetchone()
    status = "active"
    if p["cancel_date"] and p["cancel_date"] <= TODAY:
        status = "cancelled"
    elif p["expiry_date"] < TODAY:
        status = "expired"
    return {
        "found": True,
        "policy_number": p["policy_number"],
        "state": p["state"],
        "named_insured": p["named_insured"],
        "effective_date": p["effective_date"].isoformat(),
        "expiry_date": p["expiry_date"].isoformat(),
        "cancel_date": p["cancel_date"].isoformat() if p["cancel_date"] else None,
        "cancel_reason": p["cancel_reason"],
        "status_today": status,
        "drivers": drivers,
        "coverages": covs,
        "vehicle": dict(veh) if veh else None,
    }


def vehicle_valuation(conn: psycopg.Connection, vin: str) -> dict:
    v = conn.execute("SELECT * FROM vehicles WHERE vin=%s", (_norm(vin),)).fetchone()
    if v is None:
        return {"found": False, "vin": vin}
    return {"found": True, "vin": v["vin"], "year": v["year"], "make": v["make"], "model": v["model"],
            "vehicle_class": v["vehicle_class"], "acv": v["acv_c"] / 100, "policy_number": v["policy_number"]}


def get_prior_claims(conn: psycopg.Connection, vin: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM prior_claims WHERE vin=%s ORDER BY loss_date", (_norm(vin),)).fetchall()
    return [{"prior_claim_number": r["prior_claim_number"], "loss_date": r["loss_date"].isoformat(),
             "damage_areas": list(r["damage_areas"]), "paid": r["paid_c"] / 100,
             "repair_verified": r["repair_verified"]} for r in rows]


def _catalog(conn: psycopg.Connection) -> dict[str, dict]:
    return {r["part_number"]: dict(r) for r in conn.execute("SELECT * FROM parts_catalog")}


def reference_prices(conn: psycopg.Connection, part_numbers: list[str]) -> list[dict]:
    cat = _catalog(conn)
    out = []
    for pn in part_numbers:
        r = cat.get(_norm(pn))
        out.append({"part_number": pn, "found": r is not None} | ({
            "description": r["description"], "reference_price": r["price_c"] / 100,
            "reference_labor_hours": r["labor_hours"], "reference_paint_hours": r["paint_hours"],
            "areas": list(r["areas"])} if r else {}))
    return out


def state_rates(conn: psycopg.Connection, state: str) -> dict:
    r = conn.execute("SELECT * FROM state_rates WHERE state=%s", (state,)).fetchone()
    return {"state": state, "body_rate": r["body_rate_c"] / 100, "paint_rate": r["paint_rate_c"] / 100,
            "materials_rate": r["materials_rate_c"] / 100, "tax_pct": r["tax_bp"] / 100}


def to_rules(est: ExtractedEstimate) -> tuple[list[rules.Line], rules.EstimateRates]:
    lines = [rules.Line(ln.op, _norm(ln.part_number), ln.description, ln.qty, rules.to_cents(ln.part_price),
                        ln.labor_hours, ln.paint_hours) for ln in est.lines]
    r = rules.EstimateRates(rules.to_cents(est.body_rate), rules.to_cents(est.paint_rate),
                            rules.to_cents(est.materials_rate), rules.to_cents(est.misc),
                            int(round(est.tax_rate_pct * 100)))
    return lines, r


def price_check(conn: psycopg.Connection, estimates: list[dict], state: str) -> dict:
    """Apply claims-manual section 6 to the extracted estimate(s)."""
    refs = {pn: rules.RefPart(pn, r["description"], r["price_c"], r["labor_hours"], r["paint_hours"])
            for pn, r in _catalog(conn).items()}
    cat_areas = {pn: list(r["areas"]) for pn, r in _catalog(conn).items()}
    out = {"estimates": [], "billed_total_c": 0, "approved_total_c": 0, "adjustments": [], "unknown_parts": [],
           "damage_areas": []}
    areas: set[str] = set()
    for e in estimates:
        est = ExtractedEstimate.model_validate(e)
        lines, rates = to_rules(est)
        pc = rules.price_check(lines, rates, refs, state)
        out["estimates"].append({"doc_id": est.doc_id, "billed": pc.billed.as_dict(), "approved": pc.approved.as_dict(),
                                 "printed_total_c": rules.to_cents(est.printed_total)})
        out["billed_total_c"] += pc.billed.total_c
        out["approved_total_c"] += pc.approved.total_c
        out["adjustments"] += [dict(a.__dict__, doc_id=est.doc_id) for a in pc.adjustments]
        out["unknown_parts"] += pc.unknown_parts
        for ln in lines:
            areas.update(cat_areas.get(ln.part_number, []))
    out["damage_areas"] = sorted(areas)
    return out


def record_decision(conn: psycopg.Connection, *, claim_id: str, run_id: str, approval_id: str,
                    recommendation_hash: str, decision: str, payout_c: int) -> dict:
    """Write-back to the claims system. The DB trigger refuses rows without a matching APPROVE."""
    row = conn.execute(
        "INSERT INTO decisions (claim_id, run_id, approval_id, recommendation_hash, decision, payout_c) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING decision_id",
        (claim_id, run_id, approval_id, recommendation_hash, decision, payout_c)).fetchone()
    return {"decision_id": row["decision_id"], "written": True}


def claim_documents(claim_id: str) -> dict[str, dict]:
    import json

    from casefile.paths import CLAIMS_DIR

    return json.loads((CLAIMS_DIR / claim_id / "text.json").read_text())


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days
