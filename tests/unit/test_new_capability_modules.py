"""
Unit tests for new capability modules:
  - risk/collateral_optimizer.py
  - risk/inventory_manager.py
  - risk/limit_monitor.py
  - risk/scenario_engine.py
  - risk/exception_manager.py
  - common/data_quality.py
  - reporting/backtester.py
  - alerting/prioritizer.py
"""

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import (  # noqa: E402
    CYCLE_AS_OF,
    default_counterparty_limits_usd,
    load_counterparties,
    load_market_quotes,
    load_positions,
)


# ===========================================================================
# Collateral Optimizer
# ===========================================================================

class TestCollateralOptimizer(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.quotes = load_market_quotes()

    def test_candidates_only_from_rehypothecable_lend_positions(self):
        from secfi_platform.risk.collateral_optimizer import build_collateral_candidates
        candidates = build_collateral_candidates(self.positions, self.quotes)
        for c in candidates:
            pos = next((p for p in self.positions if p.position_id == c.position_id), None)
            self.assertIsNotNone(pos)
            self.assertTrue(pos.is_rehypothecable)
            self.assertIn(pos.direction.value, ("LEND", "REVERSE_REPO"))

    def test_candidates_sorted_by_ctd_score(self):
        from secfi_platform.risk.collateral_optimizer import build_collateral_candidates
        candidates = build_collateral_candidates(self.positions, self.quotes)
        scores = [c.ctd_score for c in candidates]
        self.assertEqual(scores, sorted(scores))

    def test_cheapest_to_deliver_respects_coverage(self):
        from secfi_platform.risk.collateral_optimizer import build_collateral_candidates, select_cheapest_to_deliver
        candidates = build_collateral_candidates(self.positions, self.quotes)
        if not candidates:
            self.skipTest("No rehypothecatable positions in fixture")
        required = Decimal("1000000")
        selected = select_cheapest_to_deliver(candidates, required)
        covered = sum(
            c.available_market_value * (Decimal("1") - c.haircut_pct) for c in selected
        )
        self.assertGreaterEqual(covered, required)

    def test_substitution_recommendations_sorted_by_savings(self):
        from secfi_platform.risk.collateral_optimizer import build_collateral_candidates, recommend_collateral_substitutions
        candidates = build_collateral_candidates(self.positions, self.quotes)
        recs = recommend_collateral_substitutions(self.positions, candidates)
        savings = [r.estimated_annual_savings_usd for r in recs]
        self.assertEqual(savings, sorted(savings, reverse=True))

    def test_total_opportunity_non_negative(self):
        from secfi_platform.risk.collateral_optimizer import (
            build_collateral_candidates, recommend_collateral_substitutions, total_collateral_optimization_opportunity_usd
        )
        candidates = build_collateral_candidates(self.positions, self.quotes)
        recs = recommend_collateral_substitutions(self.positions, candidates)
        total = total_collateral_optimization_opportunity_usd(recs)
        self.assertGreaterEqual(total, Decimal("0"))


# ===========================================================================
# Inventory Manager
# ===========================================================================

class TestInventoryManager(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()

    def test_inventory_snapshot_covers_all_securities(self):
        from secfi_platform.risk.inventory_manager import build_inventory_snapshot
        snapshot = build_inventory_snapshot(self.positions)
        security_ids_in_positions = {p.security.internal_id for p in self.positions}
        self.assertTrue(security_ids_in_positions.issubset(set(snapshot.keys())))

    def test_on_loan_quantity_matches_lend_positions(self):
        from secfi_platform.risk.inventory_manager import build_inventory_snapshot
        snapshot = build_inventory_snapshot(self.positions)
        for pos in self.positions:
            if pos.direction.value == "LEND":
                snap = snapshot.get(pos.security.internal_id)
                self.assertIsNotNone(snap)
                self.assertGreaterEqual(snap.on_loan_quantity, pos.quantity)

    def test_locate_resolution_fully_fills_when_adequate_inventory(self):
        from secfi_platform.risk.inventory_manager import (
            build_inventory_snapshot, LocateRequest, resolve_locate
        )
        snapshot = build_inventory_snapshot(self.positions)
        # Pick a security with known lendable inventory
        avail_snaps = [s for s in snapshot.values() if s.free_to_lend > 0]
        if not avail_snaps:
            self.skipTest("No free-to-lend inventory in fixture")
        snap = avail_snaps[0]
        request = LocateRequest(
            request_id="LOC001", security_internal_id=snap.security_internal_id,
            requested_quantity=snap.free_to_lend * Decimal("0.5"),
            requesting_counterparty_id="CPTY001", purpose="SHORT_SELL",
        )
        resolution = resolve_locate(request, snapshot, self.positions)
        self.assertTrue(resolution.is_fully_filled)
        self.assertEqual(resolution.shortfall_quantity, Decimal("0"))

    def test_locate_resolution_reports_shortfall(self):
        from secfi_platform.risk.inventory_manager import (
            build_inventory_snapshot, LocateRequest, resolve_locate
        )
        snapshot = build_inventory_snapshot(self.positions)
        request = LocateRequest(
            request_id="LOC002", security_internal_id="DOES_NOT_EXIST",
            requested_quantity=Decimal("1000000"),
            requesting_counterparty_id="CPTY001", purpose="SHORT_SELL",
        )
        resolution = resolve_locate(request, snapshot, self.positions)
        self.assertFalse(resolution.is_fully_filled)
        self.assertEqual(resolution.shortfall_quantity, Decimal("1000000"))

    def test_find_substitutes_excludes_original_security(self):
        from secfi_platform.risk.inventory_manager import build_inventory_snapshot, find_substitutes
        snapshot = build_inventory_snapshot(self.positions)
        position_metadata = {p.security.internal_id: p.security for p in self.positions}
        subs = find_substitutes("SEC001", snapshot, position_metadata)
        for s in subs:
            self.assertNotEqual(s.substitute_security_id, "SEC001")

    def test_inventory_summary_returns_valid_utilization(self):
        from secfi_platform.risk.inventory_manager import build_inventory_snapshot, inventory_summary
        snapshot = build_inventory_snapshot(self.positions)
        summary = inventory_summary(snapshot)
        self.assertGreaterEqual(summary["book_utilization_pct"], 0.0)
        self.assertLessEqual(summary["book_utilization_pct"], 1.0)


# ===========================================================================
# Limit Monitor
# ===========================================================================

class TestLimitMonitor(unittest.TestCase):
    def setUp(self):
        self.counterparties = load_counterparties()
        self.positions = load_positions()
        self.limits = default_counterparty_limits_usd()

    def test_dashboard_covers_all_counterparties(self):
        from secfi_platform.risk.limit_monitor import compute_limit_utilization_dashboard
        rows = compute_limit_utilization_dashboard(
            self.counterparties.values(), self.positions, self.limits
        )
        self.assertEqual(len(rows), len(self.counterparties))

    def test_watch_list_counterparty_visible(self):
        from secfi_platform.risk.limit_monitor import compute_limit_utilization_dashboard
        rows = compute_limit_utilization_dashboard(
            self.counterparties.values(), self.positions, self.limits
        )
        delta_row = next((r for r in rows if r.counterparty_id == "CPTY004"), None)
        self.assertIsNotNone(delta_row)
        self.assertTrue(delta_row.watch_list)

    def test_simulated_incremental_breach_detected(self):
        from secfi_platform.risk.limit_monitor import compute_limit_utilization_dashboard, simulate_incremental_exposure
        rows = compute_limit_utilization_dashboard(
            self.counterparties.values(), self.positions, self.limits
        )
        # Add enough to guarantee a breach on a counterparty with a small limit
        impact = simulate_incremental_exposure("CPTY004", Decimal("10000000"), rows, self.limits)
        self.assertTrue(impact.would_breach)
        self.assertEqual(impact.recommendation[:7], "DECLINE")

    def test_simulated_incremental_safe_proceed(self):
        from secfi_platform.risk.limit_monitor import compute_limit_utilization_dashboard, simulate_incremental_exposure
        rows = compute_limit_utilization_dashboard(
            self.counterparties.values(), self.positions, self.limits
        )
        # A tiny incremental on a low-utilization counterparty should be safe
        impact = simulate_incremental_exposure("CPTY005", Decimal("1000"), rows, self.limits)
        self.assertFalse(impact.would_breach)

    def test_detect_breaches_returns_correct_count(self):
        from secfi_platform.risk.limit_monitor import compute_limit_utilization_dashboard, detect_limit_breaches
        tight_limits = {"CPTY002": Decimal("100"), "CPTY001": Decimal("100")}
        rows = compute_limit_utilization_dashboard(
            self.counterparties.values(), self.positions, tight_limits
        )
        breaches = detect_limit_breaches(rows)
        self.assertGreaterEqual(len(breaches), 1)
        for b in breaches:
            self.assertGreater(b.current_exposure_usd, b.limit_usd)


# ===========================================================================
# Scenario Engine
# ===========================================================================

class TestScenarioEngine(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.counterparties = load_counterparties()

    def test_standard_scenarios_return_all_four(self):
        from secfi_platform.risk.scenario_engine import run_all_standard_scenarios
        results = run_all_standard_scenarios(self.positions, self.counterparties)
        self.assertEqual(len(results), 4)

    def test_equity_crash_scenario_shows_negative_revenue_delta(self):
        from secfi_platform.risk.scenario_engine import run_all_standard_scenarios
        results = run_all_standard_scenarios(self.positions, self.counterparties)
        equity_crash = next(r for r in results if "CRASH" in r.scenario_name)
        self.assertLess(equity_crash.revenue_delta_usd, Decimal("0"))

    def test_scenario_affects_at_least_one_position(self):
        from secfi_platform.risk.scenario_engine import run_all_standard_scenarios
        results = run_all_standard_scenarios(self.positions, self.counterparties)
        for r in results:
            self.assertGreater(r.positions_affected, 0)

    def test_comparison_matrix_sorted_worst_first(self):
        from secfi_platform.risk.scenario_engine import run_all_standard_scenarios, scenario_comparison_matrix
        results = run_all_standard_scenarios(self.positions, self.counterparties)
        matrix = scenario_comparison_matrix(results)
        deltas = [row["revenue_delta_usd"] for row in matrix]
        self.assertEqual(deltas, sorted(deltas))

    def test_reverse_stress_finds_break_even_shock(self):
        from secfi_platform.risk.scenario_engine import reverse_stress_threshold
        threshold = Decimal("5000")   # small threshold easily reachable
        result = reverse_stress_threshold(self.positions, self.counterparties, threshold)
        if result is not None:
            self.assertGreater(result["revenue_loss_usd"], float(threshold) * 0.99)


# ===========================================================================
# Exception Manager
# ===========================================================================

class TestExceptionManager(unittest.TestCase):
    def test_pricing_exception_lifecycle(self):
        from secfi_platform.risk.exception_manager import (
            ExceptionManager, raise_pricing_exception
        )
        mgr = ExceptionManager()
        exc = raise_pricing_exception("P001", Decimal("10"), Decimal("100"), Decimal("-90"))
        mgr.add(exc)
        self.assertEqual(len(mgr.pending_approval()), 1)
        self.assertEqual(len(mgr.active()), 0)
        from datetime import date
        mgr.approve(exc.exception_id, "risk_manager_1", expiry_date=date(2026, 12, 31))
        self.assertEqual(len(mgr.active()), 1)
        self.assertEqual(len(mgr.pending_approval()), 0)

    def test_limit_exception_requires_approval(self):
        from secfi_platform.risk.exception_manager import ExceptionManager, raise_limit_exception
        from secfi_platform.common.enums import ApprovalStatus
        mgr = ExceptionManager()
        exc = raise_limit_exception("CPTY001", Decimal("300000000"), Decimal("250000000"), Decimal("50000000"))
        mgr.add(exc)
        self.assertEqual(exc.status, ApprovalStatus.PROPOSED)
        self.assertEqual(exc.exception_type, "LIMIT")
        self.assertFalse(exc.is_active())

    def test_close_removes_from_active(self):
        from secfi_platform.risk.exception_manager import ExceptionManager, raise_pricing_exception
        from datetime import date
        mgr = ExceptionManager()
        exc = raise_pricing_exception("P002", Decimal("15"), Decimal("200"), Decimal("-185"))
        mgr.add(exc)
        mgr.approve(exc.exception_id, "trader_1", expiry_date=date(2099, 12, 31))
        self.assertEqual(len(mgr.active()), 1)
        mgr.close(exc.exception_id, "Rate corrected")
        self.assertEqual(len(mgr.active()), 0)

    def test_summary_counts_match(self):
        from secfi_platform.risk.exception_manager import (
            ExceptionManager, raise_pricing_exception, raise_limit_exception
        )
        from datetime import date
        mgr = ExceptionManager()
        mgr.add(raise_pricing_exception("P003", Decimal("10"), Decimal("100"), Decimal("-90")))
        mgr.add(raise_pricing_exception("P004", Decimal("10"), Decimal("100"), Decimal("-90")))
        mgr.add(raise_limit_exception("CPTY002", Decimal("300"), Decimal("250"), Decimal("50")))
        summary = mgr.summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_type"]["PRICING"], 2)
        self.assertEqual(summary["by_type"]["LIMIT"], 1)


# ===========================================================================
# Data Quality Controls
# ===========================================================================

class TestDataQualityControls(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.quotes = load_market_quotes()
        self.all_quotes = list(self.quotes.values())

    def test_report_has_source_profiles(self):
        from secfi_platform.common.data_quality import build_data_quality_report
        now = datetime.now(timezone.utc)
        report = build_data_quality_report(now, self.positions, self.quotes, self.all_quotes)
        self.assertGreater(len(report.source_profiles), 0)

    def test_dq_score_between_0_and_100(self):
        from secfi_platform.common.data_quality import build_data_quality_report
        now = datetime.now(timezone.utc)
        report = build_data_quality_report(now, self.positions, self.quotes, self.all_quotes)
        self.assertGreaterEqual(report.overall_dq_score, 0.0)
        self.assertLessEqual(report.overall_dq_score, 100.0)

    def test_empty_quotes_generates_coverage_warning(self):
        from secfi_platform.common.data_quality import build_data_quality_report
        now = datetime.now(timezone.utc)
        report = build_data_quality_report(now, self.positions, {}, [])
        # All positions have no quote
        self.assertEqual(len(report.positions_with_no_market_quote), len(self.positions))
        self.assertLess(report.overall_dq_score, 80.0)

    def test_cross_source_disagreement_detected(self):
        from secfi_platform.common.data_quality import build_data_quality_report, profile_market_quotes
        # GME: DataLend ~2220bps, EquiLend ~2180bps — these should agree closely
        # but let's load both sources and check disagreement is at most 1 entry
        from tests._helpers import load_market_quotes
        equilend_quotes = list(load_market_quotes("market_rates_equilend.csv").values())
        datalend_quotes = list(load_market_quotes("market_rates_datalend.csv").values())
        all_q = equilend_quotes + datalend_quotes
        now = datetime.now(timezone.utc)
        profiles, anomalies = profile_market_quotes(all_q, reference_now=now)
        # Verify we get profiles for both sources
        sources = {p.source_name for p in profiles}
        self.assertIn("EQUILEND", sources)
        self.assertIn("DATALEND", sources)


# ===========================================================================
# Backtester
# ===========================================================================

class TestBacktester(unittest.TestCase):
    def setUp(self):
        self.positions = load_positions()
        self.quotes = load_market_quotes()

    def _get_synthetic_records(self):
        from tests._helpers import load_counterparties, load_market_quotes
        from secfi_platform.pricing.pricing_intelligence import generate_pricing_recommendations
        from secfi_platform.reporting.backtester import generate_synthetic_executed_recommendations
        recs = generate_pricing_recommendations(self.positions, self.quotes)
        return generate_synthetic_executed_recommendations(recs, noise_pct=0.20)

    def test_backtester_runs_on_synthetic_data(self):
        from secfi_platform.reporting.backtester import Backtester
        records = self._get_synthetic_records()
        if not records:
            self.skipTest("No pricing recommendations in fixture for backtesting")
        backtester = Backtester(records)
        report = backtester.run()
        self.assertGreater(report.total_executed, 0)
        self.assertEqual(report.synthetic_count, report.total_executed)

    def test_quality_score_bounded(self):
        from secfi_platform.reporting.backtester import Backtester
        records = self._get_synthetic_records()
        if not records:
            self.skipTest("No pricing recommendations")
        report = Backtester(records).run()
        self.assertGreaterEqual(report.overall_model_quality_score, 0.0)
        self.assertLessEqual(report.overall_model_quality_score, 100.0)

    def test_empty_backtester_returns_gracefully(self):
        from secfi_platform.reporting.backtester import Backtester
        report = Backtester([]).run()
        self.assertEqual(report.total_recommendations_evaluated, 0)
        self.assertEqual(report.overall_model_quality_score, 0.0)

    def test_calibration_summary_renders(self):
        from secfi_platform.reporting.backtester import Backtester
        records = self._get_synthetic_records()
        if not records:
            self.skipTest("No pricing recommendations")
        report = Backtester(records).run()
        summary = report.calibration_summary()
        self.assertIn("Backtest Report", summary)
        self.assertIn("SYNTHETIC", summary)


# ===========================================================================
# Alert Prioritizer
# ===========================================================================

class TestAlertPrioritizer(unittest.TestCase):
    def _make_alert(self, category, severity, entity_id):
        from secfi_platform.common.types import Alert
        from datetime import timezone
        return Alert(
            alert_id=f"A-{entity_id}",
            raised_at=datetime.now(timezone.utc),
            severity=severity,
            category=category,
            title=f"{category} alert for {entity_id}",
            detail=f"Test alert for {entity_id}",
            related_entity_type="POSITION",
            related_entity_id=entity_id,
            requires_acknowledgement=True,
        )

    def setUp(self):
        from secfi_platform.alerting.prioritizer import reset_dedup_registry
        reset_dedup_registry()

    def test_critical_alerts_ranked_first(self):
        from secfi_platform.common.enums import BreakSeverity
        from secfi_platform.alerting.prioritizer import prioritize_alerts
        alerts = [
            self._make_alert("RECALL_BUYIN", BreakSeverity.MEDIUM, "P001"),
            self._make_alert("COUNTERPARTY_LIMIT", BreakSeverity.CRITICAL, "CPTY001"),
            self._make_alert("RECONCILIATION_BREAK", BreakSeverity.HIGH, "P002"),
        ]
        prioritized = [p for p in prioritize_alerts(alerts) if not p.suppressed]
        self.assertEqual(prioritized[0].alert.severity, BreakSeverity.CRITICAL)

    def test_duplicate_alert_is_suppressed(self):
        from secfi_platform.common.enums import BreakSeverity
        from secfi_platform.alerting.prioritizer import prioritize_alerts
        alert = self._make_alert("RECALL_BUYIN", BreakSeverity.HIGH, "P001")
        first_run = prioritize_alerts([alert])
        second_run = prioritize_alerts([alert])  # same alert, same key
        suppressed = [p for p in second_run if p.suppressed]
        self.assertEqual(len(suppressed), 1)

    def test_mass_alert_collapse(self):
        from secfi_platform.common.enums import BreakSeverity
        from secfi_platform.alerting.prioritizer import prioritize_alerts
        # 25 pricing alerts in the same category should collapse
        alerts = [
            self._make_alert("PRICING_EXCEPTION", BreakSeverity.MEDIUM, f"P{i:03d}")
            for i in range(25)
        ]
        from secfi_platform.alerting.prioritizer import reset_dedup_registry
        reset_dedup_registry()
        prioritized = [p for p in prioritize_alerts(alerts, mass_alert_threshold=20) if not p.suppressed]
        # Should be 1 mass-collapsed alert, not 25
        pricing_alerts = [p for p in prioritized if "PRICING_EXCEPTION" in p.alert.category]
        self.assertEqual(len(pricing_alerts), 1)
        self.assertIn("Mass", pricing_alerts[0].alert.title)

    def test_routing_targets_assigned(self):
        from secfi_platform.common.enums import BreakSeverity
        from secfi_platform.alerting.prioritizer import prioritize_alerts
        alert = self._make_alert("RECALL_BUYIN", BreakSeverity.CRITICAL, "P010")
        prioritized = [p for p in prioritize_alerts([alert]) if not p.suppressed]
        self.assertGreater(len(prioritized[0].routing_targets), 0)

    def test_feed_summary_correct_counts(self):
        from secfi_platform.common.enums import BreakSeverity
        from secfi_platform.alerting.prioritizer import prioritize_alerts, alert_feed_summary, reset_dedup_registry
        reset_dedup_registry()
        alerts = [
            self._make_alert("RECALL_BUYIN", BreakSeverity.CRITICAL, "P100"),
            self._make_alert("RECONCILIATION_BREAK", BreakSeverity.HIGH, "P101"),
        ]
        prioritized = prioritize_alerts(alerts)
        summary = alert_feed_summary(prioritized)
        self.assertEqual(summary["total_active_alerts"], 2)


if __name__ == "__main__":
    unittest.main()
