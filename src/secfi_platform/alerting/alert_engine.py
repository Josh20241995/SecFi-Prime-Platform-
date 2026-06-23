"""
Alerting engine.

Converts engine outputs (exposures, breaks, watchlist items, urgency
queue rows, recommendations) into Alert objects when configured
thresholds are crossed. Alerts are distinct from Recommendations:
  - A Recommendation proposes a desk ACTION and requires approval.
  - An Alert notifies a HUMAN that something needs attention; it may or
    may not have an associated Recommendation.

Alert routing (which team sees which category) is configured in
configs/base.yaml under `alerting.routing` and is intentionally kept out
of code — compliance/risk frequently need to adjust routing without a
code deploy, and this keeps that change in the configuration-control
process rather than the SDLC release process. See docs/governance.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import BreakSeverity
from secfi_platform.common.types import Alert


@dataclass(frozen=True)
class AlertThresholds:
    limit_utilization_warn_pct: float = 0.85
    limit_utilization_breach_pct: float = 1.00
    stale_data_minutes: float = 240.0
    recall_urgency_alert_score: float = 70.0
    buyin_risk_alert_score: float = 70.0
    corporate_action_immediate_alert: bool = True


def alert_from_limit_breach(counterparty_id: str, utilization_pct: float, limit_usd: Decimal,
                             thresholds: AlertThresholds = AlertThresholds()) -> Optional[Alert]:
    if utilization_pct >= thresholds.limit_utilization_breach_pct:
        severity = BreakSeverity.CRITICAL
        title = f"Counterparty limit BREACHED: {counterparty_id}"
    elif utilization_pct >= thresholds.limit_utilization_warn_pct:
        severity = BreakSeverity.HIGH
        title = f"Counterparty limit approaching: {counterparty_id}"
    else:
        return None

    return Alert(
        alert_id=str(uuid.uuid4()),
        raised_at=datetime.now(timezone.utc),
        severity=severity,
        category="COUNTERPARTY_LIMIT",
        title=title,
        detail=f"Utilization at {utilization_pct:.1%} of ${limit_usd:,.0f} limit.",
        related_entity_type="COUNTERPARTY",
        related_entity_id=counterparty_id,
        requires_acknowledgement=True,
    )


def alert_from_stale_data(source_name: str, age_minutes: float,
                           thresholds: AlertThresholds = AlertThresholds()) -> Optional[Alert]:
    if age_minutes < thresholds.stale_data_minutes:
        return None
    return Alert(
        alert_id=str(uuid.uuid4()),
        raised_at=datetime.now(timezone.utc),
        severity=BreakSeverity.MEDIUM if age_minutes < thresholds.stale_data_minutes * 2 else BreakSeverity.HIGH,
        category="DATA_FRESHNESS",
        title=f"Stale data: {source_name}",
        detail=f"Source '{source_name}' has not refreshed in {age_minutes:.0f} minutes "
               f"(threshold {thresholds.stale_data_minutes:.0f}). Downstream confidence scores are "
               f"being penalized; see explainability/explain.py staleness_penalty.",
        related_entity_type="DATA_SOURCE",
        related_entity_id=source_name,
        requires_acknowledgement=True,
    )


def alert_from_recall_queue_row(row, thresholds: AlertThresholds = AlertThresholds()) -> Optional[Alert]:
    if row.buyin_risk_score < thresholds.buyin_risk_alert_score and row.urgency_score < thresholds.recall_urgency_alert_score:
        return None
    severity = BreakSeverity.CRITICAL if row.buyin_risk_score >= 85 else BreakSeverity.HIGH
    return Alert(
        alert_id=str(uuid.uuid4()),
        raised_at=datetime.now(timezone.utc),
        severity=severity,
        category="RECALL_BUYIN",
        title=f"Buy-in risk on {row.ticker}",
        detail=f"Buy-in risk score {row.buyin_risk_score:.0f}/100, urgency {row.urgency_score:.0f}/100. "
               f"Recommended action: {row.recommended_action.value}. Drivers: {'; '.join(row.drivers)}",
        related_entity_type="POSITION",
        related_entity_id=row.position_id,
        requires_acknowledgement=True,
    )


def alert_from_recon_break(brk: dict, thresholds: AlertThresholds = AlertThresholds()) -> Optional[Alert]:
    if brk["severity"] not in (BreakSeverity.HIGH, BreakSeverity.CRITICAL):
        return None
    return Alert(
        alert_id=str(uuid.uuid4()),
        raised_at=datetime.now(timezone.utc),
        severity=brk["severity"],
        category="RECONCILIATION_BREAK",
        title=f"{brk['severity'].value} break: {brk['break_type'].value}",
        detail=brk["probable_root_cause"] + " Recommended: " + brk["recommended_action"],
        related_entity_type="POSITION" if brk.get("position_id") else "SECURITY",
        related_entity_id=brk.get("position_id") or brk.get("security_internal_id") or "UNKNOWN",
        requires_acknowledgement=True,
    )


def alert_from_corporate_action(impact, thresholds: AlertThresholds = AlertThresholds()) -> Optional[Alert]:
    from secfi_platform.common.enums import ActionUrgency

    if impact.urgency not in (ActionUrgency.IMMEDIATE, ActionUrgency.ACT_TODAY):
        return None
    return Alert(
        alert_id=str(uuid.uuid4()),
        raised_at=datetime.now(timezone.utc),
        severity=BreakSeverity.CRITICAL if impact.urgency == ActionUrgency.IMMEDIATE else BreakSeverity.HIGH,
        category="CORPORATE_ACTION",
        title=f"{impact.event.action_type.value} — {impact.urgency.value}",
        detail=f"Composite risk score {impact.composite_risk_score:.0f}/100, "
               f"{len(impact.affected_position_ids)} position(s) affected, "
               f"${impact.affected_market_value_usd:,.0f} market value.",
        related_entity_type="SECURITY",
        related_entity_id=impact.event.security_internal_id,
        requires_acknowledgement=True,
    )


def collect_alerts(*alert_lists: Iterable[Optional[Alert]]) -> list:
    out = []
    for lst in alert_lists:
        for a in lst:
            if a is not None:
                out.append(a)
    severity_rank = {BreakSeverity.CRITICAL: 3, BreakSeverity.HIGH: 2, BreakSeverity.MEDIUM: 1, BreakSeverity.LOW: 0}
    out.sort(key=lambda a: severity_rank[a.severity], reverse=True)
    return out
