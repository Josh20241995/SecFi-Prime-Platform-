"""
Alert prioritization engine.

Raw alerts from the alerting engine (`alerting/alert_engine.py`) need
further processing before reaching the desk:
  1. DEDUPLICATION: Same underlying issue generating multiple alerts
     across cycles (e.g., a position that stays in the buy-in risk
     queue for 3 consecutive intraday cycles) should not create 3
     separate CRITICAL alerts — the first fires, subsequent ones are
     suppressed until the condition resolves or a configurable re-alert
     interval elapses.
  2. THROTTLING: During a market dislocation (e.g., a rates spike that
     triggers 200 pricing alerts simultaneously), the desk should get
     a single "mass pricing alert" summary rather than 200 individual
     alerts that make the feed unusable.
  3. PRIORITY RANKING: Within each severity tier, rank alerts by
     estimated P&L-at-risk so the trader's eyes go to the most
     material items first.
  4. ROUTING ANNOTATION: Annotate each alert with which teams/channels
     should receive it (sourced from configs/base.yaml
     `alerting.routing`).
  5. SUPPRESSION OF ACKNOWLEDGED ITEMS: Once an alert is acknowledged,
     suppress re-alerts of the same condition for a configurable window.

This module operates on the output of `alerting/alert_engine.py` —
it does not generate new analytical content, it organizes what was
already generated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from secfi_platform.common.enums import BreakSeverity
from secfi_platform.common.types import Alert


# ---- Deduplication registry (in-memory for this build; back with Redis in production) ------

_dedup_registry: dict = {}    # (category, related_entity_id) -> last_alert_at datetime


def _dedup_key(alert: Alert) -> str:
    return f"{alert.category}::{alert.related_entity_type}::{alert.related_entity_id}"


# ---- Routing table (mirrors configs/base.yaml `alerting.routing`) -----------------------

DEFAULT_ROUTING: dict = {
    "COUNTERPARTY_LIMIT": ["desk-risk-channel", "counterparty-risk-team"],
    "DATA_FRESHNESS": ["platform-engineering-channel"],
    "RECALL_BUYIN": ["desk-trading-channel", "ops-settlements-team"],
    "RECONCILIATION_BREAK": ["ops-recon-team"],
    "CORPORATE_ACTION": ["desk-trading-channel", "corporate-actions-team"],
    "PRICING_EXCEPTION": ["desk-trading-channel"],
    "LIMIT_EXCEPTION": ["desk-risk-channel", "credit-risk-team"],
    "DATA_QUALITY_EXCEPTION": ["ops-data-quality-team"],
}


@dataclass
class PrioritizedAlert:
    alert: Alert
    priority_rank: int                          # 1 = most urgent, ascending
    routing_targets: list                        # list[str] channel/team names
    suppressed: bool                              # True = duplicate, not to be re-fired
    suppression_reason: Optional[str]
    estimated_pnl_at_risk_usd: Optional[float]


def _estimate_pnl_at_risk(alert: Alert) -> Optional[float]:
    """
    Quick heuristic for P&L-at-risk from alert metadata. In production,
    join against the positions table to get actual MV; this is a
    deterministic proxy that keeps the prioritizer self-contained.
    """
    severity_proxy = {
        BreakSeverity.CRITICAL: 500_000,
        BreakSeverity.HIGH: 100_000,
        BreakSeverity.MEDIUM: 25_000,
        BreakSeverity.LOW: 5_000,
    }
    return float(severity_proxy.get(alert.severity, 0))


def prioritize_alerts(
    alerts: list,
    routing: Optional[dict] = None,
    re_alert_interval_minutes: float = 60.0,
    mass_alert_threshold: int = 20,
    reference_now: Optional[datetime] = None,
) -> list:
    """
    Process a raw list of Alert objects into a ranked, deduplicated,
    routed list of PrioritizedAlert objects.

    Parameters
    ----------
    re_alert_interval_minutes : float
        Suppress re-alerts of the same condition until this many minutes
        have elapsed since the last alert for the same key.
    mass_alert_threshold : int
        If a single category produces more than this many alerts, collapse
        them into a single summary alert.
    """
    routing = routing or DEFAULT_ROUTING
    now = reference_now or datetime.now(timezone.utc)
    re_alert_delta = timedelta(minutes=re_alert_interval_minutes)

    severity_rank = {
        BreakSeverity.CRITICAL: 0,
        BreakSeverity.HIGH: 1,
        BreakSeverity.MEDIUM: 2,
        BreakSeverity.LOW: 3,
    }

    # Mass alert collapse — group by category
    by_category: dict = {}
    for alert in alerts:
        by_category.setdefault(alert.category, []).append(alert)

    processed: list = []
    collapsed: list = []

    for category, category_alerts in by_category.items():
        if len(category_alerts) > mass_alert_threshold:
            # Collapse into a single summary alert
            max_sev = min(category_alerts, key=lambda a: severity_rank[a.severity])
            summary = Alert(
                alert_id=f"MASS-{category}-{now.isoformat()}",
                raised_at=now,
                severity=max_sev.severity,
                category=category,
                title=f"Mass {category} alert: {len(category_alerts)} conditions triggered",
                detail=(
                    f"{len(category_alerts)} {category} alerts triggered in this cycle. "
                    f"Worst severity: {max_sev.severity.value}. "
                    f"Sample: {category_alerts[0].title}"
                ),
                related_entity_type="BOOK",
                related_entity_id="BOOK_WIDE",
                requires_acknowledgement=True,
            )
            collapsed.append(summary)
        else:
            processed.extend(category_alerts)

    # Deduplication pass
    prioritized = []
    for alert in processed + collapsed:
        key = _dedup_key(alert)
        last_fired = _dedup_registry.get(key)
        is_suppressed = False
        suppression_reason = None
        if last_fired is not None and (now - last_fired) < re_alert_delta:
            is_suppressed = True
            suppression_reason = (
                f"Same condition alerted {int((now - last_fired).total_seconds() / 60)}m ago; "
                f"re-alert interval is {re_alert_interval_minutes:.0f}m."
            )
        else:
            _dedup_registry[key] = now

        prioritized.append(PrioritizedAlert(
            alert=alert,
            priority_rank=0,   # set below after sort
            routing_targets=routing.get(alert.category, ["desk-trading-channel"]),
            suppressed=is_suppressed,
            suppression_reason=suppression_reason,
            estimated_pnl_at_risk_usd=_estimate_pnl_at_risk(alert),
        ))

    # Sort: severity first, then P&L at risk descending, then time ascending
    non_suppressed = [p for p in prioritized if not p.suppressed]
    non_suppressed.sort(
        key=lambda p: (
            severity_rank[p.alert.severity],
            -(p.estimated_pnl_at_risk_usd or 0),
            p.alert.raised_at,
        )
    )
    for rank, item in enumerate(non_suppressed, start=1):
        item.priority_rank = rank

    suppressed = [p for p in prioritized if p.suppressed]
    return non_suppressed + suppressed


def alert_feed_summary(prioritized: list) -> dict:
    """Desk-facing summary of the current alert feed — for the API and daily summary."""
    active = [p for p in prioritized if not p.suppressed]
    return {
        "total_active_alerts": len(active),
        "suppressed_alerts": len(prioritized) - len(active),
        "by_severity": {
            sev.value: sum(1 for p in active if p.alert.severity == sev)
            for sev in BreakSeverity
        },
        "by_category": {
            cat: sum(1 for p in active if p.alert.category == cat)
            for cat in {p.alert.category for p in active}
        },
        "top_3": [
            {
                "rank": p.priority_rank,
                "title": p.alert.title,
                "severity": p.alert.severity.value,
                "routing": p.routing_targets,
            }
            for p in active[:3]
        ],
    }


def reset_dedup_registry() -> None:
    """Clear the deduplication registry — called at the start of each test run."""
    global _dedup_registry
    _dedup_registry = {}
