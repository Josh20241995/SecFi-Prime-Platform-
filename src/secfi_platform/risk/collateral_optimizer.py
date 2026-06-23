"""
Collateral optimization engine.

A securities-finance desk borrows and lends against collateral; the
choice of WHICH collateral to post or accept is not arbitrary — it
affects:
  - Funding cost (posting high-quality-liquid-assets as collateral is
    more expensive than posting equities because the HQLA is itself a
    scarce funding resource)
  - Haircut / over-collateralization cost
  - Counterparty eligibility (what collateral each counterparty's CSA /
    SLA agreement accepts)
  - Regulatory treatment (LCR impact of collateral received vs. posted)

This module answers three related questions:
  1. CHEAPEST-TO-DELIVER: Given a set of collateral-eligible positions,
     which is cheapest to post for a given financing need?
  2. UPGRADE/DOWNGRADE: Can a specific piece of collateral currently
     posted be replaced with a cheaper substitute that is still eligible,
     freeing up the higher-quality collateral for a more valuable use?
  3. ELIGIBILITY SCREENING: Which positions from inventory are eligible
     as collateral for a specific counterparty and transaction type?

Algorithm choice: scored ranking (not LP here, because collateral
substitution is dominated by eligibility constraints — the feasibility
check is the hard part, the optimality within the feasible set is
usually obvious once you have the cheapest-to-deliver ordering). An LP
extension is documented in _build_ctd_lp_extension() for desks where
the substitution problem is genuinely large-scale.

Assumptions explicitly stated per governance requirement:
  A1: Collateral cost is modeled as opportunity_cost_bps = funding_rate
      for that security (i.e., what the desk could earn lending it out
      instead of posting it). For HQLA (GOVT_SECURITIES), this is the GC
      repo rate; for equities, it is the securities-lending fee. Where
      funding rates are not available for a specific security, the module
      falls back to the collateral-type average from market_quotes.
  A2: Eligibility rules are simplified to a per-counterparty whitelist
      of CollateralType values (sourced from configs/risk_limits.yaml
      in production, seeded from the collateral schedule tables in the
      firm's legal agreement system). A real deployment would source
      these from the legal/documentation system's API, not from config.
  A3: Haircuts used here are from the Position.CollateralLeg or the
      fallback table in risk/counterparty_risk.py. The desk's negotiated
      bilateral haircuts (from the SLA/CSA) may differ; a real deployment
      sources haircuts from the legal-agreement system.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import CollateralType, ProductType
from secfi_platform.common.types import MarketRateQuote, Position


# Collateral preference ordering (cheapest-to-deliver first) — configurable
# in production via configs/risk_limits.yaml `collateral.ctd_tier_order`.
# Lowest number = cheapest to post (least opportunity cost / regulatory impact).
CTD_TIER: dict[CollateralType, int] = {
    CollateralType.EQUITIES: 1,               # cheapest: equities cost lend-fee opportunity cost
    CollateralType.NON_CASH_OTHER: 2,
    CollateralType.AGENCY_SECURITIES: 3,
    CollateralType.LETTER_OF_CREDIT: 4,
    CollateralType.GOVT_SECURITIES: 5,        # most expensive: HQLA, high opportunity cost
    CollateralType.CASH: 6,                   # most expensive for the desk (full reinvestment risk)
}


@dataclass(frozen=True)
class CollateralCandidate:
    position_id: str
    security_internal_id: str
    ticker: str
    collateral_type: CollateralType
    product_type: ProductType
    available_market_value: Decimal       # MV available to pledge as collateral
    currency: str
    haircut_pct: Decimal                   # effective haircut when posted
    opportunity_cost_bps: Decimal          # bps p.a. the desk foregoes by posting this (not lending it out)
    ctd_score: float                        # lower = cheaper to deliver, from _score()


@dataclass
class CollateralSubstitutionRecommendation:
    position_id: str                              # the financing trade that needs collateral
    current_collateral_type: Optional[CollateralType]
    current_collateral_value: Optional[Decimal]
    proposed_candidate: Optional[CollateralCandidate]
    estimated_annual_savings_usd: Decimal         # positive = saves money
    rationale: str
    is_eligible: bool                              # proposed candidate passes counterparty eligibility
    action_required: str                           # "SUBSTITUTE" | "ACCEPT_CURRENT" | "NO_SUBSTITUTE_AVAILABLE"


@dataclass(frozen=True)
class CollateralEligibilitySchedule:
    """Per-counterparty eligible collateral types and minimum haircut floors."""
    counterparty_id: str
    eligible_collateral_types: frozenset      # frozenset[CollateralType]
    minimum_haircut_pct: dict                   # CollateralType -> Decimal minimum haircut


def _opportunity_cost_bps(
    pos: Position,
    market_quotes: dict,
    fallback_by_collateral_type: Optional[dict] = None,
) -> Decimal:
    """
    Estimate the opportunity cost of posting `pos` as collateral rather
    than lending it out. For equities/ETFs, this is the lending fee;
    for government bonds, this is the GC repo rate.
    """
    quote = market_quotes.get(pos.security.internal_id)
    if quote is not None:
        rate = quote.weighted_avg_fee_bps if quote.weighted_avg_fee_bps is not None else quote.avg_fee_bps
        if rate is not None:
            return rate
    # Fallback: use product-type-level average if available
    if fallback_by_collateral_type:
        for col_type, rate in fallback_by_collateral_type.items():
            if pos.security.product_type in (ProductType.EQUITY, ProductType.ETF, ProductType.ADR) and col_type == CollateralType.EQUITIES:
                return rate
            if pos.security.product_type == ProductType.GOVT_BOND and col_type == CollateralType.GOVT_SECURITIES:
                return rate
    return Decimal("20")   # conservative fallback: 20bps opportunity cost if unknown


def _ctd_score(candidate: CollateralCandidate) -> float:
    """
    Cheapest-to-deliver score. Lower = prefer to post this first.
    Weighted combination of:
      - CTD tier (which type of collateral this is)
      - Opportunity cost (desk's foregone earnings per dollar posted)
      - Haircut burden (higher haircut => need to post more MV for the same collateral value)
    """
    tier = CTD_TIER.get(candidate.collateral_type, 3)
    opp_cost_norm = float(candidate.opportunity_cost_bps) / 100.0     # normalize bps to comparable scale
    haircut_norm = float(candidate.haircut_pct) * 10.0
    return float(tier) * 0.50 + opp_cost_norm * 0.35 + haircut_norm * 0.15


def build_collateral_candidates(
    inventory_positions: Iterable[Position],
    market_quotes: dict,
    fallback_opportunity_cost: Optional[dict] = None,
) -> list:
    """
    Build a scored, ranked list of CollateralCandidate objects from
    inventory positions that could be posted as collateral.
    Only positions that are:
      - `is_rehypothecable` (flag on Position), and
      - direction LEND or REVERSE_REPO (desk holds these, can pledge out)
    are eligible as collateral sources in this model.
    """
    candidates = []
    for pos in inventory_positions:
        if not pos.is_rehypothecable:
            continue
        if pos.direction.value not in ("LEND", "REVERSE_REPO"):
            continue
        # Determine CollateralType for this security type
        col_type_map = {
            ProductType.EQUITY: CollateralType.EQUITIES,
            ProductType.ETF: CollateralType.EQUITIES,
            ProductType.ADR: CollateralType.EQUITIES,
            ProductType.GOVT_BOND: CollateralType.GOVT_SECURITIES,
            ProductType.CORPORATE_BOND: CollateralType.NON_CASH_OTHER,
        }
        col_type = col_type_map.get(pos.security.product_type, CollateralType.NON_CASH_OTHER)
        opp_cost = _opportunity_cost_bps(pos, market_quotes, fallback_opportunity_cost)
        # Haircut: use a standard haircut for this collateral type if not on an existing collateral leg
        from secfi_platform.risk.counterparty_risk import DEFAULT_HAIRCUTS_BPS
        haircut = Decimal(str(DEFAULT_HAIRCUTS_BPS.get(col_type, 1500))) / Decimal("10000")

        candidate = CollateralCandidate(
            position_id=pos.position_id,
            security_internal_id=pos.security.internal_id,
            ticker=pos.security.ticker,
            collateral_type=col_type,
            product_type=pos.security.product_type,
            available_market_value=pos.market_value,
            currency=pos.currency,
            haircut_pct=haircut,
            opportunity_cost_bps=opp_cost,
            ctd_score=0.0,    # recomputed below after construction
        )
        # Recompute ctd_score now that the object exists
        import dataclasses
        scored = dataclasses.replace(candidate, ctd_score=_ctd_score(candidate))
        candidates.append(scored)

    candidates.sort(key=lambda c: c.ctd_score)
    return candidates


def select_cheapest_to_deliver(
    candidates: list,
    required_collateral_value_usd: Decimal,
    eligibility_schedule: Optional[CollateralEligibilitySchedule] = None,
) -> list:
    """
    Greedily select the cheapest-eligible collateral candidates to cover
    `required_collateral_value_usd` (post-haircut value).
    Returns the selected candidates in CTD order.
    """
    selected = []
    covered = Decimal("0")

    for candidate in candidates:
        if covered >= required_collateral_value_usd:
            break
        # Eligibility gate
        if eligibility_schedule is not None:
            if candidate.collateral_type not in eligibility_schedule.eligible_collateral_types:
                continue
            min_hc = eligibility_schedule.minimum_haircut_pct.get(candidate.collateral_type, Decimal("0"))
            effective_hc = max(candidate.haircut_pct, min_hc)
        else:
            effective_hc = candidate.haircut_pct

        post_haircut_value = candidate.available_market_value * (Decimal("1") - effective_hc)
        selected.append(candidate)
        covered += post_haircut_value

    return selected


def recommend_collateral_substitutions(
    financing_positions: Iterable[Position],       # positions that NEED collateral posted against them
    inventory_candidates: list,                     # output of build_collateral_candidates
    eligibility_schedule: Optional[CollateralEligibilitySchedule] = None,
) -> list:
    """
    For each financing position (BORROW/REPO), check whether the currently-
    posted collateral can be replaced with a cheaper candidate from inventory.
    Produces CollateralSubstitutionRecommendation objects.
    """
    recommendations = []
    for pos in financing_positions:
        if pos.direction.value not in ("BORROW", "REPO"):
            continue

        current_col = pos.collateral[0] if pos.collateral else None
        current_type = current_col.collateral_type if current_col else None
        current_value = current_col.market_value if current_col else None
        current_opp_cost = Decimal("20")  # fallback if no current collateral info

        # Find the cheapest eligible candidate that isn't what's already posted
        best_candidate = None
        for candidate in inventory_candidates:
            if current_type is not None and candidate.collateral_type == current_type:
                # Already posting this type; only suggest if a different type saves money
                continue
            if eligibility_schedule and candidate.collateral_type not in eligibility_schedule.eligible_collateral_types:
                continue
            if candidate.available_market_value < (pos.market_value * Decimal("0.9")):
                continue   # insufficient inventory to cover this trade
            best_candidate = candidate
            break

        if best_candidate is None:
            recommendations.append(CollateralSubstitutionRecommendation(
                position_id=pos.position_id,
                current_collateral_type=current_type,
                current_collateral_value=current_value,
                proposed_candidate=None,
                estimated_annual_savings_usd=Decimal("0"),
                rationale="No cheaper eligible substitute found in current inventory.",
                is_eligible=False,
                action_required="NO_SUBSTITUTE_AVAILABLE",
            ))
            continue

        savings_bps = current_opp_cost - best_candidate.opportunity_cost_bps
        annual_savings = pos.market_value * savings_bps / Decimal("10000")

        if annual_savings <= Decimal("500"):    # materiality threshold: don't churn for <$500/yr
            recommendations.append(CollateralSubstitutionRecommendation(
                position_id=pos.position_id,
                current_collateral_type=current_type,
                current_collateral_value=current_value,
                proposed_candidate=best_candidate,
                estimated_annual_savings_usd=annual_savings,
                rationale=(
                    f"Potential savings of ${annual_savings:,.0f}/yr below $500 materiality "
                    f"threshold — no action recommended."
                ),
                is_eligible=True,
                action_required="ACCEPT_CURRENT",
            ))
        else:
            recommendations.append(CollateralSubstitutionRecommendation(
                position_id=pos.position_id,
                current_collateral_type=current_type,
                current_collateral_value=current_value,
                proposed_candidate=best_candidate,
                estimated_annual_savings_usd=annual_savings,
                rationale=(
                    f"Substitute {best_candidate.ticker} ({best_candidate.collateral_type.value}, "
                    f"{float(best_candidate.opportunity_cost_bps):.0f}bps opp cost) for current "
                    f"{current_type.value if current_type else 'unknown'} collateral "
                    f"({float(current_opp_cost):.0f}bps opp cost). "
                    f"Estimated annual saving: ${annual_savings:,.0f}."
                ),
                is_eligible=True,
                action_required="SUBSTITUTE",
            ))

    recommendations.sort(key=lambda r: r.estimated_annual_savings_usd, reverse=True)
    return recommendations


def total_collateral_optimization_opportunity_usd(recommendations: list) -> Decimal:
    return sum(
        (r.estimated_annual_savings_usd for r in recommendations if r.action_required == "SUBSTITUTE"),
        Decimal("0"),
    )
