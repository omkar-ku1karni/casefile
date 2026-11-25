"""Seeded world spec: policies, vehicles, shops, prior claims and 120 claims with computed ground truth.

Facts come from code. Each claim is built from exactly one scenario, and ground truth is computed by
``rules.decide`` from the *true* facts here - never from the rendered documents and never by an LLM.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from datetime import date, time, timedelta

from casefile import rules
from casefile.datagen.catalog import (
    CITIES,
    FIRST,
    LAST,
    MIDDLE,
    PARTS,
    ROADS,
    SHOP_WORDS_A,
    SHOP_WORDS_B,
    STORES,
    VEHICLE_CLASSES,
    VEHICLES,
    catalog,
    part_number,
)

SEED = 20261124
TODAY = date(2026, 11, 24)
VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"

SCENARIOS = {
    "S01": "clean collision",
    "S02": "clean comprehensive",
    "S03": "below deductible",
    "S04": "no coverage for cause",
    "S05": "policy inactive on loss date",
    "S06": "inflated estimate",
    "S07": "comprehensive-only policy, animal/weather loss",
    "S08": "total loss",
    "S09": "supplement estimate",
    "S10": "unrepaired prior damage overlap",
    "S11": "narrative vs estimate damage mismatch",
    "S12": "FNOL vs police report date conflict",
    "S13": "driver not listed on policy",
    "S14": "required document missing",
    "S15": "VIN typo on FNOL",
}
PER_SCENARIO = 8
DEV_PER_SCENARIO = 3

# subtype: (cause, other_vehicle, area options)
SUBTYPES = {
    "pole": ("collision", False, ["front"]),
    "guardrail_side": ("collision", False, ["left", "right"]),
    "swerve_animal": ("collision", False, ["front"]),
    "mirror_clip": ("collision", False, ["left", "right", "rear"]),
    "rear_ended": ("collision", True, ["rear"]),
    "t_boned": ("collision", True, ["left", "right"]),
    "rear_end_other": ("collision", True, ["front"]),
    "parking_lot": ("collision", True, ["left", "right"]),
    "deer_strike": ("animal", False, ["front"]),
    "hail": ("weather", False, ["roof"]),
    "tree_limb": ("weather", False, ["roof"]),
    "keyed": ("vandalism", False, ["left", "right"]),
    "broken_window": ("vandalism", False, ["left", "right"]),
    "catalytic": ("theft", False, ["undercarriage"]),
    "windshield": ("glass", False, ["glass"]),
}

SIDE_POOLS = {
    "left": {"minor": ["MRL"], "moderate": ["DFL", "MRL", "FDL"], "heavy": ["DFL", "DRL", "MRL", "FDL", "QPL"]},
    "right": {"minor": ["MRR"], "moderate": ["DFR", "MRR", "FDR"], "heavy": ["DFR", "DRR", "MRR", "FDR", "QPR"]},
    "front": {
        "minor": ["FBC"],
        "moderate": ["FBC", "GRL", "HLL", "FDL"],
        "heavy": ["FBC", "GRL", "HLL", "HLR", "HOD", "FDL", "FDR", "RSP", "CND", "RAD"],
    },
    "rear": {
        "minor": ["TLL"],
        "moderate": ["RBC", "TLL", "TLR"],
        "heavy": ["RBC", "TLL", "TLR", "TRK", "RBP", "QPL"],
    },
}


@dataclass
class Ctx:
    rng: random.Random
    cat: dict
    used_vins: set
    used_policy_numbers: set


# small generators
def make_vin(ctx: Ctx, wmi: str, year: int) -> str:
    year_codes = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 2010 = A ...
    while True:
        body = "".join(ctx.rng.choice(VIN_CHARS) for _ in range(5))
        check = ctx.rng.choice("0123456789X")
        yc = year_codes[(year - 2010) % len(year_codes)]
        serial = "".join(ctx.rng.choice("0123456789") for _ in range(6))
        vin = f"{wmi}{body}{check}{yc}{ctx.rng.choice('ABCDEFGH')}{serial}"
        if vin not in ctx.used_vins:
            ctx.used_vins.add(vin)
            return vin


def vin_typo(rng: random.Random, vin: str) -> str:
    pos = rng.randrange(11, 17)  # in the serial: a single wrong digit, like a transcription slip
    choices = [c for c in "0123456789" if c != vin[pos]]
    return vin[:pos] + rng.choice(choices) + vin[pos + 1 :]


def person(rng: random.Random, last: str | None = None) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(MIDDLE)}. {last or rng.choice(LAST)}"


def short_name(full: str) -> str:
    """'Katherine A. Morales' -> 'Katherine Morales'."""
    parts = full.split()
    return f"{parts[0]} {parts[-1]}"


def acv_cents(rng: random.Random, cls: str, year: int) -> int:
    base = VEHICLE_CLASSES[cls][3]
    v = base * (0.85 ** (TODAY.year - year)) * rng.uniform(0.9, 1.1)
    return int(round(v / 50) * 50) * 100


def rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randrange((end - start).days + 1))


# world entities
def make_shops(ctx: Ctx) -> list[dict]:
    shops = []
    names = set()
    for i in range(25):
        state = ["OH", "IN", "MI"][i % 3]
        while True:
            name = f"{ctx.rng.choice(SHOP_WORDS_A)} {ctx.rng.choice(SHOP_WORDS_B)}"
            if name not in names:
                names.add(name)
                break
        s = rules.STATE_RATES[state]
        shops.append({
            "shop_id": f"SH{i + 1:02d}",
            "name": name,
            "state": state,
            "city": ctx.rng.choice(CITIES[state]),
            "layout": "ABC"[i % 3],
            "inflates": i in (4, 11, 19),
            "body_rate_c": int(round(s["body_rate"] * ctx.rng.uniform(0.98, 1.05) / 50) * 50),
            "paint_rate_c": int(round(s["paint_rate"] * ctx.rng.uniform(0.98, 1.05) / 50) * 50),
            "materials_rate_c": int(round(s["materials_rate"] * ctx.rng.uniform(0.98, 1.04) / 50) * 50),
        })
    return shops


def make_policy(ctx: Ctx, state: str, profile: str, *, year_range=(2012, 2025), cls: str | None = None) -> dict:
    rng = ctx.rng
    while True:
        pn = f"HM-{state}-{rng.randrange(100000, 999999)}"
        if pn not in ctx.used_policy_numbers:
            ctx.used_policy_numbers.add(pn)
            break
    options = [v for v in VEHICLES if cls is None or v[2] == cls]
    make, model, vcls, wmi = rng.choice(options)
    year = rng.randint(*year_range)
    last = rng.choice(LAST)
    insured = person(rng, last)
    drivers = [insured]
    for _ in range(rng.choice([0, 0, 1, 1, 2])):
        d = person(rng, last if rng.random() < 0.7 else None)
        if d.split()[0] != insured.split()[0]:
            drivers.append(d)
    coverages = [{"code": "LIAB", "deductible_c": 0}]
    if profile == "full":
        coverages.append({"code": "COLL", "deductible_c": rng.choice([50000, 50000, 100000, 25000])})
    if profile in ("full", "liab_comp"):
        coverages.append({"code": "COMP", "deductible_c": rng.choice([25000, 50000, 50000, 10000])})
    return {
        "policy_number": pn,
        "state": state,
        "named_insured": insured,
        "drivers": drivers,
        "vehicle": {
            "vin": make_vin(ctx, wmi, year), "year": year, "make": make, "model": model, "vehicle_class": vcls,
        },
        "coverages": coverages,
        "effective_date": None,
        "expiry_date": None,
        "cancel_date": None,
        "cancel_reason": None,
    }


def set_term(rng: random.Random, pol: dict, loss: date, mode: str) -> None:
    """mode: active | cancelled_before | expired_before | cancelled_after."""
    term = rng.choice([182, 365])
    if mode == "expired_before":
        expiry = loss - timedelta(days=rng.randint(3, 30))
        eff = expiry - timedelta(days=term)
    else:
        eff = loss - timedelta(days=rng.randint(15, term - 10))
        expiry = eff + timedelta(days=term)
    pol["effective_date"], pol["expiry_date"] = eff, expiry
    if mode == "cancelled_before":
        pol["cancel_date"] = loss - timedelta(days=rng.randint(5, 40))
        pol["cancel_reason"] = "non-payment of premium"
        if pol["cancel_date"] <= eff:
            pol["cancel_date"] = eff + timedelta(days=5)
    elif mode == "cancelled_after":
        pol["cancel_date"] = min(loss + timedelta(days=rng.randint(3, 60)), expiry - timedelta(days=1))
        pol["cancel_reason"] = rng.choice(["insured request", "vehicle sold", "non-payment of premium"])


# estimates
def honest_line(ctx: Ctx, ref: dict, op: str) -> dict:
    r = ctx.rng
    if op == "Refn":
        return {
            "op": "Refn", "part_number": ref["part_number"], "description": ref["description"], "qty": 1,
            "part_price_c": 0, "labor_hours": 0.0,
            "paint_hours": round(max(ref["paint_hours"], 1.0) * r.uniform(0.9, 1.08), 1),
        }
    return {
        "op": "Repl", "part_number": ref["part_number"], "description": ref["description"], "qty": 1,
        "part_price_c": int(round(ref["price_c"] * r.uniform(0.93, 1.07))),
        "labor_hours": round(ref["labor_hours"] * r.uniform(0.9, 1.08), 1),
        "paint_hours": round(ref["paint_hours"] * r.uniform(0.9, 1.08), 1),
    }


def make_estimate(ctx: Ctx, shop: dict, cls: str, codes: list[str], op: str, state: str, est_date: date) -> dict:
    lines = []
    for code in codes:
        ref = ctx.cat[part_number(cls, code)]
        ln = honest_line(ctx, ref, op if ref["paint_hours"] > 0 or op == "Repl" else "Repl")
        if ln["op"] == "Refn" and ref["paint_hours"] == 0:
            continue
        lines.append(ln)
    return {
        "shop_id": shop["shop_id"],
        "estimate_number": f"E{ctx.rng.randrange(10000, 99999)}",
        "estimate_date": est_date,
        "lines": lines,
        "body_rate_c": shop["body_rate_c"],
        "paint_rate_c": shop["paint_rate_c"],
        "materials_rate_c": shop["materials_rate_c"],
        "misc_c": ctx.rng.choice([0, 450, 750, 1250]),
        "tax_bp": rules.STATE_RATES[state]["tax_bp"],
        "supplement": False,
    }


def inflate(ctx: Ctx, est: dict) -> None:
    r = ctx.rng
    repl = [ln for ln in est["lines"] if ln["op"] == "Repl"]
    mode = r.choice(["parts", "hours", "mixed", "rate"])
    if mode == "rate":
        est["body_rate_c"] = int(round(est["body_rate_c"] * r.uniform(1.22, 1.35) / 50) * 50)
        return
    targets = r.sample(repl, k=min(len(repl), r.randint(1, 3)))
    for ln in targets:
        if mode in ("parts", "mixed"):
            ln["part_price_c"] = int(round(ln["part_price_c"] * r.uniform(1.28, 1.6)))
        if mode in ("hours", "mixed") or ln["labor_hours"] < 1.0:
            ln["labor_hours"] = round(max(ln["labor_hours"] * r.uniform(1.4, 1.9), ln["labor_hours"] + 0.5), 1)


def est_lines(est: dict) -> tuple[list[rules.Line], rules.EstimateRates]:
    lines = [rules.Line(**{k: ln[k] for k in ("op", "part_number", "description", "qty", "part_price_c",
                                             "labor_hours", "paint_hours")}) for ln in est["lines"]]
    rates = rules.EstimateRates(est["body_rate_c"], est["paint_rate_c"], est["materials_rate_c"], est["misc_c"],
                                est["tax_bp"])
    return lines, rates


def ref_parts(cat: dict) -> dict[str, rules.RefPart]:
    return {pn: rules.RefPart(pn, p["description"], p["price_c"], p["labor_hours"], p["paint_hours"])
            for pn, p in cat.items()}


# claims
SCENARIO_SUBTYPES = {
    "S01": ["pole", "guardrail_side", "rear_ended", "t_boned", "rear_end_other", "parking_lot"],
    "S02": ["deer_strike", "hail", "tree_limb", "keyed", "broken_window", "catalytic", "windshield"],
    "S03": ["mirror_clip"],
    "S04": ["swerve_animal", "pole", "rear_end_other", "rear_ended"],
    "S05": ["pole", "rear_ended", "deer_strike", "hail", "t_boned"],
    "S06": ["pole", "rear_ended", "t_boned", "deer_strike", "hail", "rear_end_other"],
    "S07": ["deer_strike", "hail", "tree_limb"],
    "S08": ["pole", "rear_end_other", "deer_strike"],
    "S09": ["pole", "deer_strike", "rear_end_other", "rear_ended"],
    "S10": ["pole", "rear_ended", "rear_end_other", "guardrail_side"],
    "S11": ["rear_ended"],
    "S12": ["rear_ended", "t_boned", "keyed", "catalytic"],
    "S13": ["pole", "rear_ended", "t_boned", "guardrail_side"],
    "S14": ["catalytic", "keyed", "rear_ended", "pole"],
    "S15": ["pole", "rear_ended", "t_boned", "rear_end_other"],
}


def build_claim(ctx: Ctx, scenario: str, k: int, shops: list[dict]) -> tuple[dict, dict, list[dict]]:
    """Returns (claim, policy, prior_claims). Retries until the scenario's invariants hold."""
    for _attempt in range(200):
        out = _try_claim(ctx, scenario, k, shops)
        if out is not None:
            return out
    raise RuntimeError(f"could not satisfy scenario {scenario}")


def _try_claim(ctx: Ctx, scenario: str, k: int, shops: list[dict]):
    r = ctx.rng
    subtype = SCENARIO_SUBTYPES[scenario][k % len(SCENARIO_SUBTYPES[scenario])]
    cause, other_vehicle, areas = SUBTYPES[subtype]
    area = r.choice(areas)
    state = r.choice(["OH", "IN", "MI"])
    profile = "full"
    if scenario == "S04":
        profile = "liab_comp" if subtype == "swerve_animal" else r.choice(["liab", "liab_comp"])
    if scenario == "S07":
        profile = "liab_comp"
    year_range, cls = (2012, 2025), None
    if scenario == "S08":
        year_range = (2009, 2013)
        cls = r.choice(["CP", "MS"])
    elif scenario != "S03":
        year_range = (2016, 2025)
    pol = make_policy(ctx, state, profile, year_range=year_range, cls=cls)
    veh = pol["vehicle"]
    loss = rand_date(r, date(2026, 1, 20), date(2026, 11, 8))
    mode = "active"
    if scenario == "S05":
        mode = r.choice(["cancelled_before", "expired_before"])
    elif r.random() < 0.15:
        mode = "cancelled_after"  # hard negative: cancelled, but only after the loss
    set_term(r, pol, loss, mode)

    # ---- damage and estimate(s)
    severity = {"S08": "heavy", "S03": "minor"}.get(scenario) or r.choice(["minor", "moderate", "moderate", "heavy"])
    if subtype == "hail":
        codes = ["HOD", "RFP"] + (["TRK"] if severity != "minor" else [])
    elif subtype == "tree_limb":
        codes = ["RFP"] + (["HOD", "WSH"] if severity != "minor" else ["WSH"])
    elif subtype == "keyed":
        codes = ["DFL", "DRL", "QPL", "FDL"] if area == "left" else ["DFR", "DRR", "QPR", "FDR"]
        codes = codes[: r.randint(2, 4)]
    elif subtype == "broken_window":
        codes = ["SGL"] if area == "left" else ["SGR"]
    elif subtype == "catalytic":
        codes = ["CAT"]
    elif subtype == "windshield":
        codes = ["WSH"]
    elif subtype == "mirror_clip":
        codes = {"left": ["MRL"], "right": ["MRR"], "rear": [r.choice(["TLL", "TLR"])]}[area]
    else:
        pool = SIDE_POOLS[area][severity]
        codes = pool if severity == "heavy" else r.sample(pool, k=r.randint(max(1, len(pool) - 1), len(pool)))
    estimate_area = area
    if scenario == "S11":  # narrative and police say rear impact; the estimate repairs the front
        estimate_area = "front"
        codes = r.sample(SIDE_POOLS["front"]["moderate"], k=3)
    op = "Refn" if subtype == "keyed" else "Repl"

    shop_pool = [s for s in shops if s["state"] == state and s["inflates"] == (scenario == "S06")]
    if not shop_pool:
        shop_pool = [s for s in shops if s["inflates"] == (scenario == "S06")]
    shop = r.choice(shop_pool)
    est_date = loss + timedelta(days=r.randint(1, 6))
    estimates = []
    if scenario != "S14" or subtype in ("catalytic", "keyed", "rear_ended"):
        if scenario == "S09":
            hidden = {"front": ["RSP", "CND"], "rear": ["RBP"]}.get(area, ["RSP"])
            base = [c for c in codes if c not in hidden] or codes[:1]
            estimates.append(make_estimate(ctx, shop, veh["vehicle_class"], base, op, state, est_date))
            sup = make_estimate(ctx, shop, veh["vehicle_class"], hidden, op, state, est_date + timedelta(days=r.randint(4, 9)))
            sup["supplement"] = True
            sup["misc_c"] = 0
            sup["estimate_number"] = estimates[0]["estimate_number"] + "-S1"
            estimates.append(sup)
        else:
            estimates.append(make_estimate(ctx, shop, veh["vehicle_class"], codes, op, state, est_date))
    if scenario == "S06":
        inflate(ctx, estimates[0])

    # ---- police report
    police_needed = "police_report" in rules.required_documents(cause, other_vehicle)
    has_police = police_needed or (cause == "collision" and r.random() < 0.3)
    if scenario == "S14" and subtype in ("catalytic", "keyed", "rear_ended"):
        has_police = False
    if scenario == "S12":
        has_police = True
    loss_time = time(r.randint(6, 22), r.choice([0, 5, 10, 15, 20, 30, 40, 45, 50]))
    police = None
    if has_police:
        inc_date, inc_time = loss, loss_time
        if scenario == "S12":
            inc_date = loss - timedelta(days=r.randint(4, 16))
        elif r.random() < 0.12:  # benign: incident just before midnight, FNOL says the next day
            loss_time = time(0, r.choice([10, 20, 35, 50]))
            inc_date, inc_time = loss - timedelta(days=1), time(23, r.choice([40, 48, 55]))
        police = {
            "report_number": f"{r.choice(['TR', 'IR', 'CR'])}-26-{r.randrange(10000, 99999)}",
            "incident_date": inc_date,
            "incident_time": inc_time,
            "officer": f"Ofc. {r.choice(FIRST)[0]}. {r.choice(LAST)}",
            "badge": str(r.randrange(100, 999)),
        }

    # ---- driver
    driver_full = r.choice(pol["drivers"])
    driver_listed = True
    if scenario == "S13":
        driver_full = person(r)
        while driver_full.split()[-1] in {d.split()[-1] for d in pol["drivers"]}:
            driver_full = person(r)
        driver_listed = False

    # ---- vins
    fnol_vin = vin_typo(r, veh["vin"]) if scenario == "S15" else veh["vin"]

    # ---- prior claims
    priors = []
    acv = acv_cents(r, veh["vehicle_class"], veh["year"])
    damage_areas = sorted({a for c in codes for a in PARTS[c][1]})
    if scenario == "S10":
        priors.append(_prior(ctx, pol, loss - timedelta(days=r.randint(40, 300)), [area], False))
    elif r.random() < 0.35:  # hard negatives
        kind = r.choice(["repaired", "other_area", "old"])
        other = r.choice([a for a in ("front", "rear", "left", "right") if a != area])
        if kind == "repaired":
            priors.append(_prior(ctx, pol, loss - timedelta(days=r.randint(40, 300)), [area], True))
        elif kind == "other_area":
            priors.append(_prior(ctx, pol, loss - timedelta(days=r.randint(40, 300)), [other], False))
        else:
            priors.append(_prior(ctx, pol, loss - timedelta(days=r.randint(420, 900)), [area], False))

    # ---- ground truth
    refs = ref_parts(ctx.cat)
    approved = billed = None
    adjusted = False
    checks = []
    if estimates:
        approved = billed = 0
        for est in estimates:
            lines, rates = est_lines(est)
            pc = rules.price_check(lines, rates, refs, state)
            checks.append(pc)
            approved += pc.approved.total_c
            billed += pc.billed.total_c
            adjusted = adjusted or pc.adjusted
    if adjusted != (scenario == "S06"):
        return None
    cov_code = rules.CAUSE_TO_COVERAGE[cause]
    cov = next((c for c in pol["coverages"] if c["code"] == cov_code), None)
    active = rules.policy_active(loss, pol["effective_date"], pol["expiry_date"], pol["cancel_date"])
    present_docs = ["fnol"] + (["estimate"] if estimates else []) + (["police_report"] if police else [])
    missing = [d for d in rules.required_documents(cause, other_vehicle) if d not in present_docs]
    siu = []
    if any(_overlaps(p, damage_areas, loss) for p in priors):
        siu.append("PRIOR_DAMAGE")
    if scenario == "S11":
        siu.append("DAMAGE_MISMATCH")
    if police and rules.dates_conflict(loss, police["incident_date"]):
        siu.append("DATE_CONFLICT")
    facts = rules.DecisionFacts(
        policy_active=active, coverage_code=cov_code, coverage_present=cov is not None,
        deductible_c=cov["deductible_c"] if cov else 0, missing_documents=missing, siu_flags=siu,
        driver_listed=driver_listed, approved_total_c=approved, adjusted=adjusted, acv_c=acv,
    )
    d = rules.decide(facts)

    # scenario invariants: the decision must be the one the scenario was built to produce
    expected = {
        "S01": "PAY", "S02": "PAY", "S03": "DENY", "S04": "DENY", "S05": "DENY", "S06": "PAY_ADJUSTED",
        "S07": "PAY", "S08": "PAY", "S09": "PAY", "S10": "REFER_SIU", "S11": "REFER_SIU", "S12": "REFER_SIU",
        "S13": "REQUEST_INFO", "S14": "REQUEST_INFO", "S15": "PAY",
    }[scenario]
    if d.decision != expected:
        return None
    intended = {"S10": ["PRIOR_DAMAGE"], "S11": ["DAMAGE_MISMATCH"], "S12": ["DATE_CONFLICT"]}.get(scenario, [])
    if siu != intended:  # exactly the planted signal, nothing accidental
        return None
    if scenario == "S08" and "TOTAL_LOSS" not in d.reason_codes:
        return None
    if scenario != "S08" and "TOTAL_LOSS" in d.reason_codes:
        return None
    if scenario in ("S01", "S02", "S07", "S09", "S15", "S06") and d.payout_c < 15000:
        return None
    if scenario == "S03" and "BELOW_DEDUCTIBLE" not in d.reason_codes:
        return None
    if scenario == "S13" and d.reason_codes != ["DRIVER_VERIFICATION"]:
        return None
    if scenario == "S14" and d.reason_codes != ["MISSING_DOC"]:
        return None

    reported = loss + timedelta(days=r.randint(0, 4))
    story = _story(ctx, subtype, area, state, police is not None)
    claim = {
        "scenario": scenario,
        "scenario_name": SCENARIOS[scenario],
        "subtype": subtype,
        "policy_number": pol["policy_number"],
        "state": state,
        "city": story["city"],
        "cause": cause,
        "other_vehicle": other_vehicle,
        "impact_area": area,
        "estimate_area": estimate_area,
        "damage_areas": damage_areas,
        "loss_date": loss,
        "loss_time": loss_time,
        "reported_date": reported,
        "driver_name": driver_full,
        "driver_listed": driver_listed,
        "fnol_vin": fnol_vin,
        "vin": veh["vin"],
        "police": police,
        "estimates": estimates,
        "acv_c": acv,
        "story": story,
        "truth": {
            "decision": d.decision,
            "reason_codes": d.reason_codes,
            "coverage_code": cov_code,
            "payout_c": d.payout_c,
            "siu_flags": siu,
            "missing_documents": missing,
            "policy_active": active,
            "coverage_present": cov is not None,
            "deductible_c": facts.deductible_c,
            "approved_total_c": approved,
            "billed_total_c": billed,
            "adjusted": adjusted,
            "total_loss": "TOTAL_LOSS" in d.reason_codes,
            "acv_c": acv,
            "adjustments": [a.__dict__ for pc in checks for a in pc.adjustments],
        },
    }
    return claim, pol, priors


def _overlaps(prior: dict, areas: list[str], loss: date) -> bool:
    within = 0 <= (loss - prior["loss_date"]).days <= rules.PRIOR_DAMAGE_WINDOW_DAYS
    return within and not prior["repair_verified"] and bool(set(prior["damage_areas"]) & set(areas))


def _prior(ctx: Ctx, pol: dict, when: date, areas: list[str], repaired: bool) -> dict:
    return {
        "prior_claim_number": f"HMC-{when.year}-{ctx.rng.randrange(10000, 99999)}",
        "policy_number": pol["policy_number"],
        "vin": pol["vehicle"]["vin"],
        "loss_date": when,
        "damage_areas": areas,
        "paid_c": ctx.rng.randrange(800, 4200) * 100,
        "repair_verified": repaired,
    }


def _story(ctx: Ctx, subtype: str, area: str, state: str, police: bool) -> dict:
    r = ctx.rng
    return {
        "city": r.choice(CITIES[state]),
        "road": r.choice(ROADS),
        "cross": r.choice(ROADS),
        "store": r.choice(STORES),
        "other_driver": person(r),
        "side_word": {"left": "driver", "right": "passenger"}.get(area, ""),
        "object": r.choice(["utility pole", "light pole", "concrete barrier", "signpost", "tree"]),
        "condition": r.choice(["wet", "icy", "dark", "foggy", "clear"]),
        "seed": r.randrange(1 << 30),
        "police": police,
    }


def build_world() -> dict:
    rng = random.Random(SEED)
    ctx = Ctx(rng, catalog(), set(), set())
    shops = make_shops(ctx)
    policies, priors, claims = [], [], []
    for sc in SCENARIOS:
        for k in range(PER_SCENARIO):
            claim, pol, pri = build_claim(ctx, sc, k, shops)
            claims.append(claim)
            policies.append(pol)
            priors.extend(pri)
    # background book of business (no claims), with their own prior-claim history
    for _ in range(110):
        state = rng.choice(["OH", "IN", "MI"])
        pol = make_policy(ctx, state, rng.choice(["full", "full", "full", "liab_comp", "liab"]))
        set_term(rng, pol, rand_date(rng, date(2026, 1, 1), date(2026, 11, 1)), "active")
        policies.append(pol)
        if rng.random() < 0.3:
            priors.append(_prior(ctx, pol, rand_date(rng, date(2024, 6, 1), date(2026, 10, 1)),
                                 [rng.choice(["front", "rear", "left", "right"])], rng.random() < 0.6))
    # anonymise order: claim ids don't reveal the scenario
    rng.shuffle(claims)
    for i, c in enumerate(claims, start=1):
        c["claim_id"] = f"CLM-{i:04d}"
    split = _split(rng, claims)
    for c in claims:
        c["split"] = split[c["claim_id"]]
    valuations = {}
    for pol in policies:
        v = pol["vehicle"]
        c = next((c for c in claims if c["vin"] == v["vin"]), None)
        valuations[v["vin"]] = c["acv_c"] if c else acv_cents(rng, v["vehicle_class"], v["year"])
    return {
        "seed": SEED,
        "today": TODAY,
        "insurer": "Halvorne Mutual Insurance Company",
        "catalog": ctx.cat,
        "state_rates": rules.STATE_RATES,
        "shops": shops,
        "policies": policies,
        "prior_claims": priors,
        "valuations": valuations,
        "claims": claims,
    }


def _split(rng: random.Random, claims: list[dict]) -> dict[str, str]:
    out = {}
    by_sc: dict[str, list[dict]] = {}
    for c in claims:
        by_sc.setdefault(c["scenario"], []).append(c)
    for sc in sorted(by_sc):
        group = sorted(by_sc[sc], key=lambda c: c["claim_id"])
        dev = set(c["claim_id"] for c in rng.sample(group, DEV_PER_SCENARIO))
        for c in group:
            out[c["claim_id"]] = "dev" if c["claim_id"] in dev else "test"
    return out


def random_string(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(string.ascii_uppercase) for _ in range(n))
