"""
Counterparty growth and contraction opportunity engine.

Combines risk-adjusted return (from risk/capital_rwa.py) with exposure
headroom (from risk/counterparty_risk.py) and pricing competitiveness
(from pricing/pricing_intelligence.py) to recommend, per counterparty:
GROW / HOLD / REDUCE / REPRICE / REROUTE / HEDGE / SUBSTITUTE / DO_NOTHING.

Decision logic (deterministic, explainable — see module docstring of
explainability/explain.py for why this is rules-based rather than a
black-box classifier):

  GROW    : RoC > target hurdle AND limit utilization < grow_headroom_threshold
            AND not on watch_list AND no unresolved CRITICAL recon breaks.
  REDUCE  : limit utilization > reduce_threshold OR RoC < min_acceptable_roc
            OR watch_list == True.
  REPRICE : RoC between min_acceptable and target hurdle (capital-adequate
            but underpriced relative to risk) AND pricing dispersion shows
            achievable upside.
  HOLD    : everything else economically acceptable with no clear signal
            to change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from secfi_platform.common.enums import ApprovalStatus, RecommendationAction
from secfi_platform.common.types import Counterparty, Recommendation
from secfi_platform.explainability.explain import (
    DataCompletenessReport,
    build_rationale,
    compute_confidence,
    compute_priority_score,
)
from secfi_platform.risk.capital_rwa import CounterpartyCapitalSummary
from secfi_platform.risk.counterparty_risk import CounterpartyExposure


@dataclass(frozen=True)
class GrowthThresholds:
    target_roc: float = 0.15                  # 15% return on capital hurdle
    min_acceptable_roc: float = 0.05
    grow_headroom_utilization_ceiling: float = 0.60
    reduce_utilization_floor: float = 0.90


@dataclass
class CounterpartyOpportunity:
    counterparty_id: str
    action: RecommendationAction
    roc: Optional[float]
    rob: Optional[float]
    utilization_pct: Optional[float]
    headroom_usd: Optional[Decimal]
    rationale: list
    confidence: float
    priority_score: float


def assess_counterparty_opportunity(
    counterparty: Counterparty,
    capital_summary: CounterpartyCapitalSummary,
    exposure: CounterpartyExposure,
    thresholds: GrowthThresholds = GrowthThresholds(),
    has_unresolved_critical_breaks: bool = False,
) -> CounterpartyOpportunity:
    roc = capital_summary.blended_return_on_capital
    rob = capital_summary.blended_return_on_balance_sheet
    util = exposure.utilization_pct

    reasons = []
    action = RecommendationAction.HOLD

    if counterparty.watch_list:
        action = RecommendationAction.REDUCE
        reasons.append("Counterparty is on the firmwide credit watch list.")
    elif has_unresolved_critical_breaks:
        action = RecommendationAction.REDUCE
        reasons.append("Counterparty has unresolved CRITICAL reconciliation breaks; reduce exposure "
                        "until operational risk is remediated.")
    elif util is not None and util > thresholds.reduce_utilization_floor:
        action = RecommendationAction.REDUCE
        reasons.append(f"Limit utilization at {util:.0%} exceeds the {thresholds.reduce_utilization_floor:.0%} "
                        f"reduce threshold.")
    elif roc is not None and roc < thresholds.min_acceptable_roc:
        action = RecommendationAction.REDUCE
        reasons.append(f"Blended return on capital {roc:.1%} is below the minimum acceptable "
                        f"{thresholds.min_acceptable_roc:.1%}.")
    elif roc is not None and thresholds.min_acceptable_roc <= roc < thresholds.target_roc:
        action = RecommendationAction.REPRICE
        reasons.append(f"Return on capital {roc:.1%} is capital-adequate but below the {thresholds.target_roc:.1%} "
                        f"target hurdle; repricing toward market should be evaluated before scaling.")
    elif (
        roc is not None and roc >= thresholds.target_roc
        and util is not None and util < thresholds.grow_headroom_utilization_ceiling
        and not exposure.wrong_way_risk_flags
    ):
        action = RecommendationAction.GROW
        reasons.append(f"Return on capital {roc:.1%} exceeds the {thresholds.target_roc:.1%} target hurdle "
                        f"with {(1 - util):.0%} limit headroom remaining and no flagged wrong-way risk.")
    else:
        reasons.append("No threshold breach or strong opportunity signal detected; maintain current balances "
                        "and monitor.")

    if exposure.wrong_way_risk_flags:
        reasons.extend(exposure.wrong_way_risk_flags)
    if exposure.herfindahl_issuer > 2500:
        reasons.append(f"Issuer concentration HHI of {exposure.herfindahl_issuer:.0f} indicates "
                        f"meaningfully concentrated exposure within this counterparty's book.")

    completeness = DataCompletenessReport(
        required_fields=3,
        present_and_valid_fields=sum(x is not None for x in (roc, rob, util)) or 1,
        fallback_fields=3 - (sum(x is not None for x in (roc, rob, util)) or 1),
    )
    confidence = compute_confidence(completeness, model_certainty=0.7, data_age_minutes=60)

    pnl_score = min(max((roc or 0) * 100, 0), 100)
    risk_score = min((util or 0) * 100, 100)
    urgency_score = 80.0 if action == RecommendationAction.REDUCE else (50.0 if action == RecommendationAction.REPRICE else 20.0)
    priority = compute_priority_score(
        pnl_score_0_100=pnl_score, risk_score_0_100=risk_score,
        urgency_score_0_100=urgency_score, confidence_0_1=confidence,
    )

    return CounterpartyOpportunity(
        counterparty_id=counterparty.counterparty_id,
        action=action,
        roc=roc,
        rob=rob,
        utilization_pct=util,
        headroom_usd=exposure.headroom_usd,
        rationale=build_rationale(*reasons),
        confidence=confidence,
        priority_score=priority,
    )


def opportunities_to_recommendations(
    opportunities: list, as_of: Optional[datetime] = None
) -> list:
    as_of = as_of or datetime.now(timezone.utc)
    recs = []
    for opp in opportunities:
        if opp.action == RecommendationAction.HOLD:
            continue
        recs.append(
            Recommendation(
                recommendation_id=str(uuid.uuid4()),
                generated_at=as_of,
                source_engine="growth.counterparty_growth",
                action=opp.action,
                target_type="COUNTERPARTY",
                target_id=opp.counterparty_id,
                quantity=opp.headroom_usd,
                from_value=None,
                to_value=None,
                estimated_pnl_impact_usd=None,
                estimated_capital_impact_usd=None,
                estimated_rwa_impact_usd=None,
                rationale=opp.rationale,
                supporting_metrics={
                    "return_on_capital": opp.roc,
                    "return_on_balance_sheet": opp.rob,
                    "utilization_pct": opp.utilization_pct,
                },
                confidence=opp.confidence,
                data_completeness_pct=1.0,
                priority_score=opp.priority_score,
                approval_status=ApprovalStatus.PROPOSED,
            )
        )
    recs.sort(key=lambda r: r.priority_score, reverse=True)
    return recs
