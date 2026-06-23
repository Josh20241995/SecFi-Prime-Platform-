"""
Integration test for the full orchestration cycle.

This is the test that proves the platform actually wires together —
every engine's output feeding the next stage correctly — not just that
each engine works in isolation. This is the closest thing in this
reference build to "deploy to a staging environment and run one cycle."
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import (  # noqa: E402
    CYCLE_AS_OF,
    default_counterparty_limits_usd,
    default_substitutes,
    load_book_recon_df,
    load_corporate_action_events,
    load_counterparties,
    load_custodian_recon_df,
    load_fx_rates,
    load_locate_shortages,
    load_market_quotes,
    load_positions,
    load_settlement_fails,
)
from secfi_platform.orchestration.scheduler import CycleInputs, run_full_cycle  # noqa: E402


def _build_inputs() -> CycleInputs:
    return CycleInputs(
        as_of=CYCLE_AS_OF,
        positions=load_positions(),
        counterparties=load_counterparties(),
        market_quotes=load_market_quotes(),
        fx_rates=load_fx_rates(),
        corporate_action_events=load_corporate_action_events(),
        settlement_fails=load_settlement_fails(),
        locate_shortages=load_locate_shortages(),
        substitutes=default_substitutes(),
        counterparty_limits_usd=default_counterparty_limits_usd(),
        book_recon_df=load_book_recon_df(),
        custodian_recon_df=load_custodian_recon_df(),
    )


class TestFullOrchestrationCycle(unittest.TestCase):
    def setUp(self):
        self.outputs = run_full_cycle(_build_inputs())

    def test_correlation_id_assigned(self):
        self.assertTrue(self.outputs.correlation_id.startswith("secfi-"))

    def test_all_counterparties_have_exposure_and_capital_summary(self):
        self.assertEqual(set(self.outputs.counterparty_exposures.keys()),
                          set(self.outputs.capital_summaries.keys()))

    def test_reconciliation_breaks_detected(self):
        self.assertGreater(len(self.outputs.recon_breaks), 0)

    def test_pricing_recommendations_generated(self):
        self.assertGreater(len(self.outputs.pricing_recommendations), 0)

    def test_optimization_completes_successfully(self):
        self.assertEqual(self.outputs.optimization_result.solver_status, "OPTIMAL")

    def test_recall_queue_has_gme_at_top(self):
        self.assertEqual(self.outputs.recall_queue[0].security_internal_id, "SEC003")

    def test_corporate_action_watchlist_populated(self):
        self.assertEqual(len(self.outputs.ca_watchlist), 5)

    def test_growth_opportunities_cover_every_counterparty(self):
        self.assertEqual(len(self.outputs.growth_opportunities), 5)

    def test_watch_list_counterparty_flagged_for_reduction(self):
        delta_opportunity = next(o for o in self.outputs.growth_opportunities if o.counterparty_id == "CPTY004")
        self.assertEqual(delta_opportunity.action.value, "REDUCE")

    def test_alerts_raised_for_recall_and_recon(self):
        categories = {a.category for a in self.outputs.alerts}
        self.assertTrue({"RECALL_BUYIN", "RECONCILIATION_BREAK"} & categories)

    def test_executive_summary_internally_consistent(self):
        summary = self.outputs.executive_summary
        self.assertEqual(summary.as_of, CYCLE_AS_OF)
        self.assertGreater(summary.book_nmv_usd, Decimal("0"))
        self.assertEqual(summary.open_critical_recon_breaks,
                          sum(1 for b in self.outputs.recon_breaks if b["severity"].value == "CRITICAL"))

    def test_executive_summary_renders_to_markdown(self):
        from secfi_platform.reporting.daily_summary import render_markdown
        markdown = render_markdown(self.outputs.executive_summary)
        self.assertIn("Daily Executive Summary", markdown)
        self.assertIn("Book NMV", markdown)

    def test_every_recommendation_carries_full_explainability_contract(self):
        all_recs = (
            self.outputs.optimization_result.recommendations
            + self.outputs.pricing_recommendations
            + self.outputs.recall_recommendations
            + self.outputs.ca_recommendations
            + self.outputs.growth_recommendations
        )
        self.assertGreater(len(all_recs), 0)
        for rec in all_recs:
            self.assertTrue(rec.rationale)
            self.assertTrue(0.0 <= rec.confidence <= 1.0)
            self.assertTrue(0.0 <= rec.data_completeness_pct <= 1.0)
            self.assertEqual(rec.approval_status.value, "PROPOSED")

    def test_cycle_is_deterministic_given_same_inputs(self):
        second_run = run_full_cycle(_build_inputs())
        self.assertEqual(
            self.outputs.executive_summary.book_nmv_usd,
            second_run.executive_summary.book_nmv_usd,
        )
        self.assertEqual(len(self.outputs.recon_breaks), len(second_run.recon_breaks))
        self.assertEqual(
            self.outputs.optimization_result.solver_status,
            second_run.optimization_result.solver_status,
        )


if __name__ == "__main__":
    unittest.main()
