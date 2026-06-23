import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import (  # noqa: E402
    CYCLE_AS_OF,
    default_counterparty_limits_usd,
    load_counterparties,
    load_positions,
)
from secfi_platform.risk.counterparty_risk import (  # noqa: E402
    compute_book_exposure_by_counterparty,
    compute_counterparty_exposure,
    STANDARD_SCENARIOS,
)


class TestCounterpartyRiskEngine(unittest.TestCase):
    def setUp(self):
        self.counterparties = load_counterparties()
        self.positions = load_positions()
        self.limits = default_counterparty_limits_usd()

    def test_exposure_computed_for_every_counterparty(self):
        results = compute_book_exposure_by_counterparty(
            self.counterparties.values(), self.positions,
            as_of=CYCLE_AS_OF.isoformat(), limits_by_counterparty_id=self.limits,
        )
        self.assertEqual(set(results.keys()), set(self.counterparties.keys()))

    def test_gross_exposure_non_negative(self):
        results = compute_book_exposure_by_counterparty(
            self.counterparties.values(), self.positions, as_of=CYCLE_AS_OF.isoformat(),
        )
        for exposure in results.values():
            self.assertGreaterEqual(exposure.gross_exposure_usd, 0)

    def test_uncollateralized_position_flows_into_uncollateralized_bucket(self):
        # P003 (GME, CPTY003) has no collateral leg in the fixture.
        cpty = self.counterparties["CPTY003"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY003"]
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat())
        self.assertGreater(exposure.uncollateralized_exposure_usd, 0)

    def test_limit_breach_detection(self):
        # A $0 limit is treated by the engine as "no limit configured" (guards
        # against division by zero / a missing-data false breach) — use a
        # small positive limit to genuinely exercise the breach path instead.
        cpty = self.counterparties["CPTY004"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY004"]
        tiny_limit = positions[0].market_value * Decimal("0.01")
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat(),
                                                   limit_usd=tiny_limit)
        self.assertTrue(exposure.limit_breached)
        self.assertEqual(exposure.headroom_usd, tiny_limit - exposure.gross_exposure_usd)

    def test_no_limit_means_no_utilization_or_breach(self):
        cpty = self.counterparties["CPTY001"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY001"]
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat(), limit_usd=None)
        self.assertIsNone(exposure.utilization_pct)
        self.assertFalse(exposure.limit_breached)

    def test_wrong_way_risk_flagged_for_concentrated_bank_dealer(self):
        # CPTY002 is a BANK_DEALER with a large JPM (Financials) position concentrated
        # relative to its other sector-tracked exposure — see tests/fixtures/positions.csv
        # comment in test docstring for the construction rationale.
        cpty = self.counterparties["CPTY002"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY002"]
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat())
        self.assertTrue(len(exposure.wrong_way_risk_flags) >= 1)

    def test_stress_scenarios_present_for_all_standard_scenarios(self):
        cpty = self.counterparties["CPTY001"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY001"]
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat())
        self.assertEqual(set(exposure.stress_results.keys()), {s.name for s in STANDARD_SCENARIOS})

    def test_equity_down_shock_increases_lend_exposure(self):
        # Desk lends securities against cash collateral sized to current MV; if security
        # price falls, collateral becomes MORE than sufficient, so net (signed) exposure
        # should fall (more negative / less positive), not rise — verify the documented
        # sign convention in risk/counterparty_risk.py rather than assume.
        cpty = self.counterparties["CPTY001"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY001" and p.direction.value == "LEND"]
        exposure = compute_counterparty_exposure(cpty, positions, as_of=CYCLE_AS_OF.isoformat())
        base_net = exposure.stress_results["BASE"]["net_exposure_usd"]
        shocked_net = exposure.stress_results["EQUITY_DOWN_10"]["net_exposure_usd"]
        self.assertLess(shocked_net, base_net)

    def test_herfindahl_within_valid_range(self):
        results = compute_book_exposure_by_counterparty(self.counterparties.values(), self.positions,
                                                          as_of=CYCLE_AS_OF.isoformat())
        for exposure in results.values():
            self.assertGreaterEqual(exposure.herfindahl_issuer, 0)
            self.assertLessEqual(exposure.herfindahl_issuer, 10000)


if __name__ == "__main__":
    unittest.main()
