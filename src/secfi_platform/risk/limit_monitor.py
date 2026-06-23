"""
Intraday limit monitoring engine.

Tracks counterparty exposure utilization continuously during the trading
day, answers "will we breach a limit if we do X more with counterparty Y,"
and produces a ranked list of counterparties approaching limits that the
desk head and risk management should watch.

This module provides the analytical layer; the actual trade-level gate
(blocking a booking if it would cause a breach) lives in the firm's risk
system or trade capture system, not here — this platform's role is
decision support and alerting, not execution control. See
docs/governance.md "Core principle."

Key functions:
  - compute_limit_utilization_dashboard: current utilization per
    counterparty, ranked by proximity to limit
  - simulate_incremental_exposure: "what does adding $X to counterparty Y
    do to their utilization?" (used by the optimization engine and by
    the API for ad-hoc what-if queries)
  - predict_limit_breach: simple linear extrapolation of intraday
    exposure trajectory to predict likely EOD utilization based on
    current trend (requires intraday exposure history — production
    implementation would read this from secfi.position_lifecycle_event
    or a time-series store; this reference build models the interface)

Assumption LM-1: Exposure is computed as gross market value of all
active positions with the counterparty (simplified vs. the full haircut-
adjusted calculation in risk/counterparty_risk.py). This is intentional
for the limit-monitoring use case: the gross metric is faster to compute,
and a monitoring tool that deliberately uses a CONSERVATIVE measure
(gross > net) is preferable to one that uses a net measure and might
miss a breach due to collateral assumptions being wrong intraday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import CounterpartyTier
from secfi_platform.common.types import Counterparty, Position


@dataclass
class LimitUtilizationRow:
    counterparty_id: str
    legal_name: str
    tier: CounterpartyTier
    gross_exposure_usd: Decimal
    limit_usd: Optional[Decimal]
    utilization_pct: Optional[float]
    headroom_usd: Optional[Decimal]
    status: str                                    # "GREEN" | "AMBER" | "RED" | "BREACH"
    days_since_last_review: Optional[int]
    watch_list: bool


@dataclass
class IncrementalExposureImpact:
    counterparty_id: str
    current_gross_usd: Decimal
    incremental_usd: Decimal
    new_gross_usd: Decimal
    current_utilization_pct: Optional[float]
    new_utilization_pct: Optional[float]
    headroom_remaining_usd: Optional[Decimal]
    would_breach: bool
    would_enter_amber: bool                        # crosses 85% but not 100%
    recommendation: str                             # plain English: "PROCEED" | "CAUTION" | "DECLINE"


@dataclass
class LimitBreachEvent:
    counterparty_id: str
    detected_at: datetime
    current_exposure_usd: Decimal
    limit_usd: Decimal
    overage_usd: Decimal
    overage_pct: float
    breach_type: str                               # "HARD_BREACH" (>100%) | "SOFT_BREACH" (>amber threshold)


# Configurable thresholds — in production, source from configs/base.yaml
AMBER_THRESHOLD_PCT = 0.85
RED_THRESHOLD_PCT = 0.95
BREACH_THRESHOLD_PCT = 1.00


def _status(utilization_pct: Optional[float]) -> str:
    if utilization_pct is None:
        return "NO_LIMIT"
    if utilization_pct >= BREACH_THRESHOLD_PCT:
        return "BREACH"
    if utilization_pct >= RED_THRESHOLD_PCT:
        return "RED"
    if utilization_pct >= AMBER_THRESHOLD_PCT:
        return "AMBER"
    return "GREEN"


def compute_limit_utilization_dashboard(
    counterparties: Iterable[Counterparty],
    positions: Iterable[Position],
    counterparty_limits_usd: dict,
    last_review_dates: Optional[dict] = None,    # counterparty_id -> date
) -> list:
    """
    Build a ranked utilization dashboard: one row per counterparty,
    sorted by utilization descending (breaches and near-breaches first).
    """
    positions_list = list(positions)
    exposure_by_cpty: dict = {}
    for pos in positions_list:
        exposure_by_cpty[pos.counterparty_id] = (
            exposure_by_cpty.get(pos.counterparty_id, Decimal("0")) + pos.market_value
        )

    rows = []
    now = datetime.now(timezone.utc).date()
    for cpty in counterparties:
        gross = exposure_by_cpty.get(cpty.counterparty_id, Decimal("0"))
        limit = counterparty_limits_usd.get(cpty.counterparty_id)
        util = float(gross / limit) if limit and limit > 0 else None
        headroom = (limit - gross) if limit else None

        last_review = (last_review_dates or {}).get(cpty.counterparty_id)
        days_since = (now - last_review).days if last_review else None

        rows.append(LimitUtilizationRow(
            counterparty_id=cpty.counterparty_id,
            legal_name=cpty.legal_name,
            tier=cpty.tier,
            gross_exposure_usd=gross,
            limit_usd=limit,
            utilization_pct=util,
            headroom_usd=headroom,
            status=_status(util),
            days_since_last_review=days_since,
            watch_list=cpty.watch_list,
        ))

    # Sort: BREACH first, then RED, AMBER, GREEN; within status by utilization desc
    status_rank = {"BREACH": 0, "RED": 1, "AMBER": 2, "GREEN": 3, "NO_LIMIT": 4}
    rows.sort(key=lambda r: (status_rank.get(r.status, 99), -(r.utilization_pct or 0)))
    return rows


def simulate_incremental_exposure(
    counterparty_id: str,
    incremental_usd: Decimal,
    current_dashboard: list,
    counterparty_limits_usd: dict,
) -> IncrementalExposureImpact:
    """
    Model the effect of adding `incremental_usd` of exposure to a
    counterparty on its limit utilization. Used by:
      - `optimization/book_optimizer.py` before proposing a REROUTE
      - The API (`GET /v1/limits/simulate-incremental`) for desk what-if
      - The intraday fast cycle's pre-trade check advisory
    """
    current_row = next((r for r in current_dashboard if r.counterparty_id == counterparty_id), None)
    current_gross = current_row.gross_exposure_usd if current_row else Decimal("0")
    limit = counterparty_limits_usd.get(counterparty_id)
    current_util = float(current_gross / limit) if limit and limit > 0 else None
    new_gross = current_gross + incremental_usd
    new_util = float(new_gross / limit) if limit and limit > 0 else None

    would_breach = new_util is not None and new_util >= BREACH_THRESHOLD_PCT
    would_amber = new_util is not None and new_util >= AMBER_THRESHOLD_PCT and not would_breach
    headroom = (limit - new_gross) if limit else None

    if would_breach:
        reco = f"DECLINE — would breach limit (new utilization {new_util:.1%})"
    elif would_amber:
        reco = f"CAUTION — would enter amber zone ({new_util:.1%}); obtain risk sign-off"
    else:
        reco = f"PROCEED — utilization would be {new_util:.1%}" if new_util else "PROCEED — no limit configured"

    return IncrementalExposureImpact(
        counterparty_id=counterparty_id,
        current_gross_usd=current_gross,
        incremental_usd=incremental_usd,
        new_gross_usd=new_gross,
        current_utilization_pct=current_util,
        new_utilization_pct=new_util,
        headroom_remaining_usd=headroom,
        would_breach=would_breach,
        would_enter_amber=would_amber,
        recommendation=reco,
    )


def detect_limit_breaches(dashboard: list) -> list:
    """Extract all current breaches and near-breaches as LimitBreachEvent objects."""
    events = []
    now = datetime.now(timezone.utc)
    for row in dashboard:
        if row.status in ("BREACH", "RED") and row.limit_usd and row.utilization_pct:
            overage = row.gross_exposure_usd - row.limit_usd
            events.append(LimitBreachEvent(
                counterparty_id=row.counterparty_id,
                detected_at=now,
                current_exposure_usd=row.gross_exposure_usd,
                limit_usd=row.limit_usd,
                overage_usd=max(overage, Decimal("0")),
                overage_pct=max(float(overage / row.limit_usd), 0.0),
                breach_type="HARD_BREACH" if row.status == "BREACH" else "SOFT_BREACH",
            ))
    return events


def limits_summary(dashboard: list) -> dict:
    status_counts = {}
    for row in dashboard:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    counterparties_with_no_limit = sum(1 for r in dashboard if r.limit_usd is None)
    return {
        "total_counterparties": len(dashboard),
        "by_status": status_counts,
        "counterparties_with_no_configured_limit": counterparties_with_no_limit,
        "watch_list_with_active_exposure": sum(
            1 for r in dashboard if r.watch_list and r.gross_exposure_usd > 0
        ),
    }
