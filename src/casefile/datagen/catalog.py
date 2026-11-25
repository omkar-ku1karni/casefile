"""Static reference data for the synthetic world: parts catalog, vehicles, names, places."""

from __future__ import annotations

import hashlib

VEHICLE_CLASSES = {
    # class code: (label, price multiplier, hours multiplier, new-vehicle base value in dollars)
    "CP": ("compact", 1.00, 1.00, 24000),
    "MS": ("midsize", 1.15, 1.05, 29000),
    "SU": ("suv", 1.30, 1.15, 33000),
    "TR": ("truck", 1.45, 1.20, 45000),
}

VEHICLES = [
    # make, model, class, WMI prefix
    ("Honda", "Civic", "CP", "2HG"),
    ("Toyota", "Corolla", "CP", "2T1"),
    ("Hyundai", "Elantra", "CP", "5NP"),
    ("Toyota", "Camry", "MS", "4T1"),
    ("Honda", "Accord", "MS", "1HG"),
    ("Nissan", "Altima", "MS", "1N4"),
    ("Ford", "Escape", "SU", "1FM"),
    ("Toyota", "RAV4", "SU", "2T3"),
    ("Chevrolet", "Equinox", "SU", "2GN"),
    ("Ford", "F-150", "TR", "1FT"),
    ("Chevrolet", "Silverado 1500", "TR", "1GC"),
    ("Ram", "1500", "TR", "1C6"),
]

# code: (description, areas, base price $, labor hours, paint hours)
PARTS = {
    "FBC": ("Front bumper cover", ("front",), 420, 2.4, 2.6),
    "GRL": ("Grille assembly", ("front",), 210, 0.6, 0.0),
    "HLL": ("Headlamp assembly LH", ("front",), 380, 0.8, 0.0),
    "HLR": ("Headlamp assembly RH", ("front",), 380, 0.8, 0.0),
    "HOD": ("Hood panel", ("front", "roof"), 540, 1.6, 3.2),
    "FDL": ("Front fender LH", ("front", "left"), 290, 2.1, 2.5),
    "FDR": ("Front fender RH", ("front", "right"), 290, 2.1, 2.5),
    "RSP": ("Radiator support", ("front",), 360, 5.5, 1.5),
    "CND": ("A/C condenser", ("front",), 310, 1.9, 0.0),
    "RAD": ("Radiator", ("front",), 330, 1.7, 0.0),
    "RBC": ("Rear bumper cover", ("rear",), 400, 2.2, 2.6),
    "TLL": ("Tail lamp assembly LH", ("rear",), 240, 0.4, 0.0),
    "TLR": ("Tail lamp assembly RH", ("rear",), 240, 0.4, 0.0),
    "TRK": ("Trunk lid / liftgate", ("rear", "roof"), 610, 1.8, 3.0),
    "RBP": ("Rear body panel", ("rear",), 260, 6.0, 2.2),
    "QPL": ("Quarter panel LH", ("rear", "left"), 690, 10.5, 3.1),
    "QPR": ("Quarter panel RH", ("rear", "right"), 690, 10.5, 3.1),
    "DFL": ("Front door shell LH", ("left",), 760, 4.2, 2.8),
    "DRL": ("Rear door shell LH", ("left",), 720, 4.0, 2.7),
    "DFR": ("Front door shell RH", ("right",), 760, 4.2, 2.8),
    "DRR": ("Rear door shell RH", ("right",), 720, 4.0, 2.7),
    "MRL": ("Outside mirror LH", ("left",), 260, 0.7, 0.5),
    "MRR": ("Outside mirror RH", ("right",), 260, 0.7, 0.5),
    "RFP": ("Roof panel", ("roof",), 880, 14.0, 3.6),
    "WSH": ("Windshield glass", ("glass",), 420, 2.2, 0.0),
    "SGL": ("Front door glass LH", ("glass", "left"), 190, 1.2, 0.0),
    "SGR": ("Front door glass RH", ("glass", "right"), 190, 1.2, 0.0),
    "BKG": ("Back glass", ("glass", "rear"), 360, 2.0, 0.0),
    "CAT": ("Catalytic converter", ("undercarriage",), 1150, 1.8, 0.0),
}

DAMAGE_AREAS = ("front", "rear", "left", "right", "roof", "glass", "undercarriage")


def part_number(cls: str, code: str) -> str:
    n = int(hashlib.sha256(f"{cls}{code}".encode()).hexdigest(), 16) % 900 + 100
    return f"{cls}-{code}-{n}"


def catalog() -> dict[str, dict]:
    """Every part for every vehicle class, keyed by part number."""
    out = {}
    for cls, (_, pm, hm, _) in VEHICLE_CLASSES.items():
        for code, (desc, areas, price, labor, paint) in PARTS.items():
            pn = part_number(cls, code)
            out[pn] = {
                "part_number": pn,
                "vehicle_class": cls,
                "code": code,
                "description": desc,
                "areas": list(areas),
                "price_c": int(round(price * pm * 100)),
                "labor_hours": round(labor * hm, 1),
                "paint_hours": round(paint * hm, 1),
            }
    return out


FIRST = [
    "Katherine", "Marcus", "Priya", "Daniel", "Elena", "Jamal", "Hannah", "Victor", "Grace", "Tomas",
    "Aisha", "Brandon", "Lucia", "Owen", "Mei", "Raymond", "Sofia", "Derek", "Naomi", "Caleb",
    "Rachel", "Andre", "Lauren", "Mateo", "Erin", "Isaac", "Bethany", "Kwame", "Molly", "Trevor",
    "Yasmin", "Gavin", "Leah", "Colin", "Dana", "Felix", "Irene", "Wesley", "Tara", "Hector",
]
LAST = [
    "Morales", "Bennett", "Okafor", "Lindqvist", "Hartley", "Novak", "Castillo", "Whitaker", "Pruitt", "Delgado",
    "Fenwick", "Abernathy", "Kowalski", "Sutton", "Iverson", "Mbeki", "Larkin", "Takahashi", "Ruiz", "Holloway",
    "Brennan", "Szabo", "Patel", "Grummond", "Ostrowski", "Vance", "Kerrigan", "Achterberg", "Nguyen", "Dempsey",
]
MIDDLE = "ABCDEFGHJKLMNPRSTW"

CITIES = {
    "OH": ["Columbus", "Dayton", "Toledo", "Akron", "Mansfield", "Lima", "Zanesville"],
    "IN": ["Fort Wayne", "Bloomington", "Muncie", "Lafayette", "Evansville", "Kokomo"],
    "MI": ["Lansing", "Kalamazoo", "Grand Rapids", "Flint", "Ann Arbor", "Saginaw"],
}
ROADS = [
    "Main St", "Oak Ave", "Route 23", "Broad St", "Maple Rd", "County Rd 12", "Lincoln Hwy", "Cedar Ln",
    "Washington Blvd", "Mill St", "State Route 9", "Riverside Dr", "Elm St", "Market St", "Hillcrest Rd",
]
STORES = ["grocery store", "pharmacy", "hardware store", "gym", "shopping plaza", "community college", "hospital garage"]

SHOP_WORDS_A = ["Ridgeway", "Northgate", "Lakeview", "Precision", "Keystone", "Summit", "Heartland", "Brightline",
                "Cornerstone", "Maplewood", "Ironside", "Bluewater", "Prairie", "Westfield", "Harbor"]
SHOP_WORDS_B = ["Collision", "Auto Body", "Collision Center", "Body & Paint", "Autoworks"]
