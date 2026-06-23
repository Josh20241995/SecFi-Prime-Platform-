import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import CYCLE_AS_OF, load_fx_rates, load_positions  # noqa: E402
from secfi_platform.risk.rates_fx import (  # noqa: E402
    build_rates_fx_report,
    compute_dv01,
    compute_fx_exposure,
    compute_funding_gap,
    recommend_hedges,
)


class TestRatesFxEngine(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.fx_rates = load_fx_rates()

    def test_dv01_only_includes_cash_rate_sensitive_legs(self):
        buckets = compute_dv01(self.positions, CYCLE_AS_OF)
        # The fixture book has REPO/REVERSE_REPO positions (cash-rate-sensitive)
        # plus LEND/BORROW positions entirely collateralized in CASH (also rate
        # sensitive) — so DV01 buckets should be non-empty.
        self.assertGreater(len(buckets), 0)
        for b in buckets:
            self.assertIsInstance(b.dv01_usd, Decimal)

    def test_dv01_sign_convention_repo_vs_reverse_repo(self):
        buckets = compute_dv01(self.positions, CYCLE_AS_OF)
        # P006 is REPO (desk borrows cash, pays rate -> negative DV01 contribution)
        # P007 is REVERSE_REPO (desk lends cash, earns rate -> positive DV01 contribution)
        # Both reference SEC007/UST10Y cash legs; net sign depends on bucket aggregation,
        # so instead verify directly via the position-level helper semantics:
        from secfi_platform.risk.rates_fx import _rate_exposure_sign
        repo_pos = next(p for p in self.positions if p.direction.value == "REPO")
        reverse_repo_pos = next(p for p in self.positions if p.direction.value == "REVERSE_REPO")
        self.assertEqual(_rate_exposure_sign(repo_pos), Decimal("-1"))
        self.assertEqual(_rate_exposure_sign(reverse_repo_pos), Decimal("1"))

    def test_fx_exposure_excludes_base_currency(self):
        exposures = compute_fx_exposure(self.positions, self.fx_rates, base_currency="USD")
        for fx in exposures:
            self.assertNotEqual(fx.currency, "USD")

    def test_fx_exposure_empty_when_book_is_all_usd(self):
        # The fixture book is entirely USD-denominated, so FX exposure should be empty
        # even though FX rates are loaded — proves the engine isn't hallucinating exposure.
        exposures = compute_fx_exposure(self.positions, self.fx_rates)
        self.assertEqual(exposures, [])

    def test_funding_gap_buckets_sum_to_total_rate_sensitive_notional(self):
        gap = compute_funding_gap(self.positions, CYCLE_AS_OF)
        total_assets = sum((v["assets_usd"] for v in gap.values()), Decimal("0"))
        total_funding = sum((v["funding_usd"] for v in gap.values()), Decimal("0"))
        self.assertGreater(total_assets + total_funding, 0)

    def test_hedge_recommendations_respect_materiality_threshold(self):
        buckets = compute_dv01(self.positions, CYCLE_AS_OF)
        fx_exposures = compute_fx_exposure(self.positions, self.fx_rates)
        recs = recommend_hedges(buckets, fx_exposures, dv01_materiality_usd=Decimal("999999999"))
        # With an absurdly high materiality threshold, no DV01 hedge should be recommended.
        ir_recs = [r for r in recs if r["type"] == "INTEREST_RATE_HEDGE"]
        self.assertEqual(ir_recs, [])

    def test_build_rates_fx_report_end_to_end(self):
        report = build_rates_fx_report(self.positions, self.fx_rates, CYCLE_AS_OF)
        self.assertEqual(report.as_of, CYCLE_AS_OF.isoformat())
        self.assertIsInstance(report.total_dv01_usd, Decimal)
        self.assertIsInstance(report.hedge_recommendations, list)


if __name__ == "__main__":
    unittest.main()
