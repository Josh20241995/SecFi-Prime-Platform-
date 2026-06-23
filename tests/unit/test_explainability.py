import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from secfi_platform.explainability.explain import (  # noqa: E402
    DataCompletenessReport,
    PriorityWeights,
    build_rationale,
    compute_confidence,
    compute_priority_score,
    staleness_penalty,
)


class TestExplainabilityFramework(unittest.TestCase):
    def test_staleness_penalty_at_zero_age_is_one(self):
        self.assertEqual(staleness_penalty(0), 1.0)

    def test_staleness_penalty_at_half_life_is_half(self):
        self.assertAlmostEqual(staleness_penalty(240, half_life_minutes=240), 0.5, places=6)

    def test_staleness_penalty_floored(self):
        self.assertEqual(staleness_penalty(100_000, half_life_minutes=240), 0.10)

    def test_confidence_zero_when_no_fields_present(self):
        completeness = DataCompletenessReport(required_fields=5, present_and_valid_fields=0, fallback_fields=5)
        confidence = compute_confidence(completeness, model_certainty=1.0, data_age_minutes=0)
        self.assertEqual(confidence, 0.0)

    def test_confidence_full_when_everything_present_and_fresh(self):
        completeness = DataCompletenessReport(required_fields=4, present_and_valid_fields=4, fallback_fields=0)
        confidence = compute_confidence(completeness, model_certainty=1.0, data_age_minutes=0)
        self.assertEqual(confidence, 1.0)

    def test_confidence_bounded_zero_to_one(self):
        completeness = DataCompletenessReport(required_fields=3, present_and_valid_fields=3, fallback_fields=0)
        confidence = compute_confidence(completeness, model_certainty=5.0, data_age_minutes=-10)
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 1.0)

    def test_build_rationale_dedupes_and_strips_empties(self):
        rationale = build_rationale("  same clause  ", "same clause", "", None, "different clause")
        self.assertEqual(rationale, ["same clause", "different clause"])

    def test_priority_score_increases_with_pnl(self):
        low = compute_priority_score(pnl_score_0_100=10, risk_score_0_100=50, urgency_score_0_100=50, confidence_0_1=1.0)
        high = compute_priority_score(pnl_score_0_100=90, risk_score_0_100=50, urgency_score_0_100=50, confidence_0_1=1.0)
        self.assertGreater(high, low)

    def test_priority_score_damped_by_low_confidence(self):
        confident = compute_priority_score(pnl_score_0_100=80, risk_score_0_100=80, urgency_score_0_100=80, confidence_0_1=1.0)
        unsure = compute_priority_score(pnl_score_0_100=80, risk_score_0_100=80, urgency_score_0_100=80, confidence_0_1=0.1)
        self.assertGreater(confident, unsure)

    def test_priority_score_bounded(self):
        score = compute_priority_score(pnl_score_0_100=1000, risk_score_0_100=1000, urgency_score_0_100=1000, confidence_0_1=1.0)
        self.assertLessEqual(score, 100.0)

    def test_custom_weights_respected(self):
        weights = PriorityWeights(pnl_weight=1.0, risk_weight=0.0, urgency_weight=0.0, confidence_weight=0.0)
        score = compute_priority_score(pnl_score_0_100=50, risk_score_0_100=100, urgency_score_0_100=100,
                                        confidence_0_1=1.0, weights=weights)
        self.assertAlmostEqual(score, 50.0 * 1.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
