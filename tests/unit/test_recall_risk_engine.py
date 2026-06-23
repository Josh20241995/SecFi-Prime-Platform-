import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import (  # noqa: E402
    default_substitutes,
    load_locate_shortages,
    load_market_quotes,
    load_positions,
    load_settlement_fails,
)
from secfi_platform.pricing.pricing_intelligence import classify_specialness  # noqa: E402
from secfi_platform.recall_buyin.recall_risk_engine import (  # noqa: E402
    compute_buyin_risk_score,
    compute_urgency_queue,
    queue_to_recommendations,
)


class TestRecallBuyinEngine(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.fails = load_settlement_fails()
        self.shortages = load_locate_shortages()
        self.substitutes = default_substitutes()
        quotes = load_market_quotes()
        self.specialness = {sec_id: classify_specialness(q) for sec_id, q in quotes.items()}

    def test_fail_to_deliver_on_deep_special_is_top_of_queue(self):
        # P003: GME (DEEP_SPECIAL), fail_age 6 days, fail-to-deliver, no substitute —
        # should be the single highest urgency/buy-in item in the whole queue.
        rows = compute_urgency_queue(self.positions, self.fails, self.shortages, self.specialness,
                                      self.substitutes, ca_driven_return_security_ids=set())
        self.assertEqual(rows[0].position_id, "P003")
        self.assertGreaterEqual(rows[0].buyin_risk_score, 70)

    def test_fail_to_receive_scores_lower_buyin_risk_than_fail_to_deliver(self):
        receiving = compute_buyin_risk_score(
            next(f for f in self.fails if f.is_desk_receiving),
            self.specialness.get("SEC002"), has_substitute=True,
        )
        delivering = compute_buyin_risk_score(
            next(f for f in self.fails if not f.is_desk_receiving and f.position_id == "P003"),
            self.specialness.get("SEC003"), has_substitute=False,
        )
        self.assertLess(receiving, delivering)

    def test_substitute_availability_reduces_buyin_score(self):
        # Use a moderate-severity synthetic fail (not the extreme P003/GME fixture,
        # which saturates at the 100-point cap regardless of substitute availability)
        # so the multiplier's effect is actually observable below the cap.
        from decimal import Decimal
        from secfi_platform.recall_buyin.recall_risk_engine import SettlementFail
        moderate_fail = SettlementFail(
            position_id="SYNTHETIC", security_internal_id="SEC003", fail_age_days=2,
            fail_quantity=Decimal("1000"), counterparty_id="CPTY003", is_desk_receiving=False,
        )
        tier = self.specialness.get("SEC003")
        with_sub = compute_buyin_risk_score(moderate_fail, tier, has_substitute=True)
        without_sub = compute_buyin_risk_score(moderate_fail, tier, has_substitute=False)
        self.assertLess(with_sub, without_sub)

    def test_ca_driven_return_increases_urgency(self):
        rows_without_ca = compute_urgency_queue(self.positions, self.fails, self.shortages, self.specialness,
                                                  self.substitutes, ca_driven_return_security_ids=set())
        rows_with_ca = compute_urgency_queue(self.positions, self.fails, self.shortages, self.specialness,
                                              self.substitutes, ca_driven_return_security_ids={"SEC003"})
        urgency_without = next(r.urgency_score for r in rows_without_ca if r.position_id == "P003")
        urgency_with = next(r.urgency_score for r in rows_with_ca if r.position_id == "P003")
        self.assertGreaterEqual(urgency_with, urgency_without)

    def test_recommendations_generated_only_for_actionable_rows(self):
        rows = compute_urgency_queue(self.positions, self.fails, self.shortages, self.specialness,
                                      self.substitutes, ca_driven_return_security_ids=set())
        recs = queue_to_recommendations(rows)
        for rec in recs:
            self.assertNotEqual(rec.action.value, "DO_NOTHING")

    def test_queue_sorted_descending_by_risk(self):
        rows = compute_urgency_queue(self.positions, self.fails, self.shortages, self.specialness,
                                      self.substitutes, ca_driven_return_security_ids=set())
        scores = [(r.buyin_risk_score, r.urgency_score) for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
