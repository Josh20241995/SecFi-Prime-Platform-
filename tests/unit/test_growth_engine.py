import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import CYCLE_AS_OF, default_counterparty_limits_usd, load_counterparties, load_positions  # noqa: E402
from secfi_platform.common.enums import RecommendationAction  # noqa: E402
from secfi_platform.growth.counterparty_growth import (  # noqa: E402
    GrowthThresholds,
    assess_counterparty_opportunity,
)
from secfi_platform.risk.capital_rwa import compute_counterparty_capital_summary  # noqa: E402
from secfi_platform.risk.counterparty_risk import compute_counterparty_exposure  # noqa: E402


class TestGrowthEngine(unittest.TestCase):
    def setUp(self):
        self.counterparties = load_counterparties()
        self.positions = load_positions()
        self.limits = default_counterparty_limits_usd()

    def _build(self, counterparty_id, has_critical_breaks=False):
        cpty = self.counterparties[counterparty_id]
        positions = [p for p in self.positions if p.counterparty_id == counterparty_id]
        capital_summary = compute_counterparty_capital_summary(cpty, positions)
        exposure = compute_counterparty_exposure(
            cpty, positions, as_of=CYCLE_AS_OF.isoformat(), limit_usd=self.limits.get(counterparty_id)
        )
        return assess_counterparty_opportunity(
            cpty, capital_summary, exposure, has_unresolved_critical_breaks=has_critical_breaks,
        )

    def test_watch_list_counterparty_recommended_reduce(self):
        # CPTY004 is watch_list=True in the fixture.
        opp = self._build("CPTY004")
        self.assertEqual(opp.action, RecommendationAction.REDUCE)

    def test_critical_breaks_force_reduce_even_if_economics_good(self):
        opp = self._build("CPTY002", has_critical_breaks=True)
        self.assertEqual(opp.action, RecommendationAction.REDUCE)

    def test_action_is_one_of_valid_enum_values(self):
        for cpty_id in self.counterparties:
            opp = self._build(cpty_id)
            self.assertIn(opp.action, list(RecommendationAction))

    def test_rationale_always_present(self):
        for cpty_id in self.counterparties:
            opp = self._build(cpty_id)
            self.assertTrue(len(opp.rationale) > 0)

    def test_priority_score_in_valid_range(self):
        for cpty_id in self.counterparties:
            opp = self._build(cpty_id)
            self.assertGreaterEqual(opp.priority_score, 0.0)
            self.assertLessEqual(opp.priority_score, 100.0)

    def test_custom_thresholds_change_outcome(self):
        cpty = self.counterparties["CPTY003"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY003"]
        capital_summary = compute_counterparty_capital_summary(cpty, positions)
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat(),
                                                   limit_usd=self.limits.get("CPTY003"))
        lenient = assess_counterparty_opportunity(
            cpty, capital_summary, exposure, GrowthThresholds(target_roc=0.001, min_acceptable_roc=-1.0),
        )
        strict = assess_counterparty_opportunity(
            cpty, capital_summary, exposure, GrowthThresholds(target_roc=10.0, min_acceptable_roc=5.0),
        )
        self.assertNotEqual(lenient.action, strict.action)


if __name__ == "__main__":
    unittest.main()
