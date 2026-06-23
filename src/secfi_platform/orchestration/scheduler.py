"""
Orchestration layer.

Defines the logical job graph for a full platform cycle and provides a
reference, in-process runner (`run_full_cycle`) suitable for local
development, CI integration tests, and as the function a real scheduler
(Airflow, Dagster, Control-M — whichever the firm standardizes on) would
invoke as its task body. This module intentionally does NOT implement a
DAG scheduler itself; see docs/runbook.md "Scheduling" for how each job
below maps to a recommended cron/event trigger in production.

Job graph (sequential dependency order; '||' marks jobs that can run in
parallel once their inputs are ready):

  1. INGEST            ingestion/connectors.py  (all sources, in parallel ||)
  2. NORMALIZE          normalization/schema_mapping.py (depends on 1)
  3. RISK                risk/counterparty_risk.py, risk/capital_rwa.py,
                          risk/rates_fx.py   (depends on 2, run in parallel ||)
  4. RECONCILE           reconciliation/recon_engine.py (depends on 2)
  5. PRICE                pricing/pricing_intelligence.py (depends on 2)
  6. OPTIMIZE             optimization/book_optimizer.py (depends on 3, 5)
  7. RECALL_BUYIN         recall_buyin/recall_risk_engine.py (depends on 2, 5)
  8. CORPORATE_ACTIONS    corporate_actions/ca_impact_engine.py (depends on 2)
  9. GROWTH               growth/counterparty_growth.py (depends on 3)
  10. ALERT                alerting/alert_engine.py (depends on 3,4,6,7,8)
  11. REPORT               reporting/daily_summary.py (depends on all above)

Recommended scheduling cadence (see docs/runbook.md for full detail):
  - Full cycle (1-11): once at EOD batch close (captures custodian EOD
    feeds for reconciliation), plus a "fast cycle" (1,2,3,5,6,7 only —
    skips reconciliation since custodian data doesn't refresh intraday in
    most setups) every 15 minutes during trading hours.
  - Corporate actions (8): once daily pre-market; CA data does not need
    intraday refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from secfi_platform.alerting.alert_engine import (
    AlertThresholds,
    alert_from_corporate_action,
    alert_from_limit_breach,
    alert_from_recall_queue_row,
    alert_from_recon_break,
    collect_alerts,
)
from secfi_platform.common.logging_setup import get_logger, log_with_fields, new_correlation_id
from secfi_platform.corporate_actions.ca_impact_engine import (
    build_corporate_action_watchlist,
    watchlist_to_recommendations,
)
from secfi_platform.growth.counterparty_growth import (
    GrowthThresholds,
    assess_counterparty_opportunity,
    opportunities_to_recommendations,
)
from secfi_platform.optimization.book_optimizer import (
    OptimizationCandidate,
    OptimizationConstraints,
    optimize_book,
)
from secfi_platform.pricing.pricing_intelligence import classify_specialness, generate_pricing_recommendations
from secfi_platform.recall_buyin.recall_risk_engine import compute_urgency_queue, queue_to_recommendations
from secfi_platform.reconciliation.recon_engine import reconcile
from secfi_platform.reporting.daily_summary import build_daily_executive_summary
from secfi_platform.risk.capital_rwa import CapitalParameters, compute_counterparty_capital_summary
from secfi_platform.risk.counterparty_risk import compute_book_exposure_by_counterparty
from secfi_platform.risk.rates_fx import build_rates_fx_report

logger = get_logger(__name__)


@dataclass
class CycleInputs:
    as_of: date
    positions: list
    counterparties: dict                      # counterparty_id -> Counterparty
    market_quotes: dict                         # security_internal_id -> MarketRateQuote
    fx_rates: dict                                # currency -> FXRate
    corporate_action_events: list
    settlement_fails: list
    locate_shortages: list
    substitutes: dict
    counterparty_limits_usd: dict
    book_recon_df: Optional[object] = None
    custodian_recon_df: Optional[object] = None


@dataclass
class CycleOutputs:
    correlation_id: str
    positions: list                        # raw Position objects, stored for downstream ad-hoc queries
    counterparty_exposures: dict
    capital_summaries: dict
    rates_fx_report: object
    recon_breaks: list
    pricing_recommendations: list
    optimization_result: object
    recall_queue: list
    recall_recommendations: list
    ca_watchlist: list
    ca_recommendations: list
    growth_opportunities: list
    growth_recommendations: list
    alerts: list
    executive_summary: object


def run_full_cycle(
    inputs: CycleInputs,
    capital_params: CapitalParameters = CapitalParameters(),
    growth_thresholds: GrowthThresholds = GrowthThresholds(),
    alert_thresholds: AlertThresholds = AlertThresholds(),
    optimization_constraints: Optional[OptimizationConstraints] = None,
) -> CycleOutputs:
    cid = new_correlation_id()
    log_with_fields(logger, 20, "cycle.start", as_of=inputs.as_of.isoformat(), position_count=len(inputs.positions))

    # ---- 3. RISK ----------------------------------------------------------
    counterparty_exposures = compute_book_exposure_by_counterparty(
        inputs.counterparties.values(), inputs.positions,
        as_of=inputs.as_of.isoformat(), limits_by_counterparty_id=inputs.counterparty_limits_usd,
    )

    positions_by_cpty: dict = {}
    for p in inputs.positions:
        positions_by_cpty.setdefault(p.counterparty_id, []).append(p)

    capital_summaries = {
        cpty_id: compute_counterparty_capital_summary(cpty, positions_by_cpty.get(cpty_id, []), capital_params)
        for cpty_id, cpty in inputs.counterparties.items()
    }

    rates_fx_report = build_rates_fx_report(inputs.positions, inputs.fx_rates, inputs.as_of)

    # ---- 4. RECONCILE ------------------------------------------------------
    recon_breaks = []
    if inputs.book_recon_df is not None and inputs.custodian_recon_df is not None:
        recon_breaks = reconcile(inputs.book_recon_df, inputs.custodian_recon_df, "CUSTODIAN", inputs.as_of)

    critical_breaks_by_cpty: dict = {}
    for b in recon_breaks:
        if b["severity"].value == "CRITICAL" and b.get("counterparty_id"):
            critical_breaks_by_cpty[b["counterparty_id"]] = True

    # ---- 5. PRICE ------------------------------------------------------------
    pricing_recommendations = generate_pricing_recommendations(inputs.positions, inputs.market_quotes)

    # ---- 6. OPTIMIZE ----------------------------------------------------------
    market_rate_bps_lookup = {
        sec_id: (q.weighted_avg_fee_bps if q.weighted_avg_fee_bps is not None else q.avg_fee_bps)
        for sec_id, q in inputs.market_quotes.items()
        if (q.weighted_avg_fee_bps is not None or q.avg_fee_bps is not None)
    }
    optimization_constraints = optimization_constraints or OptimizationConstraints(
        counterparty_limits_usd=inputs.counterparty_limits_usd
    )
    candidates = [
        OptimizationCandidate(position=p, candidate_counterparties=tuple(
            cid_ for cid_ in inputs.counterparties if cid_ != p.counterparty_id
        )[:3])  # limit fan-out to top 3 alternative counterparties per position for tractability
        for p in inputs.positions
    ]
    optimization_result = optimize_book(
        candidates, inputs.counterparties, market_rate_bps_lookup, optimization_constraints, capital_params,
    )

    # ---- 7. RECALL / BUY-IN --------------------------------------------------
    specialness_by_security = {
        sec_id: classify_specialness(quote) for sec_id, quote in inputs.market_quotes.items()
    }

    ca_driven_return_ids = {
        e.security_internal_id for e in inputs.corporate_action_events
        if e.record_date and (e.record_date - inputs.as_of).days <= 5
    }

    recall_queue = compute_urgency_queue(
        inputs.positions, inputs.settlement_fails, inputs.locate_shortages,
        specialness_by_security, inputs.substitutes, ca_driven_return_ids,
    )
    recall_recommendations = queue_to_recommendations(recall_queue)

    # ---- 8. CORPORATE ACTIONS --------------------------------------------------
    ca_watchlist = build_corporate_action_watchlist(inputs.corporate_action_events, inputs.positions, inputs.as_of)
    ca_recommendations = watchlist_to_recommendations(ca_watchlist)

    # ---- 9. GROWTH ----------------------------------------------------------------
    growth_opportunities = [
        assess_counterparty_opportunity(
            cpty, capital_summaries[cpty_id], counterparty_exposures[cpty_id], growth_thresholds,
            has_unresolved_critical_breaks=critical_breaks_by_cpty.get(cpty_id, False),
        )
        for cpty_id, cpty in inputs.counterparties.items()
    ]
    growth_recommendations = opportunities_to_recommendations(growth_opportunities)

    # ---- 10. ALERTS -----------------------------------------------------------------
    limit_alerts = [
        alert_from_limit_breach(cpty_id, e.utilization_pct, e.limit_usd, alert_thresholds)
        for cpty_id, e in counterparty_exposures.items()
        if e.utilization_pct is not None
    ]
    recall_alerts = [alert_from_recall_queue_row(row, alert_thresholds) for row in recall_queue]
    recon_alerts = [alert_from_recon_break(b, alert_thresholds) for b in recon_breaks]
    ca_alerts = [alert_from_corporate_action(impact, alert_thresholds) for impact in ca_watchlist]
    alerts = collect_alerts(limit_alerts, recall_alerts, recon_alerts, ca_alerts)

    # ---- 11. REPORT -----------------------------------------------------------------
    executive_summary = build_daily_executive_summary(
        as_of=inputs.as_of,
        positions=inputs.positions,
        counterparty_exposures=counterparty_exposures,
        recon_breaks=recon_breaks,
        recall_queue=recall_queue,
        ca_watchlist=ca_watchlist,
        pricing_recommendations=pricing_recommendations,
        optimization_result=optimization_result,
        growth_opportunities=growth_recommendations,
        alerts=alerts,
    )

    log_with_fields(
        logger, 20, "cycle.complete", alerts_raised=len(alerts),
        recommendations_generated=(
            len(pricing_recommendations) + len(optimization_result.recommendations)
            + len(recall_recommendations) + len(ca_recommendations) + len(growth_recommendations)
        ),
    )

    return CycleOutputs(
        correlation_id=cid,
        positions=inputs.positions,
        counterparty_exposures=counterparty_exposures,
        capital_summaries=capital_summaries,
        rates_fx_report=rates_fx_report,
        recon_breaks=recon_breaks,
        pricing_recommendations=pricing_recommendations,
        optimization_result=optimization_result,
        recall_queue=recall_queue,
        recall_recommendations=recall_recommendations,
        ca_watchlist=ca_watchlist,
        ca_recommendations=ca_recommendations,
        growth_opportunities=growth_opportunities,
        growth_recommendations=growth_recommendations,
        alerts=alerts,
        executive_summary=executive_summary,
    )
