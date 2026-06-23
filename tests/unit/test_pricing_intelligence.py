import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import load_market_quotes, load_positions  # noqa: E402
from secfi_platform.common.enums import SpecialnessTier  # noqa: E402
from secfi_platform.pricing.pricing_intelligence import (  # noqa: E402
    build_pricing_dispersion,
    classify_specialness,
    generate_pricing_recommendations,
)


class TestPricingIntelligenceEngine(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.quotes = load_market_quotes()

    def test_deep_special_classification(self):
        gme_quote = self.quotes["SEC003"]
        tier = classify_specialness(gme_quote)
        self.assertEqual(tier, SpecialnessTier.DEEP_SPECIAL)

    def test_gc_classification(self):
        spy_quote = self.quotes["SEC004"]
        tier = classify_specialness(spy_quote)
        self.assertEqual(tier, SpecialnessTier.GC)

    def test_specials_in_waiting_classification(self):
        tsla_quote = self.quotes["SEC002"]
        tier = classify_specialness(tsla_quote)
        self.assertEqual(tier, SpecialnessTier.SPECIALS_IN_WAITING)

    def test_dispersion_z_score_present_when_tier_has_multiple_members(self):
        rows = build_pricing_dispersion(self.positions, self.quotes)
        scored = [r for r in rows if r.z_score_within_tier is not None]
        self.assertGreater(len(scored), 0)

    def test_missing_quote_flagged_not_dropped(self):
        positions_subset = self.positions[:1]
        rows = build_pricing_dispersion(positions_subset, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].data_quality_flag, "MISSING")

    def test_gme_generates_large_reprice_recommendation(self):
        # GME (SEC003) is booked far below its deep-special market rate on the LEND
        # side (P003, P011) — must generate high-value REPRICE recommendations.
        recs = generate_pricing_recommendations(self.positions, self.quotes)
        gme_recs = [r for r in recs if "SEC003" in str(r.supporting_metrics)]
        target_position_recs = [r for r in recs if r.target_id in ("P003", "P011")]
        self.assertTrue(len(target_position_recs) >= 1)
        for r in target_position_recs:
            self.assertGreater(r.estimated_pnl_impact_usd, 0)

    def test_borrow_overpaying_flags_reprice_down(self):
        # P004 BORROW SEC004 (SPY/GC) at 20bps vs market ~8bps — desk overpaying.
        recs = generate_pricing_recommendations(self.positions, self.quotes)
        p004_recs = [r for r in recs if r.target_id == "P004"]
        self.assertEqual(len(p004_recs), 1)
        self.assertLess(p004_recs[0].to_value, p004_recs[0].from_value)

    def test_reverse_repo_not_misclassified_as_paying_side(self):
        # P007 is REVERSE_REPO at 520bps vs market ~511bps — desk EARNS more than
        # market already; must NOT generate a "reprice down" recommendation
        # (regression test for the LEND/REVERSE_REPO economic-side fix).
        recs = generate_pricing_recommendations(self.positions, self.quotes)
        p007_recs = [r for r in recs if r.target_id == "P007"]
        self.assertEqual(p007_recs, [])

    def test_all_recommendations_have_rationale(self):
        recs = generate_pricing_recommendations(self.positions, self.quotes)
        for r in recs:
            self.assertTrue(len(r.rationale) > 0)
            self.assertGreaterEqual(r.confidence, 0.0)
            self.assertLessEqual(r.confidence, 1.0)


if __name__ == "__main__":
    unittest.main()
