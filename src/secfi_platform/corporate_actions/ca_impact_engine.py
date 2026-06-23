"""
Corporate action impact engine.

Screens corporate actions over a forward window (default 60 days) and
scores each event's likely impact on:
  - borrow supply risk (will lendable supply shrink?)
  - recall risk (will lenders recall to participate/vote/elect?)
  - rate dislocation risk (will fee/specialness move sharply around the event?)
  - settlement fail risk (will settlement conventions around the event create fails?)
  - balance-sheet impact (will the position's economics or eligibility change?)

Event-type-specific heuristics (configurable in configs/base.yaml under
`corporate_actions.event_weights`):

  CASH_DIVIDEND        : recall risk spikes near record date (lenders often
                          recall to capture dividend/avoid manufactured
                          payment tax treatment); rate dislocation moderate.
  STOCK_DIVIDEND/SPLIT  : settlement fail risk elevated around effective
                          date due to ratio adjustment; quantity-mismatch
                          break risk elevated (see reconciliation engine).
  REVERSE_SPLIT          : high fail risk (fractional share handling),
                          high recall risk (HTB names often have reverse
                          splits as a precursor to delisting).
  MERGER/TENDER_OFFER    : supply risk very high (shares get tendered out
                          of lendable pools); recall risk high near election
                          deadline.
  SPIN_OFF                : balance-sheet impact high (new security created,
                          new eligibility/haircut/risk-weight needed); recall
                          risk high near record date.
  BOND_CALL/REDEMPTION    : supply risk high (called bonds disappear from
                          lendable pool); settlement risk high at call date.
  INDEX_REBALANCE          : supply/demand shift risk (additions often see
                          borrow demand spike pre-effective-date; deletions
                          often see lendable supply spike).

Urgency window mapping: ActionUrgency is derived from days-to-event AND
event severity score, not days alone — a low-impact event tomorrow is
less urgent than a high-impact event in 5 days.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import (
    ActionUrgency,
    ApprovalStatus,
    CorporateActionType,
    RecommendationAction,
)
from secfi_platform.common.types import CorporateActionEvent, Position, Recommendation
from secfi_platform.explainability.explain import (
    DataCompletenessReport,
    build_rationale,
    compute_confidence,
    compute_priority_score,
)


EVENT_IMPACT_WEIGHTS = {
    # action_type -> (supply_risk, recall_risk, rate_dislocation_risk, settlement_fail_risk, balance_sheet_impact)
    # each weight is 0-100, representing the BASE severity before proximity scaling.
    CorporateActionType.CASH_DIVIDEND: (20, 55, 35, 15, 10),
    CorporateActionType.STOCK_DIVIDEND: (35, 45, 30, 50, 30),
    CorporateActionType.SPLIT: (30, 30, 25, 55, 35),
    CorporateActionType.REVERSE_SPLIT: (60, 65, 55, 75, 60),
    CorporateActionType.MERGER: (80, 75, 60, 50, 70),
    CorporateActionType.TENDER_OFFER: (75, 80, 55, 45, 65),
    CorporateActionType.SPIN_OFF: (55, 70, 40, 55, 80),
    CorporateActionType.REDEMPTION: (70, 40, 30, 60, 55),
    CorporateActionType.BOND_CALL: (70, 35, 30, 60, 55),
    CorporateActionType.COUPON_PAYMENT: (10, 30, 15, 10, 5),
    CorporateActionType.UST_AUCTION_SETTLEMENT: (25, 15, 35, 30, 20),
    CorporateActionType.ADR_RATIO_CHANGE: (40, 40, 45, 50, 45),
    CorporateActionType.INDEX_REBALANCE: (50, 35, 60, 25, 30),
    CorporateActionType.VOLUNTARY_ELECTION: (45, 60, 30, 30, 40),
    CorporateActionType.MANDATORY_OTHER: (40, 40, 30, 35, 35),
}


@dataclass
class CorporateActionImpact:
    event: CorporateActionEvent
    affected_position_ids: list
    days_to_key_date: Optional[int]
    supply_risk_score: float
    recall_risk_score: float
    rate_dislocation_risk_score: float
    settlement_fail_risk_score: float
    balance_sheet_impact_score: float
    composite_risk_score: float
    urgency: ActionUrgency
    affected_market_value_usd: Decimal
    rationale: list


def _key_date(event: CorporateActionEvent) -> Optional[date]:
    for candidate in (event.record_date, event.ex_date, event.effective_date, event.election_deadline, event.payment_date):
        if candidate is not None:
            return candidate
    return None


def _proximity_multiplier(days_to_event: Optional[int]) -> float:
    if days_to_event is None:
        return 0.5
    if days_to_event < 0:
        return 0.3  # event already passed within window edge case; still informational
    if days_to_event <= 2:
        return 1.5
    if days_to_event <= 7:
        return 1.25
    if days_to_event <= 15:
        return 1.0
    if days_to_event <= 30:
        return 0.75
    return 0.5  # 31-60 days out: informational/monitor territory


def _urgency_from_scores(composite: float, days_to_event: Optional[int]) -> ActionUrgency:
    if days_to_event is not None and days_to_event <= 1 and composite >= 50:
        return ActionUrgency.IMMEDIATE
    if days_to_event is not None and days_to_event <= 3 and composite >= 40:
        return ActionUrgency.ACT_TODAY
    if composite >= 60:
        return ActionUrgency.ACT_THIS_WEEK
    if composite >= 30:
        return ActionUrgency.MONITOR
    return ActionUrgency.INFORMATIONAL


def assess_corporate_action(
    event: CorporateActionEvent,
    positions_for_security: Iterable[Position],
    as_of: date,
    window_days: int = 60,
) -> Optional[CorporateActionImpact]:
    key_date = _key_date(event)
    if key_date is not None:
        days_to_event = (key_date - as_of).days
        if days_to_event > window_days:
            return None
    else:
        days_to_event = None

    weights = EVENT_IMPACT_WEIGHTS.get(event.action_type, (30, 30, 30, 30, 30))
    mult = _proximity_multiplier(days_to_event)

    supply, recall, rate, settle, bs = (min(w * mult, 100.0) for w in weights)
    composite = round((supply * 0.25 + recall * 0.25 + rate * 0.2 + settle * 0.15 + bs * 0.15), 2)

    positions = list(positions_for_security)
    affected_mv = sum((p.market_value for p in positions), Decimal("0"))

    rationale = build_rationale(
        f"{event.action_type.value} event for security {event.security_internal_id}, "
        f"{'mandatory' if event.is_mandatory else 'voluntary'}, key date in "
        f"{days_to_event if days_to_event is not None else 'an unspecified number of'} day(s).",
        f"Affects {len(positions)} open position(s) totaling ${affected_mv:,.0f} market value.",
        f"Supply risk {supply:.0f}/100, recall risk {recall:.0f}/100, rate dislocation risk {rate:.0f}/100, "
        f"settlement fail risk {settle:.0f}/100, balance-sheet impact {bs:.0f}/100.",
        event.terms_summary,
    )

    return CorporateActionImpact(
        event=event,
        affected_position_ids=[p.position_id for p in positions],
        days_to_key_date=days_to_event,
        supply_risk_score=supply,
        recall_risk_score=recall,
        rate_dislocation_risk_score=rate,
        settlement_fail_risk_score=settle,
        balance_sheet_impact_score=bs,
        composite_risk_score=composite,
        urgency=_urgency_from_scores(composite, days_to_event),
        affected_market_value_usd=affected_mv,
        rationale=rationale,
    )


def build_corporate_action_watchlist(
    events: Iterable[CorporateActionEvent],
    positions: Iterable[Position],
    as_of: date,
    window_days: int = 60,
) -> list:
    positions = list(positions)
    by_security: dict = {}
    for p in positions:
        by_security.setdefault(p.security.internal_id, []).append(p)

    watchlist = []
    for event in events:
        impact = assess_corporate_action(
            event, by_security.get(event.security_internal_id, []), as_of, window_days
        )
        if impact is not None:
            watchlist.append(impact)

    watchlist.sort(key=lambda i: i.composite_risk_score, reverse=True)
    return watchlist


def watchlist_to_recommendations(watchlist: Iterable[CorporateActionImpact], as_of: Optional[datetime] = None) -> list:
    as_of = as_of or datetime.now(timezone.utc)
    recs = []
    for impact in watchlist:
        if impact.urgency in (ActionUrgency.INFORMATIONAL, ActionUrgency.MONITOR):
            continue
        if not impact.affected_position_ids:
            continue

        action = (
            RecommendationAction.RETURN if impact.recall_risk_score >= 60
            else RecommendationAction.HEDGE if impact.rate_dislocation_risk_score >= 50
            else RecommendationAction.DO_NOTHING
        )
        if action == RecommendationAction.DO_NOTHING:
            continue

        completeness = DataCompletenessReport(required_fields=3, present_and_valid_fields=3, fallback_fields=0)
        confidence = compute_confidence(completeness, model_certainty=impact.composite_risk_score / 100.0, data_age_minutes=60)
        priority = compute_priority_score(
            pnl_score_0_100=min(float(impact.affected_market_value_usd) / 1_000_000 * 10, 100),
            risk_score_0_100=impact.composite_risk_score,
            urgency_score_0_100={
                ActionUrgency.IMMEDIATE: 100, ActionUrgency.ACT_TODAY: 80,
                ActionUrgency.ACT_THIS_WEEK: 50,
            }.get(impact.urgency, 20),
            confidence_0_1=confidence,
        )

        for position_id in impact.affected_position_ids:
            recs.append(
                Recommendation(
                    recommendation_id=str(uuid.uuid4()),
                    generated_at=as_of,
                    source_engine="corporate_actions.ca_impact_engine",
                    action=action,
                    target_type="POSITION",
                    target_id=position_id,
                    quantity=None,
                    from_value=None,
                    to_value=None,
                    estimated_pnl_impact_usd=None,
                    estimated_capital_impact_usd=None,
                    estimated_rwa_impact_usd=None,
                    rationale=impact.rationale + [f"Urgency: {impact.urgency.value}."],
                    supporting_metrics={
                        "event_id": impact.event.event_id,
                        "action_type": impact.event.action_type.value,
                        "composite_risk_score": impact.composite_risk_score,
                        "days_to_key_date": impact.days_to_key_date,
                    },
                    confidence=confidence,
                    data_completeness_pct=completeness.completeness_pct,
                    priority_score=priority,
                    approval_status=ApprovalStatus.PROPOSED,
                )
            )
    recs.sort(key=lambda r: r.priority_score, reverse=True)
    return recs
