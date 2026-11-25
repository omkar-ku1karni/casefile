"""Render a claim's documents as PDFs with real text layers (fpdf2), then read them back (pdfplumber).

Each PDF carries a footer marking it as synthetic. Date formats and labels vary by document and
by shop layout on purpose, so extraction has to normalise rather than copy.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time
from pathlib import Path

import pdfplumber
from fpdf import FPDF

from casefile import rules
from casefile.datagen import narratives

FOOTER = "SYNTHETIC DOCUMENT - Casefile test corpus. Not a real insurance record."
FIXED_DATE = datetime(2026, 11, 24, tzinfo=UTC)


class Doc(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120)
        self.cell(0, 5, f"{FOOTER}   Page {self.page_no()}", align="C")
        self.set_text_color(0)


def _new() -> Doc:
    pdf = Doc(format="Letter")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()
    return pdf


def _kv(pdf: Doc, pairs: list[tuple[str, str]], col_w: float = 95) -> None:
    pdf.set_font("Helvetica", "", 9)
    for i in range(0, len(pairs), 2):
        for label, value in pairs[i : i + 2]:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(38, 6, label + ":")
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(col_w - 38, 6, value)
        pdf.ln(6)


def _h(pdf: Doc, text: str, size: int = 13) -> None:
    pdf.set_font("Helvetica", "B", size)
    pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")


def _para(pdf: Doc, text: str) -> None:
    pdf.set_font("Helvetica", "", 9.5)
    pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")


def _t(t: time) -> str:
    return t.strftime("%H:%M")


def _ampm(t: time) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def last_first(full: str) -> str:
    p = full.split()
    mid = f" {p[1][0]}" if len(p) == 3 else ""
    return f"{p[-1].upper()}, {p[0].upper()}{mid}"


# documents
def fnol(claim: dict, pol: dict, path: Path) -> None:
    rng = random.Random(claim["story"]["seed"] + 7)
    veh = pol["vehicle"]
    pdf = _new()
    _h(pdf, "HALVORNE MUTUAL INSURANCE COMPANY", 14)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "First Notice of Loss - Auto (Personal Lines)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    driver = claim["driver_name"]
    driver_written = driver if rng.random() < 0.5 else narratives_short(driver)
    pairs = [
        ("Claim no.", claim["claim_id"]),
        ("Date reported", claim["reported_date"].strftime("%m/%d/%Y")),
        ("Policy number", claim["policy_number"]),
        ("Named insured", pol["named_insured"]),
        ("Vehicle", f"{veh['year']} {veh['make']} {veh['model']}"),
        ("VIN", claim["fnol_vin"]),
        ("Driver at time of loss", driver_written),
        ("Date of loss", claim["loss_date"].strftime("%m/%d/%Y")),
        ("Time of loss", _ampm(claim["loss_time"])),
        ("Location", f"{claim['story']['road']}, {claim['city']}, {claim['state']}"),
        ("Type of loss", narratives.CLAIMANT_LOSS_TYPE[claim["subtype"]]),
        ("Other vehicle involved", "Yes" if claim["other_vehicle"] else "No"),
        ("Police report filed", f"Yes - {claim['police']['report_number']}" if claim["police"] else "No"),
        ("Damage (claimant)", narratives.damage_description(claim)),
    ]
    _kv(pdf, pairs)
    pdf.ln(3)
    _h(pdf, "Description of loss (in claimant's words)", 10)
    _para(pdf, narratives.claimant_narrative(claim))
    pdf.ln(3)
    _h(pdf, "Documents submitted with this notice", 10)
    docs = []
    if claim["police"]:
        docs.append("police report")
    if claim["estimates"]:
        docs.append("repair estimate" + ("s" if len(claim["estimates"]) > 1 else ""))
    _para(pdf, ", ".join(docs) if docs else "none - estimate to follow")
    if claim["subtype"] in ("pole", "rear_ended") and not claim["estimates"]:
        _para(pdf, "Claimant states the shop estimate will be sent later.")
    pdf.output(str(path))


def narratives_short(full: str) -> str:
    p = full.split()
    return f"{p[0]} {p[-1]}"


def police_report(claim: dict, pol: dict, path: Path) -> None:
    rng = random.Random(claim["story"]["seed"] + 11)
    pr = claim["police"]
    veh = pol["vehicle"]
    pdf = _new()
    _h(pdf, f"{claim['city'].upper()} POLICE DEPARTMENT (synthetic)", 13)
    pdf.set_font("Helvetica", "", 10)
    kind = "Traffic Crash Report" if claim["cause"] == "collision" else "Incident Report"
    pdf.cell(0, 6, kind, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    fmt = rng.choice(["%Y-%m-%d", "%d %b %Y"])
    _kv(pdf, [
        ("Report no.", pr["report_number"]),
        ("Date of incident", pr["incident_date"].strftime(fmt)),
        ("Time", _t(pr["incident_time"])),
        ("Location", f"{claim['story']['road']} / {claim['story']['cross']}, {claim['city']}, {claim['state']}"),
        ("Reporting officer", f"{pr['officer']} #{pr['badge']}"),
        ("Report date", (pr["incident_date"]).strftime(fmt)),
    ])
    pdf.ln(2)
    _h(pdf, "Unit 1", 10)
    _kv(pdf, [
        ("Driver / owner", last_first(claim["driver_name"])),
        ("Vehicle", f"{veh['year']} {veh['make'].upper()} {veh['model'].upper()}"),
        ("VIN", claim["vin"]),
        ("Insurance", "Halvorne Mutual"),
    ])
    v2 = ""
    if claim["other_vehicle"] and claim["subtype"] != "parking_lot":
        v2 = last_first(claim["story"]["other_driver"])
        pdf.ln(1)
        _h(pdf, "Unit 2", 10)
        _kv(pdf, [("Driver", v2), ("Insurance", "other carrier")])
    pdf.ln(2)
    _h(pdf, "Narrative", 10)
    _para(pdf, narratives.officer_narrative(claim, last_first(claim["driver_name"]), v2 or "unknown"))
    pdf.output(str(path))


LAYOUT_LABELS = {
    "A": {"Repl": "Repl", "Refn": "Refn", "parts": "Parts", "body": "Body labor", "paint": "Paint labor",
          "materials": "Paint materials", "misc": "Misc / hazmat", "tax": "Tax", "total": "GRAND TOTAL",
          "cols": ["Line", "Op", "Description", "Part No.", "Qty", "Price", "Labor", "Paint"]},
    "B": {"Repl": "R&R", "Refn": "Refinish", "parts": "Parts total", "body": "Sheet metal labor",
          "paint": "Refinish labor", "materials": "Paint supplies", "misc": "Shop supplies", "tax": "Sales tax",
          "total": "ESTIMATE TOTAL",
          "cols": ["#", "Operation", "Part Number", "Description", "Qty", "Part Amt", "Body Hrs", "Ref Hrs"]},
    "C": {"Repl": "Replace", "Refn": "Refinish", "parts": "Parts", "body": "Body", "paint": "Paint",
          "materials": "Materials", "misc": "Sublet/misc", "tax": "Tax", "total": "TOTAL DUE",
          "cols": ["Ln", "Oper", "Description", "Part #", "Qty", "Unit $", "B hrs", "P hrs"]},
}


def estimate(claim: dict, pol: dict, est: dict, shop: dict, path: Path) -> None:
    L = LAYOUT_LABELS[shop["layout"]]
    veh = pol["vehicle"]
    lines, rates = _rules_lines(est)
    tot = rules.estimate_totals(lines, rates)
    pdf = _new()
    _h(pdf, shop["name"].upper(), 14)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"{shop['city']}, {shop['state']}  -  Shop ID {shop['shop_id']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    title = "SUPPLEMENT 1" if est["supplement"] else ("ESTIMATE OF RECORD" if shop["layout"] != "B" else "Preliminary Estimate")
    _h(pdf, title, 12)
    info = [
        ("Estimate no.", est["estimate_number"]),
        ("Date", est["estimate_date"].strftime("%B %d, %Y")),
        ("Customer", pol["named_insured"]),
        ("Insurer / claim", f"Halvorne Mutual / {claim['claim_id']}"),
        ("Vehicle", f"{veh['year']} {veh['make']} {veh['model']}"),
        ("VIN", claim["vin"]),
    ]
    if est["supplement"]:
        info.append(("Supplements", est["estimate_number"].split("-")[0]))
        info.append(("Reason", "hidden damage found at teardown"))
    _kv(pdf, info)
    pdf.ln(2)

    def totals_block():
        pdf.set_font("Helvetica", "", 9)
        rows = [
            (L["parts"], rules.fmt_usd(tot.parts_c)),
            (f"{L['body']} {tot.body_hours:.1f} hrs @ {rules.fmt_usd(est['body_rate_c'])}/hr", rules.fmt_usd(tot.body_c)),
            (f"{L['paint']} {tot.paint_hours:.1f} hrs @ {rules.fmt_usd(est['paint_rate_c'])}/hr", rules.fmt_usd(tot.paint_c)),
            (f"{L['materials']} {tot.paint_hours:.1f} hrs @ {rules.fmt_usd(est['materials_rate_c'])}/hr", rules.fmt_usd(tot.materials_c)),
            (L["misc"], rules.fmt_usd(tot.misc_c)),
            (f"{L['tax']} ({est['tax_bp'] / 100:.2f}% on parts and materials)", rules.fmt_usd(tot.tax_c)),
        ]
        for label, val in rows:
            pdf.cell(140, 5.5, label)
            pdf.cell(40, 5.5, val, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(140, 7, L["total"])
        pdf.cell(40, 7, rules.fmt_usd(tot.total_c), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    if shop["layout"] == "C":
        _h(pdf, "Summary", 10)
        totals_block()
    widths = [10, 18, 52, 30, 10, 22, 16, 16]
    pdf.set_font("Helvetica", "B", 8.5)
    cols = L["cols"]
    order = [0, 1, 2, 3, 4, 5, 6, 7] if shop["layout"] != "B" else [0, 1, 3, 2, 4, 5, 6, 7]
    for c, w in zip(cols, widths):
        pdf.cell(w, 6, c, border="B")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8.5)
    for i, ln in enumerate(est["lines"], start=1):
        cells = [
            str(i), L[ln["op"]], ln["description"], ln["part_number"], str(ln["qty"]),
            rules.fmt_usd(ln["part_price_c"]) if ln["op"] == "Repl" else "-",
            f"{ln['labor_hours']:.1f}" if ln["labor_hours"] else "-",
            f"{ln['paint_hours']:.1f}" if ln["paint_hours"] else "-",
        ]
        cells = [cells[j] for j in order]
        for c, w in zip(cells, widths):
            pdf.cell(w, 5.5, c)
        pdf.ln(5.5)
    pdf.ln(3)
    if shop["layout"] != "C":
        totals_block()
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4, "Labor and refinish times per shop guide. Prices subject to change; supplements may follow "
                         "upon teardown. This estimate is not an authorization to repair.")
    pdf.output(str(path))


def _rules_lines(est: dict):
    lines = [rules.Line(ln["op"], ln["part_number"], ln["description"], ln["qty"], ln["part_price_c"],
                        ln["labor_hours"], ln["paint_hours"]) for ln in est["lines"]]
    r = rules.EstimateRates(est["body_rate_c"], est["paint_rate_c"], est["materials_rate_c"], est["misc_c"], est["tax_bp"])
    return lines, r


def render_claim(claim: dict, pol: dict, shops: dict, out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = []
    fnol(claim, pol, out_dir / "fnol.pdf")
    docs.append({"doc_id": "fnol", "type": "fnol", "file": "fnol.pdf"})
    if claim["police"]:
        police_report(claim, pol, out_dir / "police_report.pdf")
        docs.append({"doc_id": "police_report", "type": "police_report", "file": "police_report.pdf"})
    for est in claim["estimates"]:
        name = "estimate_supplement" if est["supplement"] else "estimate"
        estimate(claim, pol, est, shops[est["shop_id"]], out_dir / f"{name}.pdf")
        docs.append({"doc_id": name, "type": "estimate", "file": f"{name}.pdf"})
    return docs


def extract_text(pdf_path: Path) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        return [(p.extract_text() or "").replace(FOOTER, "").strip() for p in pdf.pages]


def _date(d) -> date:
    return d if isinstance(d, date) else date.fromisoformat(d)
