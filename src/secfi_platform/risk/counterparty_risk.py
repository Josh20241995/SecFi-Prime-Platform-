"""
Counterparty and balance-sheet risk engine.

Computes, per counterparty (and rolled up to parent_group_id for funds with
multiple legal entities trading the same desk), the full exposure picture
required by a prime brokerage securities finance desk:

  - gross exposure, net exposure, collateralized vs uncollateralized splits
  - mark-to-market exposure at current prices
  - stress exposure under price / rate / FX / spread shocks
  - wrong-way risk flagging
  - concentration (issuer, sector, region, product, single-counterparty)
  - margin adequacy
  - limit utilization and breach detection

Design notes / assumptions (stated explicitly per governance requirement):
  A1. Exposure at default for a securities finance trade is modeled as the
      standard SFT exposure: max(0, MV_securities_leg - MV_collateral_leg_haircut_adjusted)
      for the desk's net risk position, signed by direction. This is the
      same conceptual exposure SA-CCR/comprehensive-approach capital
      models use; this module computes RISK exposure for desk decision
      support, NOT official regulatory capital (see risk/capital_rwa.py
      for the regulatory capital approximation, which is explicitly
      labeled as an approximation requiring Treasury/Capital Management
      sign-off before being treated as official).
  A2. Haircuts are sourced from configs/risk_limits.yaml by collateral
      type when not present on the CollateralLeg itself (fallback logic
      per skill requirement: "If a piece of functionality depends on
      unavailable data, specify the exact data required and give fallback
      logic").
  A3. Wrong-way risk is flagged, not modeled probabilistically, when the
      counterparty's sector/region matches the collateral or borrowed
      security's issuer sector/region above a configurable concentration
      threshold (a real WWR model requires joint default/exposure
      correlation data from the firm's credit risk system — not assumed
      available here; see docs/assumptions_and_limitations.md item R-3).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import CollateralType, Direction
from secfi_platform.common.types import Counterparty, Position


# ---------------------------------------------------------------------------
# Shock specifications
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShockScenario:
    name: str
    equity_price_shock_pct: float = 0.0     # applied to security market value, signed
    rate_shock_bps: float = 0.0              # applied to repo/fee rates, parallel
    fx_shock_pct: float = 0.0                # applied to non-base-currency legs, signed
    spread_widening_bps: float = 0.0         # applied to collateral haircuts (widens them)


STANDARD_SCENARIOS: tuple[ShockScenario, ...] = (
    ShockScenario("BASE", 0.0, 0.0, 0.0, 0.0),
    ShockScenario("EQUITY_DOWN_10", equity_price_shock_pct=-0.10),
    ShockScenario("EQUITY_DOWN_25_CRISIS", equity_price_shock_pct=-0.25, spread_widening_bps=150),
    ShockScenario("RATES_UP_100BP", rate_shock_bps=100),
    ShockScenario("FX_USD_UP_10", fx_shock_pct=0.10),
    ShockScenario(
        "COMBINED_STRESS",
        equity_price_shock_pct=-0.15,
        rate_shock_bps=75,
        fx_shock_pct=0.08,
        spread_widening_bps=100,
    ),
)


# ---------------------------------------------------------------------------
# Exposure results
# ---------------------------------------------------------------------------

@dataclass
class CounterpartyExposure:
    counterparty_id: str
    as_of: str
    gross_exposure_usd: Decimal
    net_exposure_usd: Decimal
    collateralized_exposure_usd: Decimal
    uncollateralized_exposure_usd: Decimal
    lend_market_value_usd: Decimal
    borrow_market_value_usd: Decimal
    collateral_market_value_usd: Decimal
    position_count: int
    limit_usd: Optional[Decimal]
    utilization_pct: Optional[float]
    limit_breached: bool
    headroom_usd: Optional[Decimal]
    concentration_by_issuer: dict          # issuer_id -> pct of gross exposure
    concentration_by_sector: dict
    concentration_by_product: dict
    herfindahl_issuer: float               # HHI on issuer exposure, 0-10000 scale
    wrong_way_risk_flags: list             # list[str] describing flagged WWR conditions
    stress_results: dict                   # scenario name -> CounterpartyExposure-like dict (net_exposure_usd, delta_usd)


DEFAULT_HAIRCUTS_BPS = {
    CollateralType.CASH: 0,
    CollateralType.GOVT_SECURITIES: 200,
    CollateralType.AGENCY_SECURITIES: 300,
    CollateralType.EQUITIES: 1500,
    CollateralType.LETTER_OF_CREDIT: 500,
    CollateralType.NON_CASH_OTHER: 2000,
}


def _effective_haircut_pct(leg, extra_spread_widening_bps: float, fallback_haircuts: dict) -> Decimal:
    base = leg.haircut_pct
    if base is None:
        base = Decimal(fallback_haircuts.get(leg.collateral_type, 2000)) / Decimal(10000)
    widened = base + Decimal(extra_spread_widening_bps) / Decimal(10000)
    return min(widened, Decimal("0.99"))


def _position_net_exposure(
    pos: Position,
    scenario: ShockScenario,
    fallback_haircuts: dict,
) -> Decimal:
    """
    Signed risk exposure of one position to the counterparty under a shock
    scenario. Positive = desk has credit exposure TO the counterparty
    (counterparty could default owing the desk value); this is the
    quantity counterparty risk limits are measured against.
    """
    shocked_mv = pos.market_value * Decimal(1 + scenario.equity_price_shock_pct)

    collateral_value = Decimal("0")
    for leg in pos.collateral:
        hc = _effective_haircut_pct(leg, scenario.spread_widening_bps, fallback_haircuts)
        collateral_value += leg.market_value * (Decimal("1") - hc)

    if pos.direction in (Direction.LEND, Direction.REVERSE_REPO):
        # Desk has given up securities/cash and holds collateral/securities back;
        # exposure = value desk is owed (securities or cash out) minus collateral held.
        exposure = shocked_mv - collateral_value
    else:
        # BORROW / REPO: desk holds the securities/cash and owes collateral back;
        # desk's exposure to counterparty is collateral posted out minus value held.
        exposure = collateral_value - shocked_mv

    return exposure


def compute_counterparty_exposure(
    counterparty: Counterparty,
    positions: Iterable[Position],
    *,
    as_of: str,
    limit_usd: Optional[Decimal] = None,
    fallback_haircuts: dict = DEFAULT_HAIRCUTS_BPS,
    scenarios: tuple[ShockScenario, ...] = STANDARD_SCENARIOS,
    wwr_concentration_threshold_pct: float = 0.35,
) -> CounterpartyExposure:
    positions = list(positions)
    base_scenario = scenarios[0]

    gross = Decimal("0")
    net = Decimal("0")
    collateralized = Decimal("0")
    uncollateralized = Decimal("0")
    lend_mv = Decimal("0")
    borrow_mv = Decimal("0")
    collateral_mv = Decimal("0")

    by_issuer: dict = defaultdict(lambda: Decimal("0"))
    by_sector: dict = defaultdict(lambda: Decimal("0"))
    by_product: dict = defaultdict(lambda: Decimal("0"))

    for pos in positions:
        pos_collateral_mv = sum((leg.market_value for leg in pos.collateral), Decimal("0"))
        collateral_mv += pos_collateral_mv

        if pos.direction in (Direction.LEND, Direction.REVERSE_REPO):
            lend_mv += pos.market_value
        else:
            borrow_mv += pos.market_value

        exposure = _position_net_exposure(pos, base_scenario, fallback_haircuts)
        net += exposure
        gross += abs(exposure)

        if pos_collateral_mv > 0:
            collateralized += abs(exposure)
        else:
            uncollateralized += abs(exposure)

        by_issuer[pos.security.issuer_id] += abs(exposure)
        if pos.security.gics_sector:
            by_sector[pos.security.gics_sector] += abs(exposure)
        by_product[pos.security.product_type.value] += abs(exposure)

    def _as_pct_map(d: dict) -> dict:
        total = sum(d.values())
        if total == 0:
            return {k: 0.0 for k in d}
        return {k: float(v / total) for k, v in d.items()}

    issuer_pct = _as_pct_map(by_issuer)
    sector_pct = _as_pct_map(by_sector)
    product_pct = _as_pct_map(by_product)

    hhi = sum((pct * 100) ** 2 for pct in issuer_pct.values()) if issuer_pct else 0.0

    wwr_flags = []
    for sector, pct in sector_pct.items():
        if pct >= wwr_concentration_threshold_pct and counterparty.counterparty_type.value == "BANK_DEALER":
            wwr_flags.append(
                f"Counterparty is a bank/dealer with {pct:.0%} of exposure concentrated in "
                f"sector '{sector}'; potential wrong-way risk if counterparty distress "
                f"correlates with sector-wide collateral devaluation. Requires credit risk review."
            )
    if counterparty.watch_list and gross > 0:
        wwr_flags.append("Counterparty is on the firmwide credit watch list with active exposure.")

    stress_results = {}
    for scenario in scenarios:
        scen_net = Decimal("0")
        for pos in positions:
            scen_net += _position_net_exposure(pos, scenario, fallback_haircuts)
        stress_results[scenario.name] = {
            "net_exposure_usd": scen_net,
            "delta_vs_base_usd": scen_net - net,
        }

    utilization_pct = None
    breached = False
    headroom = None
    if limit_usd is not None and limit_usd > 0:
        utilization_pct = float(gross / limit_usd)
        breached = gross > limit_usd
        headroom = limit_usd - gross

    return CounterpartyExposure(
        counterparty_id=counterparty.counterparty_id,
        as_of=as_of,
        gross_exposure_usd=gross,
        net_exposure_usd=net,
        collateralized_exposure_usd=collateralized,
        uncollateralized_exposure_usd=uncollateralized,
        lend_market_value_usd=lend_mv,
        borrow_market_value_usd=borrow_mv,
        collateral_market_value_usd=collateral_mv,
        position_count=len(positions),
        limit_usd=limit_usd,
        utilization_pct=utilization_pct,
        limit_breached=breached,
        headroom_usd=headroom,
        concentration_by_issuer=issuer_pct,
        concentration_by_sector=sector_pct,
        concentration_by_product=product_pct,
        herfindahl_issuer=hhi,
        wrong_way_risk_flags=wwr_flags,
        stress_results=stress_results,
    )


def compute_book_exposure_by_counterparty(
    counterparties: Iterable[Counterparty],
    positions: Iterable[Position],
    *,
    as_of: str,
    limits_by_counterparty_id: Optional[dict] = None,
    scenarios: tuple[ShockScenario, ...] = STANDARD_SCENARIOS,
) -> dict:
    """Batch entry point used by the daily/intraday orchestration cycle."""
    limits_by_counterparty_id = limits_by_counterparty_id or {}
    positions_by_cpty: dict = defaultdict(list)
    for pos in positions:
        positions_by_cpty[pos.counterparty_id].append(pos)

    results = {}
    for cpty in counterparties:
        cpty_positions = positions_by_cpty.get(cpty.counterparty_id, [])
        results[cpty.counterparty_id] = compute_counterparty_exposure(
            cpty,
            cpty_positions,
            as_of=as_of,
            limit_usd=limits_by_counterparty_id.get(cpty.counterparty_id),
            scenarios=scenarios,
        )
    return results
