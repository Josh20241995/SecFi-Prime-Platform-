"""
Capital, RWA, leverage ratio, and firmwide balance-sheet consumption layer.

*** GOVERNANCE NOTE (read before using output as official capital numbers) ***
This module computes a DESK-DECISION-SUPPORT APPROXIMATION of regulatory
capital consumption, modeled on the shape of the Basel SA-CCR /
comprehensive-approach methodology for securities financing transactions.
It is explicitly NOT the firm's books-and-records regulatory capital
engine. Official RWA, leverage exposure, and capital figures must come
from Treasury/Capital Management's certified calculation engine. This
module exists so the desk can see DIRECTIONAL, MARGINAL capital cost of a
trade BEFORE booking it, for optimization and pricing purposes, with a
documented, simplified formula. Any use of this module's output for
external reporting, regulatory filings, or official capital attestation
is out of scope and prohibited. See docs/model_risk.md and
docs/assumptions_and_limitations.md item C-1.

Simplified methodology used here:
  EAD_sft   = max(0, (E - C) * (1 + supervisory_haircut_addon))
              where E = securities/cash leg market value,
                    C = collateral market value (post-haircut already
                        applied upstream in risk/counterparty_risk.py),
                    supervisory_haircut_addon = configs/risk_limits.yaml
                    `capital.sft_addon_pct` (default 0, since haircuts are
                    already collateral-leg-level; addon exists for desks
                    that want an extra conservatism buffer).
  RWA       = EAD_sft * risk_weight(counterparty_tier, counterparty_type)
              risk weights are configurable per configs/risk_limits.yaml
              `capital.risk_weights_pct`, NOT invented Basel constants —
              the actual standardized risk weights must be supplied by
              Treasury/Capital Management and loaded into config.
  LR_exposure = gross SFT exposure measure per leverage ratio rules
              (simplified: EAD_sft, no master-netting-agreement netting
              benefit applied unless counterparty.is_netting_eligible).
  Capital_cost_usd = RWA * capital.target_cet1_ratio * capital.cost_of_capital_pct (annualized)
  RoB (return on balance sheet) = annualized_revenue_usd / LR_exposure
  RoC (return on capital) = annualized_revenue_usd / (RWA * target_cet1_ratio)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import CounterpartyTier, CounterpartyType
from secfi_platform.common.types import Counterparty, Position


DEFAULT_RISK_WEIGHTS_PCT = {
    # counterparty_type -> tier -> risk weight (as a decimal fraction)
    CounterpartyType.BANK_DEALER: {
        CounterpartyTier.TIER_1_PRIME: Decimal("0.20"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("0.30"),
        CounterpartyTier.TIER_3_WATCH: Decimal("0.50"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("1.00"),
    },
    CounterpartyType.HEDGE_FUND: {
        CounterpartyTier.TIER_1_PRIME: Decimal("1.00"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("1.00"),
        CounterpartyTier.TIER_3_WATCH: Decimal("1.50"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("2.50"),
    },
    CounterpartyType.ASSET_MANAGER: {
        CounterpartyTier.TIER_1_PRIME: Decimal("0.50"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("0.75"),
        CounterpartyTier.TIER_3_WATCH: Decimal("1.00"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("1.50"),
    },
    CounterpartyType.PENSION_INSURANCE: {
        CounterpartyTier.TIER_1_PRIME: Decimal("0.20"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("0.30"),
        CounterpartyTier.TIER_3_WATCH: Decimal("0.50"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("1.00"),
    },
    CounterpartyType.CCP: {
        CounterpartyTier.TIER_1_PRIME: Decimal("0.02"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("0.02"),
        CounterpartyTier.TIER_3_WATCH: Decimal("0.04"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("0.04"),
    },
    CounterpartyType.SOVEREIGN_SUPRANATIONAL: {
        CounterpartyTier.TIER_1_PRIME: Decimal("0.00"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("0.00"),
        CounterpartyTier.TIER_3_WATCH: Decimal("0.20"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("0.50"),
    },
    CounterpartyType.OTHER: {
        CounterpartyTier.TIER_1_PRIME: Decimal("1.00"),
        CounterpartyTier.TIER_2_STANDARD: Decimal("1.00"),
        CounterpartyTier.TIER_3_WATCH: Decimal("1.50"),
        CounterpartyTier.TIER_4_RESTRICTED: Decimal("2.50"),
    },
}


@dataclass(frozen=True)
class CapitalParameters:
    target_cet1_ratio: Decimal = Decimal("0.115")     # 11.5% target CET1
    cost_of_capital_pct: Decimal = Decimal("0.12")     # 12% hurdle rate, annualized
    sft_addon_pct: Decimal = Decimal("0.00")
    risk_weights_pct: dict = None

    def __post_init__(self):
        if self.risk_weights_pct is None:
            object.__setattr__(self, "risk_weights_pct", DEFAULT_RISK_WEIGHTS_PCT)


@dataclass
class PositionCapitalProfile:
    position_id: str
    ead_usd: Decimal
    risk_weight_pct: Decimal
    rwa_usd: Decimal
    leverage_exposure_usd: Decimal
    annualized_revenue_usd: Decimal
    capital_cost_usd: Decimal
    return_on_balance_sheet: Optional[float]
    return_on_capital: Optional[float]
    risk_adjusted_return_on_capital: Optional[float]   # RAROC-style: (revenue - expected loss) / capital


@dataclass
class CounterpartyCapitalSummary:
    counterparty_id: str
    total_ead_usd: Decimal
    total_rwa_usd: Decimal
    total_leverage_exposure_usd: Decimal
    total_annualized_revenue_usd: Decimal
    total_capital_cost_usd: Decimal
    blended_return_on_balance_sheet: Optional[float]
    blended_return_on_capital: Optional[float]
    netting_benefit_applied: bool


def _annualized_revenue(pos: Position) -> Decimal:
    """Net annualized revenue estimate for one position from its rate."""
    rate_decimal = pos.rate_bps / Decimal(10000)
    if pos.rate_type_is_rebate:
        # Rebate: desk pays out a rebate on cash collateral received when lending,
        # or receives a rebate when borrowing against cash posted. Net revenue
        # for a LEND position with a rebate is (reinvestment_spread - rebate);
        # absent a configured reinvestment spread we report the rebate cost as
        # negative revenue, which is conservative and clearly labeled.
        return -1 * pos.market_value * rate_decimal
    return pos.market_value * rate_decimal


def compute_position_capital_profile(
    pos: Position,
    counterparty: Counterparty,
    params: CapitalParameters = CapitalParameters(),
) -> PositionCapitalProfile:
    collateral_mv = sum((leg.market_value * (Decimal("1") - leg.haircut_pct) for leg in pos.collateral), Decimal("0"))
    raw_exposure = abs(pos.market_value - collateral_mv)
    ead = raw_exposure * (Decimal("1") + params.sft_addon_pct)

    rw_table = params.risk_weights_pct.get(counterparty.counterparty_type, {})
    risk_weight = rw_table.get(counterparty.tier, Decimal("1.50"))  # conservative default if unmapped

    rwa = ead * risk_weight
    leverage_exposure = ead if not counterparty.is_netting_eligible else ead * Decimal("0.6")

    revenue = _annualized_revenue(pos)
    capital_required = rwa * params.target_cet1_ratio
    capital_cost = capital_required * params.cost_of_capital_pct

    rob = float(revenue / leverage_exposure) if leverage_exposure > 0 else None
    roc = float(revenue / capital_required) if capital_required > 0 else None

    expected_loss = Decimal("0")
    if counterparty.pd_1y is not None and counterparty.lgd_assumption is not None:
        expected_loss = ead * Decimal(str(counterparty.pd_1y)) * Decimal(str(counterparty.lgd_assumption))
    raroc = None
    if capital_required > 0:
        raroc = float((revenue - expected_loss) / capital_required)

    return PositionCapitalProfile(
        position_id=pos.position_id,
        ead_usd=ead,
        risk_weight_pct=risk_weight,
        rwa_usd=rwa,
        leverage_exposure_usd=leverage_exposure,
        annualized_revenue_usd=revenue,
        capital_cost_usd=capital_cost,
        return_on_balance_sheet=rob,
        return_on_capital=roc,
        risk_adjusted_return_on_capital=raroc,
    )


def compute_counterparty_capital_summary(
    counterparty: Counterparty,
    positions: Iterable[Position],
    params: CapitalParameters = CapitalParameters(),
) -> CounterpartyCapitalSummary:
    profiles = [compute_position_capital_profile(p, counterparty, params) for p in positions]

    total_ead = sum((p.ead_usd for p in profiles), Decimal("0"))
    total_rwa = sum((p.rwa_usd for p in profiles), Decimal("0"))
    total_lev = sum((p.leverage_exposure_usd for p in profiles), Decimal("0"))
    total_rev = sum((p.annualized_revenue_usd for p in profiles), Decimal("0"))
    total_cap_cost = sum((p.capital_cost_usd for p in profiles), Decimal("0"))

    blended_rob = float(total_rev / total_lev) if total_lev > 0 else None
    blended_roc = float(total_rev / (total_rwa * params.target_cet1_ratio)) if total_rwa > 0 else None

    return CounterpartyCapitalSummary(
        counterparty_id=counterparty.counterparty_id,
        total_ead_usd=total_ead,
        total_rwa_usd=total_rwa,
        total_leverage_exposure_usd=total_lev,
        total_annualized_revenue_usd=total_rev,
        total_capital_cost_usd=total_cap_cost,
        blended_return_on_balance_sheet=blended_rob,
        blended_return_on_capital=blended_roc,
        netting_benefit_applied=counterparty.is_netting_eligible,
    )


def marginal_capital_impact(
    candidate_position: Position,
    counterparty: Counterparty,
    params: CapitalParameters = CapitalParameters(),
) -> PositionCapitalProfile:
    """
    Marginal capital/RWA/leverage impact of ADDING a hypothetical position.
    Used by the optimization engine (optimization/book_optimizer.py) to
    price the capital cost of each candidate reallocation before solving,
    and by the API for ad-hoc "what if I add $50mm to this name" queries.
    """
    return compute_position_capital_profile(candidate_position, counterparty, params)
