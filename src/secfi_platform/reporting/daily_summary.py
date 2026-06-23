"""
Reporting layer.

Assembles outputs from every engine into the desk-facing artifacts listed
in section L of the spec: daily executive summary, recommendation queues,
heatmaps (as structured data — actual rendering is the API/UI layer's
job), and drill-down structures by desk/entity/counterparty/product.

This module is deliberately "dumb" — it does not compute anything new,
it only aggregates and ranks. All actual analytics live in their owning
engine module. This separation means the reporting format can change
(new dashboard, new export format) without touching any risk/pricing/
optimization logic, and the analytics can be unit-tested independently
of any reporting concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from secfi_platform.explainability.explain import rank_recommendations


@dataclass
class DailyExecutiveSummary:
    as_of: date
    generated_at: datetime
    book_nmv_usd: Decimal
    total_lend_mv_usd: Decimal
    total_borrow_mv_usd: Decimal
    total_gross_exposure_usd: Decimal
    counterparties_at_or_over_limit: int
    open_critical_recon_breaks: int
    recall_buyin_queue_top: list                 # top N UrgencyQueueRow-derived summaries
    corporate_action_watchlist_top: list
    top_pricing_opportunities: list               # top N Recommendation from pricing engine
    top_optimization_recommendations: list
    top_growth_opportunities: list
    open_alerts_by_severity: dict
    total_estimated_pnl_opportunity_usd: Decimal
    data_quality_notes: list


def build_daily_executive_summary(
    *,
    as_of: date,
    positions,
    counterparty_exposures: dict,
    recon_breaks: list,
    recall_queue: list,
    ca_watchlist: list,
    pricing_recommendations: list,
    optimization_result,
    growth_opportunities,
    alerts: list,
    top_n: int = 10,
) -> DailyExecutiveSummary:
    from secfi_platform.common.enums import Direction

    positions = list(positions)
    book_nmv = sum((p.market_value for p in positions), Decimal("0"))
    lend_mv = sum((p.market_value for p in positions if p.direction in (Direction.LEND, Direction.REVERSE_REPO)), Decimal("0"))
    borrow_mv = sum((p.market_value for p in positions if p.direction in (Direction.BORROW, Direction.REPO)), Decimal("0"))
    total_gross = sum((e.gross_exposure_usd for e in counterparty_exposures.values()), Decimal("0"))
    breached = sum(1 for e in counterparty_exposures.values() if e.limit_breached)
    critical_breaks = sum(1 for b in recon_breaks if b["severity"].value == "CRITICAL")

    all_recs = []
    all_recs.extend(pricing_recommendations)
    if optimization_result is not None:
        all_recs.extend(optimization_result.recommendations)
    all_recs.extend(growth_opportunities)
    pnl_opportunity = sum(
        (r.estimated_pnl_impact_usd for r in all_recs if r.estimated_pnl_impact_usd and r.estimated_pnl_impact_usd > 0),
        Decimal("0"),
    )

    alerts_by_sev: dict = {}
    for a in alerts:
        alerts_by_sev[a.severity.value] = alerts_by_sev.get(a.severity.value, 0) + 1

    data_quality_notes = []
    stale_positions = sum(1 for p in positions if p.data_quality_flag.value != "OK")
    if stale_positions:
        data_quality_notes.append(f"{stale_positions} position(s) carry a non-OK data quality flag.")

    return DailyExecutiveSummary(
        as_of=as_of,
        generated_at=datetime.now(timezone.utc),
        book_nmv_usd=book_nmv,
        total_lend_mv_usd=lend_mv,
        total_borrow_mv_usd=borrow_mv,
        total_gross_exposure_usd=total_gross,
        counterparties_at_or_over_limit=breached,
        open_critical_recon_breaks=critical_breaks,
        recall_buyin_queue_top=recall_queue[:top_n],
        corporate_action_watchlist_top=ca_watchlist[:top_n],
        top_pricing_opportunities=rank_recommendations(pricing_recommendations)[:top_n],
        top_optimization_recommendations=(
            rank_recommendations(optimization_result.recommendations)[:top_n] if optimization_result else []
        ),
        top_growth_opportunities=rank_recommendations(growth_opportunities)[:top_n],
        open_alerts_by_severity=alerts_by_sev,
        total_estimated_pnl_opportunity_usd=pnl_opportunity,
        data_quality_notes=data_quality_notes,
    )


def render_markdown(summary: DailyExecutiveSummary) -> str:
    """Renders the executive summary to Markdown for email/Slack/PDF distribution."""
    lines = [
        f"# Securities Finance Desk — Daily Executive Summary",
        f"**As of:** {summary.as_of.isoformat()}  ",
        f"**Generated:** {summary.generated_at.isoformat()}",
        "",
        "## Book Snapshot",
        f"- Book NMV: ${summary.book_nmv_usd:,.0f}",
        f"- On-loan / financing-out MV: ${summary.total_lend_mv_usd:,.0f}",
        f"- On-borrow / financing-in MV: ${summary.total_borrow_mv_usd:,.0f}",
        f"- Total gross counterparty exposure: ${summary.total_gross_exposure_usd:,.0f}",
        f"- Counterparties at/over limit: {summary.counterparties_at_or_over_limit}",
        f"- Open CRITICAL reconciliation breaks: {summary.open_critical_recon_breaks}",
        f"- Estimated total P&L opportunity in queue: ${summary.total_estimated_pnl_opportunity_usd:,.0f}",
        "",
        "## Open Alerts by Severity",
    ]
    for sev, count in summary.open_alerts_by_severity.items():
        lines.append(f"- {sev}: {count}")

    lines.append("")
    lines.append("## Top Recall / Buy-In Risk Items")
    for row in summary.recall_buyin_queue_top:
        lines.append(f"- **{row.ticker}** ({row.position_id}) — buy-in risk {row.buyin_risk_score:.0f}, "
                      f"urgency {row.urgency_score:.0f}, action: {row.recommended_action.value}")

    lines.append("")
    lines.append("## Corporate Action Watchlist (Top)")
    for impact in summary.corporate_action_watchlist_top:
        lines.append(f"- **{impact.event.action_type.value}** on {impact.event.security_internal_id} — "
                      f"composite risk {impact.composite_risk_score:.0f}, urgency {impact.urgency.value}")

    lines.append("")
    lines.append("## Top Pricing Opportunities")
    for rec in summary.top_pricing_opportunities:
        pnl = rec.estimated_pnl_impact_usd or Decimal("0")
        lines.append(f"- {rec.target_id}: {rec.action.value}, est. P&L ${pnl:,.0f}, confidence {rec.confidence:.0%}")

    lines.append("")
    lines.append("## Top Optimization Recommendations")
    for rec in summary.top_optimization_recommendations:
        pnl = rec.estimated_pnl_impact_usd or Decimal("0")
        lines.append(f"- {rec.target_id}: {rec.action.value}, est. P&L ${pnl:,.0f}, confidence {rec.confidence:.0%}")

    lines.append("")
    lines.append("## Top Counterparty Growth/Contraction Opportunities")
    for rec in summary.top_growth_opportunities:
        lines.append(f"- {rec.target_id}: {rec.action.value}, confidence {rec.confidence:.0%}")

    if summary.data_quality_notes:
        lines.append("")
        lines.append("## Data Quality Notes")
        for note in summary.data_quality_notes:
            lines.append(f"- {note}")

    return "\n".join(lines)
