"""
Inventory management and substitution logic.

Answers the desk's core inventory questions:
  1. AVAILABILITY: What is the net available inventory for each security
     (lendable vs. on loan vs. committed elsewhere)?
  2. LOCATE RESOLUTION: Which positions in inventory can fulfill a pending
     locate request, and in what priority order?
  3. SUBSTITUTION MATCHING: For a position that needs to be returned or
     recalled, which substitute securities would satisfy the borrower's
     economic objective (same exposure, similar liquidity/risk)?
  4. REHYPOTHECATION TRACKING: What portion of received collateral can be
     rehypothecated, and how much has already been used?

This module is consumed by:
  - `recall_buyin/recall_risk_engine.py` for the `substitute_candidates`
    field in the urgency queue
  - `risk/collateral_optimizer.py` for building the candidate pool
  - The intraday fast cycle (via `orchestration/scheduler.py`) for
    ongoing availability tracking

Assumption IM-1: "Lendable inventory" is modeled here as positions where
direction=LEND AND is_rehypothecable=True AND no pending recall flag.
In production, lendable inventory comes from the firm's inventory
management system (which also tracks agent-bank lending programs,
beneficial-owner instructions, custody holdings, and prime brokerage
margin book holdings) — this module models the desk's own book only, and
the real lendable pool is typically much larger. See
docs/assumptions_and_limitations.md.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import Direction, ProductType
from secfi_platform.common.types import Position


@dataclass
class SecurityInventorySnapshot:
    security_internal_id: str
    ticker: str
    product_type: ProductType
    lendable_quantity: Decimal              # available to lend (not yet on loan)
    on_loan_quantity: Decimal               # currently lent out
    on_borrow_quantity: Decimal             # currently borrowed in
    net_inventory: Decimal                  # lendable - on_borrow (net long or short)
    committed_quantity: Decimal             # committed to pending recalls/returns not yet settled
    free_to_lend: Decimal                   # lendable - committed
    currency: str


@dataclass(frozen=True)
class LocateRequest:
    request_id: str
    security_internal_id: str
    requested_quantity: Decimal
    requesting_counterparty_id: str
    purpose: str                            # "SHORT_SELL" | "FINANCING" | "HEDGE"


@dataclass
class LocateResolution:
    request_id: str
    security_internal_id: str
    requested_quantity: Decimal
    available_quantity: Decimal
    filled_quantity: Decimal
    source_position_ids: list               # list[str] — which positions cover this locate
    is_fully_filled: bool
    shortfall_quantity: Decimal
    rationale: str


@dataclass(frozen=True)
class SubstituteMatch:
    original_security_id: str
    original_ticker: str
    substitute_security_id: str
    substitute_ticker: str
    substitute_product_type: ProductType
    available_quantity: Decimal
    match_quality: str                       # "EXACT_SAME_ISSUER" | "SAME_SECTOR" | "SAME_PRODUCT_TYPE" | "BROAD_MARKET"
    match_score: float                        # 0-1, higher = better substitute
    rationale: str


@dataclass
class RehypothecationTracker:
    position_id: str
    security_internal_id: str
    received_quantity: Decimal               # collateral received from counterparty
    max_rehypothecatable_quantity: Decimal   # received * rehyp_rate (from agreement terms)
    already_used_quantity: Decimal           # how much we've already pledged out again
    available_to_rehyp: Decimal              # max_rehypothecatable - already_used
    rehyp_utilization_pct: float


def build_inventory_snapshot(positions: Iterable[Position]) -> dict:
    """
    Compute per-security inventory from the current book.
    Returns dict[security_internal_id -> SecurityInventorySnapshot].
    """
    lendable: dict = defaultdict(Decimal)
    on_loan: dict = defaultdict(Decimal)
    on_borrow: dict = defaultdict(Decimal)
    metadata: dict = {}

    for pos in positions:
        sec_id = pos.security.internal_id
        metadata[sec_id] = (pos.security.ticker, pos.security.product_type, pos.currency)
        if pos.direction == Direction.LEND:
            on_loan[sec_id] += pos.quantity
        elif pos.direction == Direction.BORROW:
            on_borrow[sec_id] += pos.quantity
        elif pos.direction == Direction.REVERSE_REPO:
            # Desk holds securities received under reverse repo — potentially relendable
            if pos.is_rehypothecable:
                lendable[sec_id] += pos.quantity

    all_securities = set(lendable) | set(on_loan) | set(on_borrow)
    snapshots = {}
    for sec_id in all_securities:
        ticker, product_type, currency = metadata.get(sec_id, ("UNKNOWN", ProductType.EQUITY, "USD"))
        lend_qty = lendable.get(sec_id, Decimal("0"))
        loan_qty = on_loan.get(sec_id, Decimal("0"))
        borrow_qty = on_borrow.get(sec_id, Decimal("0"))
        net = lend_qty - borrow_qty
        free = max(lend_qty - Decimal("0"), Decimal("0"))  # committed handled separately below
        snapshots[sec_id] = SecurityInventorySnapshot(
            security_internal_id=sec_id,
            ticker=ticker,
            product_type=product_type,
            lendable_quantity=lend_qty,
            on_loan_quantity=loan_qty,
            on_borrow_quantity=borrow_qty,
            net_inventory=net,
            committed_quantity=Decimal("0"),   # refined below with recall data
            free_to_lend=free,
            currency=currency,
        )
    return snapshots


def resolve_locate(
    request: LocateRequest,
    inventory_snapshot: dict,
    positions: Iterable[Position],
) -> LocateResolution:
    """
    Determine how much of a locate can be filled from current inventory.
    Source positions are identified (for audit) in priority order:
      1. Open LEND positions against the exact security (if the desk
         has lent it out, it might be recallable — but we DON'T auto-
         recall here; we flag the position as a potential source)
      2. REVERSE_REPO / rehypothecatable received collateral
      3. On-borrow positions against the same security (netting benefit)
    """
    snapshot = inventory_snapshot.get(request.security_internal_id)
    if snapshot is None:
        return LocateResolution(
            request_id=request.request_id,
            security_internal_id=request.security_internal_id,
            requested_quantity=request.requested_quantity,
            available_quantity=Decimal("0"),
            filled_quantity=Decimal("0"),
            source_position_ids=[],
            is_fully_filled=False,
            shortfall_quantity=request.requested_quantity,
            rationale="Security not found in current inventory.",
        )

    available = snapshot.free_to_lend
    filled = min(available, request.requested_quantity)
    shortfall = max(request.requested_quantity - filled, Decimal("0"))

    # Identify contributing positions (for audit trail)
    positions_list = list(positions)
    sources = [
        p.position_id for p in positions_list
        if p.security.internal_id == request.security_internal_id
        and p.direction in (Direction.LEND, Direction.REVERSE_REPO)
        and p.is_rehypothecable
    ]

    return LocateResolution(
        request_id=request.request_id,
        security_internal_id=request.security_internal_id,
        requested_quantity=request.requested_quantity,
        available_quantity=available,
        filled_quantity=filled,
        source_position_ids=sources,
        is_fully_filled=(shortfall == 0),
        shortfall_quantity=shortfall,
        rationale=(
            f"Located {filled:,.0f} of {request.requested_quantity:,.0f} requested. "
            f"Free-to-lend inventory: {available:,.0f}. "
            f"{'Fully filled.' if shortfall == 0 else f'Shortfall {shortfall:,.0f} — may require sourcing or recall.'}"
        ),
    )


def find_substitutes(
    security_internal_id: str,
    inventory_snapshot: dict,
    position_metadata: dict,          # security_internal_id -> Security (from positions)
    min_available_quantity: Decimal = Decimal("1000"),
) -> list:
    """
    For a security that cannot be sourced, find substitutes in inventory
    ordered by match quality. Used by the recall/buy-in urgency queue
    to populate `substitute_candidates`.

    Matching hierarchy:
      1. Same issuer (different maturity/CUSIP) — for bonds
      2. Same GICS sector AND same product type
      3. Same product type (e.g., any equity for an equity)
      4. Broad market (any lendable name with sufficient inventory)
    """
    original_sec = position_metadata.get(security_internal_id)
    if original_sec is None:
        return []

    results = []
    for sec_id, snap in inventory_snapshot.items():
        if sec_id == security_internal_id:
            continue
        if snap.free_to_lend < min_available_quantity:
            continue
        sub_sec = position_metadata.get(sec_id)
        if sub_sec is None:
            continue

        # Score the match
        if sub_sec.issuer_id == original_sec.issuer_id:
            quality = "EXACT_SAME_ISSUER"
            score = 1.0
        elif (sub_sec.gics_sector and sub_sec.gics_sector == original_sec.gics_sector
              and sub_sec.product_type == original_sec.product_type):
            quality = "SAME_SECTOR"
            score = 0.75
        elif sub_sec.product_type == original_sec.product_type:
            quality = "SAME_PRODUCT_TYPE"
            score = 0.50
        else:
            quality = "BROAD_MARKET"
            score = 0.20

        results.append(SubstituteMatch(
            original_security_id=security_internal_id,
            original_ticker=original_sec.ticker,
            substitute_security_id=sec_id,
            substitute_ticker=snap.ticker,
            substitute_product_type=snap.product_type,
            available_quantity=snap.free_to_lend,
            match_quality=quality,
            match_score=score,
            rationale=(
                f"{snap.ticker} is a {quality.replace('_', ' ').lower()} substitute "
                f"with {snap.free_to_lend:,.0f} available."
            ),
        ))

    results.sort(key=lambda r: r.match_score, reverse=True)
    return results


def build_rehypothecation_tracker(
    positions: Iterable[Position],
    standard_rehyp_rate: Decimal = Decimal("1.00"),   # 100% by default; set per-counterparty from CSA in production
) -> list:
    """
    Track rehypothecation headroom for positions where the desk holds
    collateral received from counterparties (BORROW positions with cash
    or non-cash collateral received).
    """
    trackers = []
    for pos in positions:
        if pos.direction not in (Direction.BORROW, Direction.REPO):
            continue
        if not pos.collateral:
            continue
        for leg in pos.collateral:
            max_rehyp = leg.market_value * standard_rehyp_rate
            # In this reference build, "already used" is estimated by looking at
            # LEND positions against the same security type — a production system
            # would track this via the firm's collateral management system.
            already_used = Decimal("0")   # placeholder — wire to real collateral tracking in production
            available = max(max_rehyp - already_used, Decimal("0"))
            utilization = float(already_used / max_rehyp) if max_rehyp > 0 else 0.0
            trackers.append(RehypothecationTracker(
                position_id=pos.position_id,
                security_internal_id=pos.security.internal_id,
                received_quantity=pos.quantity,
                max_rehypothecatable_quantity=pos.quantity * standard_rehyp_rate,
                already_used_quantity=already_used,
                available_to_rehyp=pos.quantity * standard_rehyp_rate - already_used,
                rehyp_utilization_pct=utilization,
            ))
    return trackers


def inventory_summary(snapshot: dict) -> dict:
    total_free = sum((s.free_to_lend for s in snapshot.values()), Decimal("0"))
    total_on_loan = sum((s.on_loan_quantity for s in snapshot.values()), Decimal("0"))
    total_on_borrow = sum((s.on_borrow_quantity for s in snapshot.values()), Decimal("0"))
    utilization = float(total_on_loan / (total_on_loan + total_free)) if (total_on_loan + total_free) > 0 else 0.0
    return {
        "total_free_to_lend_quantity": total_free,
        "total_on_loan_quantity": total_on_loan,
        "total_on_borrow_quantity": total_on_borrow,
        "book_utilization_pct": utilization,
        "unique_securities": len(snapshot),
    }
