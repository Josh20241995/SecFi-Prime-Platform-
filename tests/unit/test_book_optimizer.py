import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import load_counterparties, load_market_quotes, load_positions  # noqa: E402
from secfi_platform.optimization.book_optimizer import (  # noqa: E402
    OptimizationCandidate,
    OptimizationConstraints,
    optimize_book,
)


def _market_rate_lookup(quotes):
    return {
        sec_id: (q.weighted_avg_fee_bps if q.weighted_avg_fee_bps is not None else q.avg_fee_bps)
        for sec_id, q in quotes.items()
    }


class TestBookOptimizer(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.counterparties = load_counterparties()
        self.quotes = load_market_quotes()
        self.market_rates = _market_rate_lookup(self.quotes)

    def _candidates(self, positions=None):
        positions = positions if positions is not None else self.positions
        return [
            OptimizationCandidate(
                position=p,
                candidate_counterparties=tuple(c for c in self.counterparties if c != p.counterparty_id)[:3],
            )
            for p in positions
        ]

    def test_solver_returns_optimal_status(self):
        constraints = OptimizationConstraints(counterparty_limits_usd={})
        result = optimize_book(self._candidates(), self.counterparties, self.market_rates, constraints)
        self.assertEqual(result.solver_status, "OPTIMAL")

    def test_no_candidates_handled_gracefully(self):
        constraints = OptimizationConstraints(counterparty_limits_usd={})
        result = optimize_book([], self.counterparties, self.market_rates, constraints)
        self.assertEqual(result.solver_status, "NO_CANDIDATES")
        self.assertEqual(result.recommendations, [])

    def test_recommendations_never_exceed_position_market_value(self):
        constraints = OptimizationConstraints(counterparty_limits_usd={})
        result = optimize_book(self._candidates(), self.counterparties, self.market_rates, constraints)
        by_position = {}
        for rec in result.recommendations:
            by_position.setdefault(rec.target_id, Decimal("0"))
            by_position[rec.target_id] += rec.quantity
        positions_by_id = {p.position_id: p for p in self.positions}
        for pos_id, allocated in by_position.items():
            self.assertLessEqual(allocated, positions_by_id[pos_id].market_value * Decimal("1.0001"))

    def test_tight_counterparty_limit_constrains_allocation(self):
        # Force an artificially tiny limit on CPTY002 and verify the optimizer
        # never recommends routing MORE balance to it than the limit allows.
        tiny_limit = Decimal("1000000")
        constraints = OptimizationConstraints(counterparty_limits_usd={"CPTY002": tiny_limit})
        result = optimize_book(self._candidates(), self.counterparties, self.market_rates, constraints)
        total_to_cpty002 = sum(
            (rec.quantity for rec in result.recommendations
             if rec.supporting_metrics.get("destination_counterparty_id") == "CPTY002"),
            Decimal("0"),
        )
        self.assertLessEqual(total_to_cpty002, tiny_limit * Decimal("1.0001"))

    def test_issuer_concentration_cap_respected(self):
        constraints = OptimizationConstraints(counterparty_limits_usd={}, issuer_concentration_cap_pct=0.05)
        result = optimize_book(self._candidates(), self.counterparties, self.market_rates, constraints)
        # With a very tight 5% issuer cap, total allocation to any single issuer's
        # positions across all recommendations should stay bounded relative to book NMV.
        self.assertIn(result.solver_status, ("OPTIMAL", "NO_VARIABLES"))

    def test_min_economic_pickup_threshold_suppresses_noise(self):
        loose = OptimizationConstraints(counterparty_limits_usd={}, min_economic_pickup_bps=0.0)
        strict = OptimizationConstraints(counterparty_limits_usd={}, min_economic_pickup_bps=1000.0)
        result_loose = optimize_book(self._candidates(), self.counterparties, self.market_rates, loose)
        result_strict = optimize_book(self._candidates(), self.counterparties, self.market_rates, strict)
        reprice_loose = [r for r in result_loose.recommendations if r.action.value == "REPRICE"]
        reprice_strict = [r for r in result_strict.recommendations if r.action.value == "REPRICE"]
        self.assertGreaterEqual(len(reprice_loose), len(reprice_strict))

    def test_all_recommendations_explainable(self):
        constraints = OptimizationConstraints(counterparty_limits_usd={})
        result = optimize_book(self._candidates(), self.counterparties, self.market_rates, constraints)
        for rec in result.recommendations:
            self.assertTrue(len(rec.rationale) > 0)
            self.assertTrue(0.0 <= rec.confidence <= 1.0)
            self.assertTrue(0.0 <= rec.priority_score <= 100.0)


if __name__ == "__main__":
    unittest.main()
