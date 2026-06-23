"""
Market pricing intelligence engine.

Ingests market composite rates (EquiLend/DataLend-style) and the desk's
live book, compares them, classifies specialness tier, and emits
explainable repricing recommendations.

Specialness classification (configurable thresholds in
configs/base.yaml under `pricing.specialness_thresholds`):
  GC                  : utilization < 50% AND fee < gc_fee_ceiling_bps
  WARM                : utilization 50-85% OR fee trending up > warm_trend_bps/week
  SPECIALS_IN_WAITING : utilization > 85% AND fee still < special_fee_floor_bps
                         (supply tightening, fee hasn't caught up yet — this is
                         the highest-value early-detection signal for the desk)
  SPECIAL             : fee >= special_fee_floor_bps
  HTB                 : fee >= htb_fee_floor_bps AND utilization > htb_utilization_floor
  DEEP_SPECIAL        : fee >= deep_special_fee_floor_bps

Mispricing score: z-score of (desk_rate - market_weighted_avg_rate) against
the cross-sectional dispersion of market rates for securities in the same
specialness tier, so a 5bp gap on a GC name (huge relative miss) is scored
very differently from a 5bp gap on a 4000bp deep special (noise).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import ApprovalStatus, Direction, RecommendationAction, SpecialnessTier
from secfi_platform.common.types import MarketRateQuote, Position, Recommendation
from secfi_platform.explainability.explain import (
    DataCompletenessReport,
    build_rationale,
    compute_confidence,
    compute_priority_score,
)


@dataclass(frozen=True)
class SpecialnessThresholds:
    gc_fee_ceiling_bps: Decimal = Decimal("25")
    warm_trend_bps_per_week: Decimal = Decimal("10")
    special_fee_floor_bps: Decimal = Decimal("100")
    htb_fee_floor_bps: Decimal = Decimal("300")
    htb_utilization_floor: Decimal = Decimal("0.90")
    deep_special_fee_floor_bps: Decimal = Decimal("1000")


def classify_specialness(
    quote: MarketRateQuote,
    thresholds: SpecialnessThresholds = SpecialnessThresholds(),
    prior_week_fee_bps: Optional[Decimal] = None,
) -> SpecialnessTier:
    fee = quote.weighted_avg_fee_bps if quote.weighted_avg_fee_bps is not None else quote.avg_fee_bps
    util = quote.utilization_pct or Decimal("0")

    if fee is None:
        return SpecialnessTier.GC  # fallback: treat unknown as GC, flagged via data_quality_flag upstream

    if fee >= thresholds.deep_special_fee_floor_bps:
        return SpecialnessTier.DEEP_SPECIAL
    if fee >= thresholds.htb_fee_floor_bps and util >= thresholds.htb_utilization_floor:
        return SpecialnessTier.HTB
    if fee >= thresholds.special_fee_floor_bps:
        return SpecialnessTier.SPECIAL
    if util >= Decimal("0.85") and fee < thresholds.special_fee_floor_bps:
        return SpecialnessTier.SPECIALS_IN_WAITING
    if prior_week_fee_bps is not None and (fee - prior_week_fee_bps) >= thresholds.warm_trend_bps_per_week:
        return SpecialnessTier.WARM
    if util >= Decimal("0.50"):
        return SpecialnessTier.WARM
    if fee < thresholds.gc_fee_ceiling_bps:
        return SpecialnessTier.GC
    return SpecialnessTier.WARM


@dataclass
class PricingDispersionRow:
    security_internal_id: str
    desk_rate_bps: Decimal
    market_weighted_avg_bps: Optional[Decimal]
    gap_bps: Optional[Decimal]
    specialness_tier: SpecialnessTier
    z_score_within_tier: Optional[float]
    data_quality_flag: str


def _tier_dispersion_stats(rows: list) -> dict:
    """mean/std of market rate within each specialness tier, for z-scoring."""
    import statistics

    by_tier: dict = {}
    for r in rows:
        if r.market_weighted_avg_bps is None:
            continue
        by_tier.setdefault(r.specialness_tier, []).append(float(r.market_weighted_avg_bps))

    stats = {}
    for tier, values in by_tier.items():
        if len(values) < 2:
            stats[tier] = (statistics.mean(values), 1.0) if values else (0.0, 1.0)
        else:
            mean = statistics.mean(values)
            stdev = statistics.stdev(values) or 1.0
            stats[tier] = (mean, stdev)
    return stats


def build_pricing_dispersion(
    positions: Iterable[Position],
    market_quotes: dict,         # security_internal_id -> MarketRateQuote
    thresholds: SpecialnessThresholds = SpecialnessThresholds(),
) -> list:
    rows = []
    for pos in positions:
        quote = market_quotes.get(pos.security.internal_id)
        if quote is None:
            rows.append(
                PricingDispersionRow(
                    security_internal_id=pos.security.internal_id,
                    desk_rate_bps=pos.rate_bps,
                    market_weighted_avg_bps=None,
                    gap_bps=None,
                    specialness_tier=SpecialnessTier.GC,
                    z_score_within_tier=None,
                    data_quality_flag="MISSING",
                )
            )
            continue
        tier = classify_specialness(quote, thresholds)
        market_rate = quote.weighted_avg_fee_bps if quote.weighted_avg_fee_bps is not None else quote.avg_fee_bps
        gap = (pos.rate_bps - market_rate) if market_rate is not None else None
        rows.append(
            PricingDispersionRow(
                security_internal_id=pos.security.internal_id,
                desk_rate_bps=pos.rate_bps,
                market_weighted_avg_bps=market_rate,
                gap_bps=gap,
                specialness_tier=tier,
                z_score_within_tier=None,  # filled below
                data_quality_flag=quote.data_quality_flag.value,
            )
        )

    stats = _tier_dispersion_stats(rows)
    out = []
    for r in rows:
        z = None
        if r.market_weighted_avg_bps is not None and r.specialness_tier in stats:
            mean, stdev = stats[r.specialness_tier]
            z = (float(r.market_weighted_avg_bps) - mean) / stdev if stdev else 0.0
        out.append(
            PricingDispersionRow(
                security_internal_id=r.security_internal_id,
                desk_rate_bps=r.desk_rate_bps,
                market_weighted_avg_bps=r.market_weighted_avg_bps,
                gap_bps=r.gap_bps,
                specialness_tier=r.specialness_tier,
                z_score_within_tier=z,
                data_quality_flag=r.data_quality_flag,
            )
        )
    return out


def generate_pricing_recommendations(
    positions: Iterable[Position],
    market_quotes: dict,
    thresholds: SpecialnessThresholds = SpecialnessThresholds(),
    min_actionable_gap_bps: Decimal = Decimal("5"),
    as_of: Optional[datetime] = None,
) -> list:
    """
    Produces Recommendation objects:
      - LEND / REVERSE_REPO positions priced BELOW market => REPRICE up
        (desk is on the "earns the rate" side of the trade and is leaving
        revenue on the table: lending securities for too low a fee, or
        lending cash via reverse repo for too low a repo rate).
      - BORROW / REPO positions priced ABOVE market => REPRICE down
        (desk is on the "pays the rate" side: borrowing securities for too
        high a fee, or borrowing cash via repo for too high a repo rate).
      - SPECIALS_IN_WAITING names the desk lends at GC-like rates => urgent REPRICE,
        flagged with elevated priority since the window to capture the spread is short.
    """
    as_of = as_of or datetime.now(timezone.utc)
    positions = list(positions)
    dispersion = {r.security_internal_id: r for r in build_pricing_dispersion(positions, market_quotes, thresholds)}

    recs = []
    for pos in positions:
        row = dispersion.get(pos.security.internal_id)
        if row is None or row.gap_bps is None:
            continue

        gap = row.gap_bps
        # "Earns the rate" side: LEND (lends securities, earns fee) and
        # REVERSE_REPO (lends cash, earns repo rate) both want the rate HIGH.
        # "Pays the rate" side: BORROW and REPO both want the rate LOW.
        # This mirrors the sign convention used in risk/rates_fx.py
        # _rate_exposure_sign for the same economic reason.
        desk_earns_the_rate = pos.direction in (Direction.LEND, Direction.REVERSE_REPO)
        is_mispriced_earning_side = desk_earns_the_rate and gap < -min_actionable_gap_bps
        is_mispriced_paying_side = (not desk_earns_the_rate) and gap > min_actionable_gap_bps

        if not (is_mispriced_earning_side or is_mispriced_paying_side):
            continue

        target_rate = row.market_weighted_avg_bps
        pnl_impact = abs(gap) / Decimal(10000) * pos.market_value

        urgency_boost = 25.0 if row.specialness_tier == SpecialnessTier.SPECIALS_IN_WAITING else 0.0
        confidence_certainty = min(abs(float(gap)) / 50.0, 1.0)
        completeness = DataCompletenessReport(
            required_fields=3,
            present_and_valid_fields=3 if row.data_quality_flag == "OK" else 2,
            fallback_fields=0 if row.data_quality_flag == "OK" else 1,
        )
        data_age_minutes = 15 if row.data_quality_flag == "OK" else 720
        confidence = compute_confidence(completeness, confidence_certainty, data_age_minutes)

        pnl_score = min(float(pnl_impact) / 25_000 * 100, 100)
        urgency_score = min(abs(float(gap)) * 1.5 + urgency_boost, 100)
        priority = compute_priority_score(
            pnl_score_0_100=pnl_score, risk_score_0_100=30.0,
            urgency_score_0_100=urgency_score, confidence_0_1=confidence,
        )

        rationale = build_rationale(
            f"{pos.security.ticker} is classified {row.specialness_tier.value} "
            f"(market weighted-avg {float(row.market_weighted_avg_bps):.1f}bps, "
            f"desk rate {float(pos.rate_bps):.1f}bps, gap {float(gap):+.1f}bps).",
            "Specials-in-waiting: utilization has crossed the threshold but fee has not yet "
            "repriced — early mover advantage on repricing." if row.specialness_tier == SpecialnessTier.SPECIALS_IN_WAITING else "",
            f"Estimated annualized P&L improvement from repricing: ${pnl_impact:,.0f}.",
            f"Data source quality flag: {row.data_quality_flag}." if row.data_quality_flag != "OK" else "",
        )

        recs.append(
            Recommendation(
                recommendation_id=str(uuid.uuid4()),
                generated_at=as_of,
                source_engine="pricing.pricing_intelligence",
                action=RecommendationAction.REPRICE,
                target_type="POSITION",
                target_id=pos.position_id,
                quantity=pos.market_value,
                from_value=pos.rate_bps,
                to_value=target_rate,
                estimated_pnl_impact_usd=pnl_impact,
                estimated_capital_impact_usd=None,
                estimated_rwa_impact_usd=None,
                rationale=rationale,
                supporting_metrics={
                    "specialness_tier": row.specialness_tier.value,
                    "gap_bps": float(gap),
                    "z_score_within_tier": row.z_score_within_tier,
                },
                confidence=confidence,
                data_completeness_pct=completeness.completeness_pct,
                priority_score=priority,
                approval_status=ApprovalStatus.PROPOSED,
            )
        )

    recs.sort(key=lambda r: r.priority_score, reverse=True)
    return recs
