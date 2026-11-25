# Halvorne Mutual — Auto Physical Damage Claims Manual (excerpt)

Synthetic. Used by the Casefile agents and implemented identically in `src/casefile/rules.py`.

## 1. Coverage by cause of loss
| Cause of loss | Coverage |
|---|---|
| Collision with a vehicle or object, including rollover and single-vehicle accidents | **COLL** (Collision) |
| Contact with an animal (e.g. striking a deer) | **COMP** (Comprehensive) |
| Weather: hail, wind, falling objects such as tree limbs | **COMP** |
| Theft of the vehicle or its parts (e.g. catalytic converter) | **COMP** |
| Vandalism or malicious mischief, including broken glass and keyed paint | **COMP** |
| Glass breakage from road debris | **COMP** |

*Swerving to avoid an animal and striking an object, without contact with the animal, is a COLLISION loss.*
The claimant's own "type of loss" label does not decide coverage; the facts of the loss do.

If the policy does not carry the coverage that the cause of loss requires, the claim is denied (`NO_COVERAGE`).
Liability (LIAB) never pays for damage to the insured's own vehicle.

## 2. Policy in force
The policy must be in force on the **date of loss stated on the FNOL**: effective date ≤ loss date ≤ expiry
date, and if the policy was cancelled, the loss date must be **before** the cancellation date. A policy that was
cancelled *after* the loss still covers the loss. Otherwise deny (`POLICY_INACTIVE`).

## 3. Required documents
- Always: the FNOL and a repair estimate.
- A police report is also required for theft, vandalism, and any collision involving another vehicle.
- Missing a required document → `REQUEST_INFO` (`MISSING_DOC`).

## 4. Special Investigations Unit (SIU) referral triggers
Refer the claim (`REFER_SIU`) when any of these holds:
- `PRIOR_DAMAGE`: a prior claim on the **same VIN** within 365 days of this loss, paid, with **no verified repair**,
  covering an overlapping damage area (front, rear, left, right, roof, glass, undercarriage).
  Prior claims that were repaired, are older than 365 days, or cover other areas are not a trigger.
- `DAMAGE_MISMATCH`: the damage being repaired on the estimate is inconsistent with how the loss is described
  (e.g. the claimant and police describe a rear impact but the estimate repairs only the front end).
  Damage that spreads to adjacent panels on the impacted side (a side scrape reaching the fender or quarter
  panel, a front impact reaching the hood and fenders) and hidden damage behind the impact are consistent.
- `DATE_CONFLICT`: the loss date on the FNOL and the incident date on the police report differ by **more than one
  day**. A one-day difference around midnight is normal and is not a trigger.
- `VIN_MISMATCH`: the vehicle in the documents is not the vehicle on the policy. A single mistyped character on
  the FNOL when the estimate and police report match the policy vehicle is a clerical error, not a mismatch.

## 5. Driver
The driver at the time of loss must be a listed driver on the policy (minor name formatting differences such as a
missing middle initial are fine). Otherwise → `REQUEST_INFO` (`DRIVER_VERIFICATION`).

## 6. Estimate review
Each estimate line is compared with the reference catalog for the vehicle class:
- Part price more than **15%** above the reference price → approve the reference price.
- Labor or paint hours more than **15%** above the reference hours → approve the reference hours.
- Body, paint or materials hourly rate more than **10%** above the state rate → approve the state rate.
- Tax is the state rate applied to parts and paint materials.
- Supplements are added to the original estimate.
If any line or rate was reduced, the decision is `PAY_ADJUSTED` (`PRICE_ADJUSTED`).

## 7. Decision order (first match wins)
1. Policy not in force → `DENY` (`POLICY_INACTIVE`)
2. Required coverage absent → `DENY` (`NO_COVERAGE`)
3. Any SIU trigger → `REFER_SIU` (the triggers as reason codes)
4. Required document missing → `REQUEST_INFO` (`MISSING_DOC`)
5. Driver not listed → `REQUEST_INFO` (`DRIVER_VERIFICATION`)
6. Approved repair cost > **75%** of actual cash value (ACV) → `PAY` as a total loss (`TOTAL_LOSS`): ACV − deductible
7. Approved repair cost ≤ deductible → `DENY` (`BELOW_DEDUCTIBLE`)
8. Otherwise `PAY_ADJUSTED` (`PRICE_ADJUSTED`) if anything was reduced, else `PAY` (`COVERED_LOSS`):
   approved repair cost − deductible

The deductible is the one for the coverage that applies (COLL or COMP).
Payout amounts are computed by the claims system, never typed in by hand.
No decision is written to the claims system without an adjuster's approval.
