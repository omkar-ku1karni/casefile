from datetime import date

from casefile import rules
from casefile.rules import DecisionFacts, EstimateRates, Line, RefPart


def facts(**kw):
    base = dict(policy_active=True, coverage_code="COLL", coverage_present=True, deductible_c=50000,
                missing_documents=[], siu_flags=[], driver_listed=True, approved_total_c=300000, adjusted=False,
                acv_c=2000000)
    return DecisionFacts(**(base | kw))


def test_money_is_integer_cents_and_half_up():
    assert rules.hours_cost(2.35, 5800) == 13920  # 2.4h (half-up tenths) x $58
    assert rules.apply_bp(10000, 575) == 575
    assert rules.to_cents(412.005) == 41201


def test_estimate_totals():
    lines = [Line("Repl", "P1", "bumper", 1, 42000, 2.4, 2.6), Line("Refn", "P2", "door", 1, 0, 0.0, 2.0)]
    t = rules.estimate_totals(lines, EstimateRates(5800, 6000, 3800, 450, 575))
    assert t.parts_c == 42000 and t.body_c == 13920 and t.paint_c == 27600 and t.materials_c == 17480
    assert t.tax_c == rules.apply_bp(42000 + 17480, 575)
    assert t.total_c == 42000 + 13920 + 27600 + 17480 + 450 + t.tax_c


def test_price_check_tolerances():
    refs = {"P1": RefPart("P1", "bumper", 40000, 2.0, 2.0)}
    ok = [Line("Repl", "P1", "bumper", 1, 46000, 2.3, 2.3)]  # exactly at +15%
    assert not rules.price_check(ok, EstimateRates(5800, 6000, 3800, 0, 575), refs, "OH").adjusted
    bad = [Line("Repl", "P1", "bumper", 1, 46100, 2.4, 2.0)]
    pc = rules.price_check(bad, EstimateRates(7000, 6000, 3800, 0, 575), refs, "OH")
    assert {a.field for a in pc.adjustments} == {"part_price", "labor_hours", "body_rate"}
    assert pc.approved.total_c < pc.billed.total_c


def test_unknown_part_is_not_approved():
    pc = rules.price_check([Line("Repl", "NOPE", "x", 1, 10000, 1.0, 0)], EstimateRates(5800, 6000, 3800, 0, 575),
                           {}, "OH")
    assert pc.unknown_parts == ["NOPE"] and pc.approved.parts_c == 0


def test_policy_active_edges():
    eff, exp = date(2026, 1, 1), date(2026, 12, 31)
    assert rules.policy_active(date(2026, 5, 1), eff, exp, None)
    assert not rules.policy_active(date(2026, 5, 1), eff, exp, date(2026, 4, 1))
    assert rules.policy_active(date(2026, 5, 1), eff, exp, date(2026, 6, 1))  # cancelled after the loss
    assert not rules.policy_active(date(2027, 1, 1), eff, exp, None)


def test_dates_conflict_one_day_is_fine():
    assert not rules.dates_conflict(date(2026, 3, 2), date(2026, 3, 1))
    assert rules.dates_conflict(date(2026, 3, 5), date(2026, 3, 1))


def test_decision_order():
    assert rules.decide(facts(policy_active=False, siu_flags=["DATE_CONFLICT"])).decision == "DENY"
    assert rules.decide(facts(coverage_present=False)).reason_codes == ["NO_COVERAGE"]
    assert rules.decide(facts(siu_flags=["PRIOR_DAMAGE"], missing_documents=["police_report"])).decision == "REFER_SIU"
    assert rules.decide(facts(missing_documents=["estimate"], approved_total_c=None)).reason_codes == ["MISSING_DOC"]
    assert rules.decide(facts(driver_listed=False)).reason_codes == ["DRIVER_VERIFICATION"]
    tl = rules.decide(facts(approved_total_c=1600000))
    assert tl.reason_codes == ["TOTAL_LOSS"] and tl.payout_c == 2000000 - 50000
    assert rules.decide(facts(approved_total_c=50000)).reason_codes == ["BELOW_DEDUCTIBLE"]
    adj = rules.decide(facts(adjusted=True))
    assert adj.decision == "PAY_ADJUSTED" and adj.payout_c == 250000
    assert rules.decide(facts()).payout_c == 250000


def test_payout_for_matches_decide():
    assert rules.payout_for("PAY", 300000, 2000000, 50000, False) == 250000
    assert rules.payout_for("DENY", 300000, 2000000, 50000, False) == 0
    assert rules.payout_for("PAY", 1600000, 2000000, 50000, True) == 1950000
