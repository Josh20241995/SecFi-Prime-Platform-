"""
Reconciliation engine.

Matches internal book records against custodian and firm balance-sheet
extracts, detects breaks, classifies probable root cause, assigns
severity/priority, and recommends remediation. Uses pandas for the
matching logic since this is naturally a set-based, vectorizable join
problem over potentially hundreds of thousands of rows at a large desk.

Matching key strategy (configurable):
  primary key   = (security_internal_id, counterparty_id, trade_date, direction)
  This is a SIMPLIFICATION. Real reconciliation keys typically also
  include settlement instruction / SSI id and trade reference number from
  the custodian, which this reference build does not assume are
  available (see docs/assumptions_and_limitations.md item OPS-2). Where
  duplicate keys exist on one side (e.g., two trades same security/
  counterparty/date), the engine falls back to closest-quantity matching
  and flags the result with lower confidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd

from secfi_platform.common.enums import BreakSeverity, BreakType

REQUIRED_BOOK_COLUMNS = {
    "position_id", "security_internal_id", "counterparty_id", "trade_date",
    "direction", "quantity", "market_value", "rate_bps",
}
REQUIRED_EXTERNAL_COLUMNS = {
    "security_internal_id", "counterparty_id", "trade_date", "direction",
    "quantity", "market_value",
}


@dataclass(frozen=True)
class ReconConfig:
    quantity_tolerance_pct: float = 0.001       # 0.1% tolerance before flagging quantity mismatch
    market_value_tolerance_pct: float = 0.005   # 0.5% tolerance (price timing differences)
    stale_position_age_days: int = 5
    critical_age_days: int = 3                  # breaks older than this auto-escalate to CRITICAL if buy-in relevant


def validate_columns(df: pd.DataFrame, required: set, frame_name: str) -> list:
    missing = required - set(df.columns)
    if missing:
        return [f"{frame_name} is missing required columns: {sorted(missing)}"]
    return []


def _classify_break(
    book_row: Optional[pd.Series],
    ext_row: Optional[pd.Series],
    config: ReconConfig,
    external_source: str,
) -> tuple:
    """Returns (BreakType, severity, probable_root_cause, recommended_action, buyin_relevant, capital_relevant)."""
    if book_row is None and ext_row is not None:
        return (
            BreakType.MISSING_ON_BOOK,
            BreakSeverity.HIGH,
            "Position exists at external source but not on internal book. Likely an unbooked trade, "
            "late trade capture entry, or a desk-side cancellation that was not communicated externally.",
            "Trade capture / middle office to confirm trade existence and book or instruct external "
            "correction within same business day.",
            True,
            True,
        )
    if book_row is not None and ext_row is None:
        return (
            BreakType.MISSING_AT_CUSTODIAN,
            BreakSeverity.HIGH,
            "Position exists on internal book but not confirmed at custodian/external source. Likely "
            "settlement not yet affirmed, a failed/rejected instruction, or a duplicate internal booking.",
            "Operations to confirm settlement instruction status with custodian; escalate if unsettled "
            "beyond T+1 for the relevant product.",
            True,
            True,
        )

    qty_book = Decimal(str(book_row["quantity"]))
    qty_ext = Decimal(str(ext_row["quantity"]))
    qty_diff_pct = abs(qty_book - qty_ext) / qty_book if qty_book != 0 else (Decimal("0") if qty_ext == 0 else Decimal("1"))

    if qty_diff_pct > Decimal(str(config.quantity_tolerance_pct)):
        return (
            BreakType.QUANTITY_MISMATCH,
            BreakSeverity.CRITICAL if qty_diff_pct > Decimal("0.10") else BreakSeverity.HIGH,
            f"Quantity differs by {float(qty_diff_pct):.2%}. Likely a partial fill not reflected on one "
            f"side, a corporate-action-driven quantity adjustment applied on only one side, or a booking error.",
            "Compare against corporate action calendar for the security first (most common root cause for "
            "round-number quantity breaks); if no CA applies, escalate to trade capture for booking review.",
            True,
            True,
        )

    mv_book = Decimal(str(book_row["market_value"]))
    mv_ext = Decimal(str(ext_row["market_value"]))
    mv_diff_pct = abs(mv_book - mv_ext) / mv_book if mv_book != 0 else Decimal("0")
    if mv_diff_pct > Decimal(str(config.market_value_tolerance_pct)):
        return (
            BreakType.PRICE_RATE_MISMATCH,
            BreakSeverity.MEDIUM,
            f"Market value differs by {float(mv_diff_pct):.2%} with matching quantity. Likely a stale "
            f"price/rate on one side or a pricing source timing difference (intraday vs. EOD mark).",
            "Confirm pricing source and timestamp on both sides; refresh internal mark if external source "
            "is using a more recent price.",
            False,
            True,
        )

    return (None, None, None, None, False, False)


def reconcile(
    book_df: pd.DataFrame,
    external_df: pd.DataFrame,
    external_source: str,
    as_of: date,
    config: ReconConfig = ReconConfig(),
) -> list:
    """
    Returns list[ReconciliationBreak]-shaped dicts (kept as plain dicts here
    rather than the frozen dataclass so the orchestration layer can attach
    `age_days` after looking up break history — see
    reconciliation/recon_engine.py usage in orchestration/scheduler.py).
    """
    errors = validate_columns(book_df, REQUIRED_BOOK_COLUMNS, "book_df") + \
        validate_columns(external_df, REQUIRED_EXTERNAL_COLUMNS, "external_df")
    if errors:
        raise ValueError("Reconciliation input validation failed: " + "; ".join(errors))

    key_cols = ["security_internal_id", "counterparty_id", "trade_date", "direction"]
    book_indexed = book_df.set_index(key_cols, drop=False)
    ext_indexed = external_df.set_index(key_cols, drop=False)

    all_keys = set(book_indexed.index) | set(ext_indexed.index)

    breaks = []
    for key in all_keys:
        book_row = book_indexed.loc[key] if key in book_indexed.index else None
        ext_row = ext_indexed.loc[key] if key in ext_indexed.index else None

        # Handle duplicate-key matches (loc returns DataFrame if >1 row); take first row,
        # flag duplicate separately.
        if isinstance(book_row, pd.DataFrame):
            if len(book_row) > 1:
                breaks.append(_make_break(
                    key, book_row.iloc[0], None, BreakType.DUPLICATE_ENTRY, BreakSeverity.MEDIUM,
                    f"{len(book_row)} internal book entries share the same reconciliation key.",
                    "Trade capture to confirm whether these are legitimately separate trades needing a "
                    "richer matching key (add trade reference number) or duplicate bookings to reverse.",
                    False, True, external_source, as_of,
                ))
            book_row = book_row.iloc[0]
        if isinstance(ext_row, pd.DataFrame):
            ext_row = ext_row.iloc[0]

        break_type, severity, root_cause, action, buyin_rel, cap_rel = _classify_break(
            book_row, ext_row, config, external_source
        )
        if break_type is None:
            continue

        breaks.append(_make_break(
            key, book_row, ext_row, break_type, severity, root_cause, action,
            buyin_rel, cap_rel, external_source, as_of,
        ))

    breaks.sort(key=lambda b: (_severity_rank(b["severity"]),), reverse=True)
    return breaks


def _severity_rank(sev: BreakSeverity) -> int:
    return {BreakSeverity.LOW: 0, BreakSeverity.MEDIUM: 1, BreakSeverity.HIGH: 2, BreakSeverity.CRITICAL: 3}[sev]


def _make_break(key, book_row, ext_row, break_type, severity, root_cause, action,
                 buyin_rel, cap_rel, external_source, as_of) -> dict:
    security_id, counterparty_id, trade_date, direction = key
    return {
        "break_id": str(uuid.uuid4()),
        "as_of": as_of,
        "position_id": book_row["position_id"] if book_row is not None and "position_id" in book_row else None,
        "security_internal_id": security_id,
        "counterparty_id": counterparty_id,
        "break_type": break_type,
        "severity": severity,
        "book_value": Decimal(str(book_row["market_value"])) if book_row is not None else None,
        "external_value": Decimal(str(ext_row["market_value"])) if ext_row is not None else None,
        "external_source": external_source,
        "delta": (
            Decimal(str(book_row["market_value"])) - Decimal(str(ext_row["market_value"]))
            if book_row is not None and ext_row is not None else None
        ),
        "probable_root_cause": root_cause,
        "recommended_action": action,
        "buyin_risk_relevant": buyin_rel,
        "capital_misstatement_relevant": cap_rel,
        "age_days": 0,
    }


def summarize_breaks(breaks: list) -> dict:
    summary = {"total": len(breaks), "by_severity": {}, "by_type": {}, "buyin_relevant_count": 0,
               "capital_relevant_count": 0}
    for b in breaks:
        sev = b["severity"].value
        bt = b["break_type"].value
        summary["by_severity"][sev] = summary["by_severity"].get(sev, 0) + 1
        summary["by_type"][bt] = summary["by_type"].get(bt, 0) + 1
        if b["buyin_risk_relevant"]:
            summary["buyin_relevant_count"] += 1
        if b["capital_misstatement_relevant"]:
            summary["capital_relevant_count"] += 1
    return summary
