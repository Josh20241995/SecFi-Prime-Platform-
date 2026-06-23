import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import load_counterparties, load_positions  # noqa: E402
from secfi_platform.risk.capital_rwa import (  # noqa: E402
    CapitalParameters,
    compute_counterparty_capital_summary,
    compute_position_capital_profile,
)


class TestCapitalRwaEngine(unittest.TestCase):
    def setUp(self):
        self.counterparties = load_counterparties()
        self.positions = load_positions()

    def test_higher_tier_risk_means_higher_rwa_per_dollar(self):
        # Compare the same hypothetical position's RWA at a TIER_1_PRIME bank-dealer
        # vs a TIER_4_RESTRICTED hedge fund — risk weight (and therefore RWA) must be higher
        # for the riskier counterparty given identical exposure.
        from secfi_platform.common.enums import CounterpartyTier
        prime = self.counterparties["CPTY002"]   # TIER_1_PRIME BANK_DEALER
        watch = self.counterparties["CPTY004"]   # TIER_3_WATCH HEDGE_FUND
        pos = next(p for p in self.positions if p.counterparty_id == "CPTY002")

        profile_prime = compute_position_capital_profile(pos, prime)
        # Re-target the same position economics at the watch-list counterparty for a fair compare.
        import dataclasses
        pos_watch = dataclasses.replace(pos, counterparty_id=watch.counterparty_id)
        profile_watch = compute_position_capital_profile(pos_watch, watch)

        self.assertGreater(profile_watch.risk_weight_pct, profile_prime.risk_weight_pct)
        self.assertGreater(profile_watch.rwa_usd, profile_prime.rwa_usd)

    def test_ead_is_non_negative(self):
        for pos in self.positions:
            cpty = self.counterparties[pos.counterparty_id]
            profile = compute_position_capital_profile(pos, cpty)
            self.assertGreaterEqual(profile.ead_usd, 0)

    def test_netting_eligible_counterparty_gets_leverage_relief(self):
        cpty = self.counterparties["CPTY002"]  # is_netting_eligible = True in fixture
        self.assertTrue(cpty.is_netting_eligible)
        pos = next(p for p in self.positions if p.counterparty_id == "CPTY002")
        profile = compute_position_capital_profile(pos, cpty)
        self.assertLess(profile.leverage_exposure_usd, profile.ead_usd)

    def test_rebate_position_reports_negative_revenue_when_no_reinvestment_spread(self):
        import dataclasses
        cpty = self.counterparties["CPTY002"]
        base_pos = next(p for p in self.positions if p.counterparty_id == "CPTY002")
        rebate_pos = dataclasses.replace(base_pos, rate_type_is_rebate=True, rate_bps=Decimal("25"))
        profile = compute_position_capital_profile(rebate_pos, cpty)
        self.assertLess(profile.annualized_revenue_usd, 0)

    def test_counterparty_summary_aggregates_all_positions(self):
        cpty = self.counterparties["CPTY002"]
        positions = [p for p in self.positions if p.counterparty_id == "CPTY002"]
        summary = compute_counterparty_capital_summary(cpty, positions)
        manual_total_rwa = sum((compute_position_capital_profile(p, cpty).rwa_usd for p in positions), Decimal("0"))
        self.assertEqual(summary.total_rwa_usd, manual_total_rwa)

    def test_capital_cost_scales_with_target_cet1_ratio(self):
        cpty = self.counterparties["CPTY001"]
        pos = next(p for p in self.positions if p.counterparty_id == "CPTY001")
        low_ratio = CapitalParameters(target_cet1_ratio=Decimal("0.08"))
        high_ratio = CapitalParameters(target_cet1_ratio=Decimal("0.16"))
        profile_low = compute_position_capital_profile(pos, cpty, low_ratio)
        profile_high = compute_position_capital_profile(pos, cpty, high_ratio)
        self.assertGreater(profile_high.capital_cost_usd, profile_low.capital_cost_usd)


if __name__ == "__main__":
    unittest.main()
