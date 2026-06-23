"""
Recall, return, buy-in, and supply risk engine.

Produces a ranked urgency queue answering: "which names need attention
RIGHT NOW, and why?" This is the module the trader looks at first thing
every morning and re-checks intraday.

Urgency score (0-100) is a weighted combination of:
  - settlement/fail aging (longer aging => higher urgency, capped)
  - locate shortage severity
  - HTB/specials concentration (scarce names are harder to re-source if recalled)
  - corporate-action-driven return obligations within the urgency window
  - custodian/desk discrepancy flags (operational risk compounding buy-in risk)

Buy-in risk score is a related but distinct 0-100 score focused
specifically on settlement-fail-to-buy-in escalation likelihood, using a
simplified staged model:
  fail_age 0-2 days   : low risk, normal settlement friction
  fail_age 3-4 days   : medium, approaching typical buy-in notice windows
  fail_age 5+ days    : high, many markets' buy-in notice periods triggered
  HTB/deep-special name : multiplies risk (scarce replacement supply)
  no substitute inventory identified : multiplies risk further

This is a SCORING/RANKING model, not a regulatory buy-in determination —
actual buy-in notice timing is market/CSDR/Reg-SHO-regime specific and
must be sourced from the firm's settlement/compliance system
(configs/base.yaml `recall_buyin.regime_notice_days` documents the
assumption used here and must be reviewed against the actual applicable
regime per market).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import ApprovalStatus, RecommendationAction, SpecialnessTier
from secfi_platform.common.types import Position, Recommendation
from secfi_platform.explainability.explain import (
    DataCompletenessReport,
    build_rationale,
    compute_confidence,
    compute_priority_score,
)


@dataclass(frozen=True)
class SettlementFail:
    position_id: str
    security_internal_id: str
    fail_age_days: int
    fail_quantity: Decimal
    counterparty_id: str
    is_desk_receiving: bool      # True = desk is owed securities (fail-to-receive); False = desk owes (fail-to-deliver)


@dataclass(frozen=True)
class LocateShortage:
    security_internal_id: str
    requested_quantity: Decimal
    available_quantity: Decimal

    @property
    def shortage_quantity(self) -> Decimal:
        return max(self.requested_quantity - self.available_quantity, Decimal("0"))


@dataclass(frozen=True)
class SubstituteInventory:
    security_internal_id: str
    substitute_security_ids: tuple[str, ...]


@dataclass
class UrgencyQueueRow:
    position_id: str
    security_internal_id: str
    ticker: str
    counterparty_id: str
    urgency_score: float
    buyin_risk_score: float
    drivers: list                    # list[str] human-readable urgency drivers
    recommended_action: RecommendationAction
    substitute_candidates: tuple
    estimated_pnl_at_risk_usd: Decimal


REGIME_NOTICE_DAYS_DEFAULT = 4   # configurable per market/regime; see module docstring


def _fail_age_component(fail_age_days: int) -> float:
    return min(fail_age_days * 12.0, 60.0)


def _specialness_multiplier(tier: Optional[SpecialnessTier]) -> float:
    return {
        None: 1.0,
        SpecialnessTier.GC: 0.8,
        SpecialnessTier.WARM: 1.0,
        SpecialnessTier.SPECIALS_IN_WAITING: 1.3,
        SpecialnessTier.SPECIAL: 1.5,
        SpecialnessTier.HTB: 1.8,
        SpecialnessTier.DEEP_SPECIAL: 2.2,
    }.get(tier, 1.0)


def compute_buyin_risk_score(
    fail: SettlementFail,
    specialness_tier: Optional[SpecialnessTier],
    has_substitute: bool,
    regime_notice_days: int = REGIME_NOTICE_DAYS_DEFAULT,
) -> float:
    if not fail.is_desk_receiving:
        # Fail-to-deliver where the desk owes securities out is the actual
        # buy-in-able direction in most regimes (counterparty can issue
        # notice against the desk). Fail-to-receive creates the desk's own
        # downstream delivery risk but is scored via urgency, not buy-in.
        base = min((fail.fail_age_days / max(regime_notice_days, 1)) * 60.0, 80.0)
    else:
        base = min((fail.fail_age_days / max(regime_notice_days, 1)) * 20.0, 30.0)

    mult = _specialness_multiplier(specialness_tier)
    if not has_substitute:
        mult *= 1.25

    score = min(base * mult, 100.0)
    return round(score, 2)


def compute_urgency_queue(
    positions: Iterable[Position],
    fails: Iterable[SettlementFail],
    locate_shortages: Iterable[LocateShortage],
    specialness_by_security: dict,                # security_internal_id -> SpecialnessTier
    substitutes: dict,                              # security_internal_id -> SubstituteInventory
    ca_driven_return_security_ids: set,             # set of security_internal_id with imminent CA-driven return need
    regime_notice_days: int = REGIME_NOTICE_DAYS_DEFAULT,
) -> list:
    positions_by_id = {p.position_id: p for p in positions}
    positions_by_security: dict = {}
    for p in positions_by_id.values():
        positions_by_security.setdefault(p.security.internal_id, []).append(p)

    shortage_by_security = {s.security_internal_id: s for s in locate_shortages}

    rows = []
    for fail in fails:
        pos = positions_by_id.get(fail.position_id)
        if pos is None:
            continue

        tier = specialness_by_security.get(pos.security.internal_id)
        substitute = substitutes.get(pos.security.internal_id)
        has_substitute = bool(substitute and substitute.substitute_security_ids)

        buyin_score = compute_buyin_risk_score(fail, tier, has_substitute, regime_notice_days)

        urgency = _fail_age_component(fail.fail_age_days)
        drivers = [f"Settlement fail aged {fail.fail_age_days} day(s); "
                   f"{'fail-to-deliver (desk owes)' if not fail.is_desk_receiving else 'fail-to-receive (desk owed)'}."]

        shortage = shortage_by_security.get(pos.security.internal_id)
        if shortage and shortage.shortage_quantity > 0:
            urgency += 15.0
            drivers.append(f"Locate shortage of {shortage.shortage_quantity} shares against open requests.")

        if tier in (SpecialnessTier.HTB, SpecialnessTier.DEEP_SPECIAL):
            urgency += 15.0
            drivers.append(f"Security classified {tier.value}; scarce replacement supply.")
        elif tier == SpecialnessTier.SPECIALS_IN_WAITING:
            urgency += 8.0
            drivers.append("Security is specials-in-waiting; supply tightening.")

        if pos.security.internal_id in ca_driven_return_security_ids:
            urgency += 20.0
            drivers.append("Upcoming corporate action creates an imminent return obligation on this name.")

        if not has_substitute:
            urgency += 5.0
            drivers.append("No substitute inventory identified for this name.")

        urgency = min(urgency, 100.0)

        if buyin_score >= 70:
            action = RecommendationAction.RECALL if fail.is_desk_receiving else RecommendationAction.RETURN
        elif has_substitute:
            action = RecommendationAction.SUBSTITUTE
        elif urgency >= 60:
            action = RecommendationAction.HEDGE
        else:
            action = RecommendationAction.DO_NOTHING

        pnl_at_risk = pos.market_value * Decimal("0.01")  # placeholder proxy: 1% of MV as a rough fail-cost proxy;
        # production should source actual claim/financing-cost-of-fail economics from ops.

        rows.append(
            UrgencyQueueRow(
                position_id=pos.position_id,
                security_internal_id=pos.security.internal_id,
                ticker=pos.security.ticker,
                counterparty_id=pos.counterparty_id,
                urgency_score=urgency,
                buyin_risk_score=buyin_score,
                drivers=drivers,
                recommended_action=action,
                substitute_candidates=substitute.substitute_security_ids if substitute else (),
                estimated_pnl_at_risk_usd=pnl_at_risk,
            )
        )

    rows.sort(key=lambda r: (r.buyin_risk_score, r.urgency_score), reverse=True)
    return rows


def queue_to_recommendations(rows: Iterable[UrgencyQueueRow], as_of: Optional[datetime] = None) -> list:
    as_of = as_of or datetime.now(timezone.utc)
    recs = []
    for row in rows:
        if row.recommended_action == RecommendationAction.DO_NOTHING:
            continue

        completeness = DataCompletenessReport(
            required_fields=4, present_and_valid_fields=4 - (0 if row.substitute_candidates else 1),
            fallback_fields=0 if row.substitute_candidates else 1,
        )
        confidence = compute_confidence(completeness, model_certainty=row.buyin_risk_score / 100.0, data_age_minutes=10)
        priority = compute_priority_score(
            pnl_score_0_100=min(float(row.estimated_pnl_at_risk_usd) / 10_000 * 100, 100),
            risk_score_0_100=row.buyin_risk_score,
            urgency_score_0_100=row.urgency_score,
            confidence_0_1=confidence,
        )
        rationale = build_rationale(*row.drivers, f"Recommended action: {row.recommended_action.value}.")

        recs.append(
            Recommendation(
                recommendation_id=str(uuid.uuid4()),
                generated_at=as_of,
                source_engine="recall_buyin.recall_risk_engine",
                action=row.recommended_action,
                target_type="POSITION",
                target_id=row.position_id,
                quantity=None,
                from_value=None,
                to_value=None,
                estimated_pnl_impact_usd=-row.estimated_pnl_at_risk_usd,
                estimated_capital_impact_usd=None,
                estimated_rwa_impact_usd=None,
                rationale=rationale,
                supporting_metrics={
                    "urgency_score": row.urgency_score,
                    "buyin_risk_score": row.buyin_risk_score,
                    "substitute_candidates": list(row.substitute_candidates),
                },
                confidence=confidence,
                data_completeness_pct=completeness.completeness_pct,
                priority_score=priority,
                approval_status=ApprovalStatus.PROPOSED,
            )
        )
    recs.sort(key=lambda r: r.priority_score, reverse=True)
    return recs
