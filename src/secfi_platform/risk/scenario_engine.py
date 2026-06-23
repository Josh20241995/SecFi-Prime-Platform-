"""
Scenario analysis engine.

Extends the basic stress scenarios in `risk/counterparty_risk.py`
(which apply uniform shocks to counterparty-level exposure) with a
book-wide, multi-dimensional scenario framework that:

  1. MARKET SCENARIOS: Apply correlated shocks to the entire book —
     price, rate, spread, FX simultaneously — and report P&L impact,
     exposure change, capital impact, and which counterparties are
     most stressed.

  2. EVENT SCENARIOS: Model specific named events:
     - "Top 3 specials recalled simultaneously"
     - "Single largest counterparty defaults"
     - "Repo rate shock +100bp overnight"
     - "GC-to-special migration wave in our top 10 borrows"
     - "Custodian settlement system down for 2 days"

  3. REVERSE STRESS: Given a loss threshold (e.g., the desk's P&L
     buffer or a regulatory stress loss limit), find what combination
     of shocks would reach that threshold — answers "what would it take
     to blow through our risk budget."

  4. SCENARIO COMPARISON: Run multiple scenarios and rank the desk's
     most dangerous exposures across all of them.

Design: all scenarios produce a `ScenarioResult` with a consistent
shape — the same fields regardless of scenario type — so downstream
(reporting, alerting, API) can handle them generically.

Assumption SE-1: "P&L impact" here means MARK-TO-MARKET change in
position economics (fee revenue impact + market value impact + capital
cost change). It does not include credit loss (counterparty default
scenarios produce EXPOSURE at risk, not expected credit loss, which
requires a PD/LGD model this module does not implement — see
docs/assumptions_and_limitations.md R-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Optional

from secfi_platform.common.types import Counterparty, Position
from secfi_platform.risk.counterparty_risk import ShockScenario, _position_net_exposure, DEFAULT_HAIRCUTS_BPS


@dataclass(frozen=True)
class ScenarioDefinition:
    """
    A parameterized scenario definition. Can represent either a market
    shock or an event scenario; `event_filter` is an optional callable
    that selects which positions are affected (e.g., "only top-3-special
    names" or "only positions with counterparty X").
    """
    name: str
    description: str
    shock: ShockScenario
    event_filter: Optional[Callable] = None    # Position -> bool; None means all positions


@dataclass
class PositionScenarioImpact:
    position_id: str
    security_ticker: str
    counterparty_id: str
    base_net_exposure_usd: Decimal
    stressed_net_exposure_usd: Decimal
    exposure_delta_usd: Decimal
    base_annual_revenue_usd: Decimal
    stressed_annual_revenue_usd: Decimal
    revenue_delta_usd: Decimal


@dataclass
class ScenarioResult:
    scenario_name: str
    run_at: datetime
    positions_affected: int
    total_positions: int
    book_nmv_usd: Decimal
    base_gross_exposure_usd: Decimal
    stressed_gross_exposure_usd: Decimal
    exposure_delta_usd: Decimal
    base_total_revenue_usd: Decimal
    stressed_total_revenue_usd: Decimal
    revenue_delta_usd: Decimal
    worst_counterparty_id: Optional[str]
    worst_counterparty_exposure_delta_usd: Optional[Decimal]
    top_position_impacts: list                  # list[PositionScenarioImpact], top 10 by absolute impact
    counterparty_exposure_deltas: dict          # counterparty_id -> delta USD
    description: str


# ---- Standard named event scenarios -----------------------------------------------

def _top_specials_recall_filter(positions: list, specialness_by_security: dict, n: int = 3) -> Callable:
    """Filter for positions in the top N most-special names."""
    from secfi_platform.common.enums import SpecialnessTier
    top_specials = {
        sec_id for sec_id, tier in specialness_by_security.items()
        if tier in (SpecialnessTier.DEEP_SPECIAL, SpecialnessTier.HTB)
    }
    top_n_ids = list(top_specials)[:n]
    def _filter(pos: Position) -> bool:
        return pos.security.internal_id in top_n_ids
    return _filter


STANDARD_EVENT_SCENARIOS = [
    ScenarioDefinition(
        name="EQUITY_MARKET_CRASH_25PCT",
        description="25% equity price decline + 150bp spread widening (2008/2020 style)",
        shock=ShockScenario("EQUITY_MARKET_CRASH_25PCT", equity_price_shock_pct=-0.25, spread_widening_bps=150),
    ),
    ScenarioDefinition(
        name="REPO_RATE_SHOCK_100BP",
        description="Overnight GC repo rate jumps 100bp (2019 repo spike analog)",
        shock=ShockScenario("REPO_RATE_SHOCK_100BP", rate_shock_bps=100),
    ),
    ScenarioDefinition(
        name="GBP_DEVALUATION_15PCT",
        description="GBP depreciates 15% vs USD (2022 gilt crisis analog)",
        shock=ShockScenario("GBP_DEVALUATION_15PCT", fx_shock_pct=-0.15),
    ),
    ScenarioDefinition(
        name="COMBINED_STRESS_REGULATORY",
        description="Regulatory combined stress: equity -15%, rates +75bp, FX +8%, spreads +100bp",
        shock=ShockScenario("COMBINED_STRESS_REGULATORY",
                             equity_price_shock_pct=-0.15, rate_shock_bps=75,
                             fx_shock_pct=0.08, spread_widening_bps=100),
    ),
]


def _compute_position_revenue(pos: Position) -> Decimal:
    """Simplified annualized revenue for scenario P&L estimation."""
    return pos.market_value * pos.rate_bps / Decimal("10000")


def _compute_stressed_revenue(pos: Position, shock: ShockScenario) -> Decimal:
    """Revenue after applying price shock to the market value base."""
    shocked_mv = pos.market_value * Decimal(str(1 + shock.equity_price_shock_pct))
    # Rate shock: repo/reverse-repo positions are directly affected; fee positions less so
    rate_adj = pos.rate_bps + Decimal(str(shock.rate_shock_bps))
    return shocked_mv * rate_adj / Decimal("10000")


def run_scenario(
    scenario: ScenarioDefinition,
    positions: Iterable[Position],
    counterparties: dict,
    fallback_haircuts: dict = DEFAULT_HAIRCUTS_BPS,
) -> ScenarioResult:
    positions = list(positions)
    total = len(positions)
    now = datetime.now(timezone.utc)

    book_nmv = sum((p.market_value for p in positions), Decimal("0"))
    base_gross = sum((abs(_position_net_exposure(p, scenario.shock.__class__("BASE",0,0,0,0), fallback_haircuts))
                      for p in positions), Decimal("0"))
    stressed_gross = sum((abs(_position_net_exposure(p, scenario.shock, fallback_haircuts))
                          for p in positions), Decimal("0"))

    from secfi_platform.risk.counterparty_risk import ShockScenario as _S
    base_shock = _S("BASE", 0.0, 0.0, 0.0, 0.0)

    affected_count = 0
    position_impacts = []
    cpty_deltas: dict = {}
    base_total_rev = Decimal("0")
    stressed_total_rev = Decimal("0")

    for pos in positions:
        if scenario.event_filter is not None and not scenario.event_filter(pos):
            continue
        affected_count += 1

        base_exp = _position_net_exposure(pos, base_shock, fallback_haircuts)
        stressed_exp = _position_net_exposure(pos, scenario.shock, fallback_haircuts)
        base_rev = _compute_position_revenue(pos)
        stressed_rev = _compute_stressed_revenue(pos, scenario.shock)

        base_total_rev += base_rev
        stressed_total_rev += stressed_rev

        cpty = pos.counterparty_id
        cpty_deltas[cpty] = cpty_deltas.get(cpty, Decimal("0")) + (stressed_exp - base_exp)

        impact = PositionScenarioImpact(
            position_id=pos.position_id,
            security_ticker=pos.security.ticker,
            counterparty_id=cpty,
            base_net_exposure_usd=base_exp,
            stressed_net_exposure_usd=stressed_exp,
            exposure_delta_usd=stressed_exp - base_exp,
            base_annual_revenue_usd=base_rev,
            stressed_annual_revenue_usd=stressed_rev,
            revenue_delta_usd=stressed_rev - base_rev,
        )
        position_impacts.append(impact)

    position_impacts.sort(key=lambda i: abs(i.exposure_delta_usd), reverse=True)

    worst_cpty = max(cpty_deltas, key=lambda c: abs(cpty_deltas[c])) if cpty_deltas else None
    worst_delta = cpty_deltas[worst_cpty] if worst_cpty else None

    return ScenarioResult(
        scenario_name=scenario.name,
        run_at=now,
        positions_affected=affected_count,
        total_positions=total,
        book_nmv_usd=book_nmv,
        base_gross_exposure_usd=base_gross,
        stressed_gross_exposure_usd=stressed_gross,
        exposure_delta_usd=stressed_gross - base_gross,
        base_total_revenue_usd=base_total_rev,
        stressed_total_revenue_usd=stressed_total_rev,
        revenue_delta_usd=stressed_total_rev - base_total_rev,
        worst_counterparty_id=worst_cpty,
        worst_counterparty_exposure_delta_usd=worst_delta,
        top_position_impacts=position_impacts[:10],
        counterparty_exposure_deltas=cpty_deltas,
        description=scenario.description,
    )


def run_all_standard_scenarios(
    positions: Iterable[Position],
    counterparties: dict,
    additional_scenarios: Optional[list] = None,
) -> list:
    """Run all standard + any additional scenarios and return results."""
    positions = list(positions)
    all_scenarios = list(STANDARD_EVENT_SCENARIOS)
    if additional_scenarios:
        all_scenarios.extend(additional_scenarios)
    return [run_scenario(s, positions, counterparties) for s in all_scenarios]


def reverse_stress_threshold(
    positions: list,
    counterparties: dict,
    loss_threshold_usd: Decimal,
    shock_parameter: str = "equity_price_shock_pct",
    shock_step: float = 0.01,
    max_shock: float = 0.75,
) -> Optional[dict]:
    """
    Find the smallest shock magnitude (in the named shock parameter)
    that produces a revenue loss >= `loss_threshold_usd`. Returns the
    reverse-stress scenario description or None if the threshold is not
    breached even at `max_shock`.

    This is a simple bisection-compatible linear scan — suitable for
    the 1D case. A full multi-dimensional reverse stress would require
    a proper optimizer and is out of scope here; see docs/algorithms.md.
    """
    base_rev = sum((_compute_position_revenue(p) for p in positions), Decimal("0"))
    shock_magnitude = shock_step
    while shock_magnitude <= max_shock:
        shock_kwargs = {shock_parameter: -shock_magnitude}
        shock = ShockScenario(f"REVERSE_STRESS_{shock_parameter}_{shock_magnitude:.0%}", **shock_kwargs)
        stressed_rev = sum((_compute_stressed_revenue(p, shock) for p in positions), Decimal("0"))
        revenue_loss = base_rev - stressed_rev
        if revenue_loss >= loss_threshold_usd:
            return {
                "shock_parameter": shock_parameter,
                "shock_magnitude": shock_magnitude,
                "revenue_loss_usd": float(revenue_loss),
                "threshold_usd": float(loss_threshold_usd),
                "description": (
                    f"Revenue loss threshold of ${loss_threshold_usd:,.0f} is reached at "
                    f"{shock_parameter} = -{shock_magnitude:.0%}."
                ),
            }
        shock_magnitude += shock_step
    return None


def scenario_comparison_matrix(results: list) -> list:
    """
    Produce a side-by-side matrix of scenario outcomes: one row per
    scenario, one column per metric. Sorted by revenue_delta ascending
    (worst P&L scenario first).
    """
    rows = []
    for r in results:
        rows.append({
            "scenario": r.scenario_name,
            "description": r.description,
            "positions_affected": r.positions_affected,
            "exposure_delta_usd": float(r.exposure_delta_usd),
            "revenue_delta_usd": float(r.revenue_delta_usd),
            "worst_counterparty_id": r.worst_counterparty_id,
            "worst_counterparty_delta_usd": float(r.worst_counterparty_exposure_delta_usd or 0),
        })
    rows.sort(key=lambda r: r["revenue_delta_usd"])
    return rows
