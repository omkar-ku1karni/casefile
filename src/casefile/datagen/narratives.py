"""Template prose for claimant and officer narratives.

Prose is generated from the claim's facts with seeded variation (phrasing, filler, typos), so the
corpus is fully reproducible with no LLM and no API key. Facts the scenario hides (e.g. the policy
status) never appear here; facts the scenario plants (e.g. a rear impact for a front-end estimate)
are stated the way a claimant would state them.
"""

from __future__ import annotations

import random

CLAIMANT = {
    "pole": [
        "I was driving {dir} on {road} near {cross} in {city}. Roads were {condition} and when I braked for a turn the car slid and I went into a {object}. Front end hit it pretty hard. Nobody was hurt.",
        "Coming home on {road}, {condition} conditions. Lost traction on the curve by {cross} and hit a {object} with the front of the car. I was able to drive it home slowly.",
        "My car went off {road} in {city} and struck a {object}. It was {condition} out. All the damage is in the front - bumper is pushed in and a light is broken.",
    ],
    "guardrail_side": [
        "I drifted on {road} because it was {condition} and scraped the {side} side along the guardrail for a few seconds before I got it back. Single car, no one else involved.",
        "Was merging onto {road} in {city} and misjudged the lane, scraped the {side} side on a concrete barrier. Just me in the car.",
    ],
    "swerve_animal": [
        "A deer ran out in front of me on {road} near {city}. I swerved hard to miss it and went into the ditch and hit a {object}. I never actually touched the deer, it ran off.",
        "Deer jumped out on {road} at dusk. I jerked the wheel to avoid it and the car went off the road and the front hit a {object}. The deer was not hit.",
    ],
    "mirror_clip": [
        "Backing out of my garage I clipped the {side} side mirror on the door frame. Small damage only, mirror housing is cracked and hanging.",
        "Pulling into a tight spot at the {store} I caught the {side} side mirror on a concrete post. Mirror is broken.",
        "I backed into a post at the {store} and broke a tail light. Minor.",
    ],
    "rear_ended": [
        "I was stopped at the red light at {road} and {cross} in {city} when the car behind me did not stop and hit me from behind. The other driver was {other}. Police came and took a report.",
        "Sitting in traffic on {road}, got rear ended by another car going maybe 25. Rear bumper and trunk are smashed, tail light broken. Other driver ({other}) admitted fault to the officer.",
        "Another vehicle struck the back of my car while I was waiting to turn left onto {cross}. Damage to the rear of my car. Other driver: {other}.",
    ],
    "t_boned": [
        "I was going through the intersection of {road} and {cross} on a green light and a car ran the stop sign and hit my {side} side. Other driver was {other}. Police report was filed.",
        "Other car pulled out from {cross} without stopping and hit the {side} side of my vehicle. Doors are dented in and the mirror is gone. The other driver's name is {other}.",
    ],
    "rear_end_other": [
        "Traffic stopped suddenly on {road} near {cross} and I could not stop in time and hit the car in front of me. Front end damage. The other driver was {other}, police came.",
        "I rear-ended a vehicle that braked hard for a yellow light at {road}/{cross}. My hood and bumper are damaged. Other party: {other}.",
    ],
    "parking_lot": [
        "Came out of the {store} on {road} and found the {side} side of my car smashed in. Somebody hit it while it was parked and drove off. I filed a police report.",
        "My car was parked at the {store} in {city}. When I came back the {side} side doors were caved in and there was paint transfer. No note. Police report made.",
    ],
    "deer_strike": [
        "Driving on {road} outside {city} around dusk a deer jumped out and I hit it head on. Front bumper, grille and lights are damaged.",
        "Hit a deer on {road}. It came out of nowhere, I could not avoid it. The front of the car took the impact.",
    ],
    "hail": [
        "Big hail storm came through {city} while my car was parked outside. Hood and roof are covered in dents.",
        "Severe hailstorm, golf ball sized. Car was in the driveway. Roof, hood and trunk all dented.",
    ],
    "tree_limb": [
        "During the wind storm a large tree limb came down on my car in the driveway. Roof is crushed in and the windshield is cracked.",
        "A branch fell on my parked car on {road} during high winds. Damage to the roof.",
    ],
    "keyed": [
        "Someone keyed my car while it was parked overnight on {road}. Long scratches down the whole {side} side through the paint.",
        "Found deep scratches along the {side} side doors this morning, looks deliberate. Car was parked at the {store}. I reported it to police.",
    ],
    "broken_window": [
        "Someone smashed the {side} front window of my car overnight. Glass everywhere, nothing taken as far as I can tell.",
        "Vandals broke the {side} door window while parked at the {store}. Reported to police.",
    ],
    "catalytic": [
        "Went to start my car and it was extremely loud. Mechanic says the catalytic converter was cut off. It was parked in my driveway overnight.",
        "Catalytic converter stolen from my vehicle overnight while parked at my apartment on {road}.",
    ],
    "windshield": [
        "A rock from a truck hit my windshield on {road}. It started as a chip and now there is a long crack across the driver's view.",
        "Windshield cracked from a stone kicked up on the highway. Crack spread overnight in the cold.",
    ],
}

FILLER = [
    "I have been a Halvorne customer for years and never had a claim like this.",
    "I need the car for work so I would like this handled quickly.",
    "I have pictures on my phone if you need them.",
    "The shop said they could start next week once approved.",
    "My kids were not in the car thank goodness.",
    "Please call me after 5pm, I work days.",
    "I already got an estimate from the body shop.",
    "Honestly this whole thing has been really stressful.",
]

CLAIMANT_LOSS_TYPE = {
    "pole": "Collision", "guardrail_side": "Collision", "swerve_animal": "Animal", "mirror_clip": "Collision",
    "rear_ended": "Collision", "t_boned": "Collision", "rear_end_other": "Collision", "parking_lot": "Collision",
    "deer_strike": "Animal", "hail": "Weather", "tree_limb": "Weather", "keyed": "Vandalism",
    "broken_window": "Vandalism", "catalytic": "Theft", "windshield": "Glass",
}

DAMAGE_WORDS = {
    "front": ["front bumper, grille, headlight", "front end", "hood and front bumper"],
    "rear": ["rear bumper, trunk, tail light", "back end", "rear bumper and tail lights"],
    "left": ["driver side doors and mirror", "driver side", "left side doors"],
    "right": ["passenger side doors and mirror", "passenger side", "right side doors"],
    "roof": ["roof and hood dents", "roof", "roof, hood, trunk lid"],
    "glass": ["windshield", "windshield cracked"],
    "undercarriage": ["catalytic converter missing", "exhaust / catalytic converter"],
}

OFFICER = {
    "pole": "V1 ({v1}) was traveling on {road} when the driver lost control on {condition} pavement and struck a {object}. Single vehicle. No injuries. V1 sustained front-end damage.",
    "guardrail_side": "V1 ({v1}) left the lane on {road} and made contact with the barrier along its {side} side. No injuries. Single vehicle.",
    "swerve_animal": "V1 ({v1}) swerved to avoid a deer on {road}, left the roadway and struck a {object}. Driver states no contact with the animal. V1 front-end damage.",
    "mirror_clip": "Minor property damage, V1 ({v1}) struck a fixed object while parking. No injuries.",
    "rear_ended": "V1 ({v1}) was stopped for a red signal at {road} and {cross}. V2 ({v2}) failed to stop and struck V1 in the rear. No injuries reported. V2 cited for following too closely. V1 rear damage observed.",
    "t_boned": "V2 ({v2}) failed to yield at the stop sign on {cross} and struck V1 ({v1}) on the {side} side. V2 cited for failure to yield. V1 {side} side damage.",
    "rear_end_other": "V1 ({v1}) struck the rear of V2 ({v2}) which was slowing for traffic on {road} near {cross}. V1 front-end damage. V1 driver cited for assured clear distance.",
    "parking_lot": "Hit-skip reported in the lot of the {store} on {road}. V1 ({v1}) was parked unattended and sustained {side} side damage. No suspect information.",
    "keyed": "Complainant reports V1 ({v1}) was scratched (keyed) along the {side} side while parked on {road}. No suspects. Damage photographed.",
    "broken_window": "Complainant reports the {side} front window of V1 ({v1}) was broken while parked. No items reported missing.",
    "catalytic": "Complainant reports the catalytic converter was cut from V1 ({v1}) while parked overnight at the residence. Saw marks observed on exhaust.",
    "deer_strike": "V1 ({v1}) struck a deer on {road}. Animal deceased at scene. V1 front-end damage.",
    "hail": "Weather-related damage reported.",
    "tree_limb": "Tree limb fell on parked V1 ({v1}) during high winds.",
    "windshield": "Road debris struck windshield of V1 ({v1}).",
}


def _typos(rng: random.Random, text: str, rate: float) -> str:
    words = text.split(" ")
    for i, w in enumerate(words):
        if len(w) > 5 and w.isalpha() and rng.random() < rate:
            j = rng.randrange(1, len(w) - 2)
            words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
    return " ".join(words)


def slots(claim: dict) -> dict:
    s = claim["story"]
    side = {"left": "driver", "right": "passenger"}.get(claim["impact_area"], "driver")
    return {
        "road": s["road"], "cross": s["cross"], "city": s["city"], "store": s["store"], "object": s["object"],
        "condition": s["condition"], "other": s["other_driver"], "side": side,
        "dir": random.Random(s["seed"]).choice(["north", "south", "east", "west"]),
    }


def claimant_narrative(claim: dict) -> str:
    rng = random.Random(claim["story"]["seed"])
    sub = claim["subtype"]
    options = CLAIMANT[sub]
    if sub == "mirror_clip":
        options = options[2:] if claim["impact_area"] == "rear" else options[:2]
    text = rng.choice(options).format(**slots(claim))
    extra = rng.sample(FILLER, k=rng.randint(0, 2))
    if extra:
        text = text + " " + " ".join(extra)
    return _typos(rng, text, 0.04)


def damage_description(claim: dict) -> str:
    rng = random.Random(claim["story"]["seed"] + 1)
    return rng.choice(DAMAGE_WORDS[claim["impact_area"]])


def officer_narrative(claim: dict, v1: str, v2: str) -> str:
    return OFFICER[claim["subtype"]].format(**slots(claim), v1=v1, v2=v2)
