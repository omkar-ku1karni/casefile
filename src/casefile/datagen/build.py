"""`python -m casefile.datagen.build`: regenerate the world, the claim PDFs and the eval set."""

from __future__ import annotations

import json
import shutil
from datetime import date, time

from casefile.datagen import pdfs
from casefile.datagen.world import build_world
from casefile.paths import CLAIMS_DIR, EVAL_DIR, WORLD_DIR


def _default(o):
    if isinstance(o, (date, time)):
        return o.isoformat()
    raise TypeError(type(o))


def dump(obj, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=_default, sort_keys=False) + "\n")


def gold_extraction(c: dict) -> dict:
    p = c["driver_name"].split()
    return {
        "policy_number": c["policy_number"],
        "vin": c["fnol_vin"],
        "loss_date": c["loss_date"].isoformat(),
        "loss_cause": c["cause"],
        "driver_name": f"{p[0]} {p[-1]}",
        "other_vehicle": c["other_vehicle"],
        "police_report_number": c["police"]["report_number"] if c["police"] else None,
        "police_incident_date": c["police"]["incident_date"].isoformat() if c["police"] else None,
        "n_estimates": len(c["estimates"]),
        "billed_total_c": c["truth"]["billed_total_c"],
    }


def main() -> None:
    world = build_world()
    shops = {s["shop_id"]: s for s in world["shops"]}
    pols = {p["policy_number"]: p for p in world["policies"]}
    if CLAIMS_DIR.exists():
        shutil.rmtree(CLAIMS_DIR)
    rows = []
    for c in sorted(world["claims"], key=lambda c: c["claim_id"]):
        out = CLAIMS_DIR / c["claim_id"]
        docs = pdfs.render_claim(c, pols[c["policy_number"]], shops, out)
        text = {d["doc_id"]: {"type": d["type"], "pages": pdfs.extract_text(out / d["file"])} for d in docs}
        dump(text, out / "text.json")
        c["documents"] = docs
        rows.append({
            "claim_id": c["claim_id"],
            "split": c["split"],
            "scenario": c["scenario"],
            "scenario_name": c["scenario_name"],
            "subtype": c["subtype"],
            "received_date": c["reported_date"].isoformat(),
            "documents": [d["doc_id"] for d in docs],
            "truth": c["truth"],
            "gold": gold_extraction(c),
        })
    dump({k: v for k, v in world.items()}, WORLD_DIR / "world.json")
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVAL_DIR / "claims.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=_default) + "\n")
    n_dev = sum(r["split"] == "dev" for r in rows)
    print(f"world: {len(world['policies'])} policies, {len(world['prior_claims'])} prior claims, "
          f"{len(world['shops'])} shops; claims: {len(rows)} ({n_dev} dev / {len(rows) - n_dev} test)")


if __name__ == "__main__":
    main()
