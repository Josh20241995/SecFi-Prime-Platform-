import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import CYCLE_AS_OF, load_book_recon_df, load_custodian_recon_df  # noqa: E402
from secfi_platform.common.enums import BreakSeverity, BreakType  # noqa: E402
from secfi_platform.reconciliation.recon_engine import ReconConfig, reconcile, summarize_breaks  # noqa: E402


class TestReconciliationEngine(unittest.TestCase):
    def setUp(self):
        self.book_df = load_book_recon_df()
        self.custodian_df = load_custodian_recon_df()

    def test_clean_matches_produce_no_breaks(self):
        # Reconciling the book against itself must produce zero breaks.
        breaks = reconcile(self.book_df, self.book_df, "CUSTODIAN", CYCLE_AS_OF)
        self.assertEqual(breaks, [])

    def test_quantity_mismatch_detected_for_p004(self):
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)
        qty_breaks = [b for b in breaks if b["break_type"] == BreakType.QUANTITY_MISMATCH]
        self.assertTrue(any(b["position_id"] == "P004" for b in qty_breaks))

    def test_missing_at_custodian_detected_for_p010(self):
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)
        missing = [b for b in breaks if b["break_type"] == BreakType.MISSING_AT_CUSTODIAN]
        self.assertTrue(any(b["position_id"] == "P010" for b in missing))
        for b in missing:
            self.assertTrue(b["buyin_risk_relevant"])

    def test_missing_on_book_detected_for_extra_custodian_row(self):
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)
        missing_on_book = [b for b in breaks if b["break_type"] == BreakType.MISSING_ON_BOOK]
        self.assertTrue(any(b["security_internal_id"] == "SEC008" and b["counterparty_id"] == "CPTY004"
                             for b in missing_on_book))

    def test_price_mismatch_detected_for_p009(self):
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)
        price_breaks = [b for b in breaks if b["break_type"] == BreakType.PRICE_RATE_MISMATCH]
        self.assertTrue(any(b["position_id"] == "P009" for b in price_breaks))

    def test_breaks_sorted_by_severity_descending(self):
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)
        rank = {BreakSeverity.LOW: 0, BreakSeverity.MEDIUM: 1, BreakSeverity.HIGH: 2, BreakSeverity.CRITICAL: 3}
        ranks = [rank[b["severity"]] for b in breaks]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_summarize_breaks_counts_match(self):
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)
        summary = summarize_breaks(breaks)
        self.assertEqual(summary["total"], len(breaks))
        self.assertEqual(sum(summary["by_severity"].values()), len(breaks))

    def test_missing_required_column_raises(self):
        bad_df = self.book_df.drop(columns=["quantity"])
        with self.assertRaises(ValueError):
            reconcile(bad_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF)

    def test_quantity_tolerance_configurable(self):
        # With a very loose tolerance, the P004 quantity mismatch should disappear.
        loose_config = ReconConfig(quantity_tolerance_pct=0.50)
        breaks = reconcile(self.book_df, self.custodian_df, "CUSTODIAN", CYCLE_AS_OF, config=loose_config)
        qty_breaks = [b for b in breaks if b["break_type"] == BreakType.QUANTITY_MISMATCH and b["position_id"] == "P004"]
        self.assertEqual(qty_breaks, [])


if __name__ == "__main__":
    unittest.main()
