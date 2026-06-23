"""
Exception management engine.

Institutional desks routinely operate with exceptions — situations where
the standard rules (pricing thresholds, exposure limits, data quality
gates) are knowingly overridden by a human with appropriate authority.
This module tracks those exceptions throughout their lifecycle:

  PRICING EXCEPTIONS
    A position priced outside the desk's approved bandwidth (too far
    above or below the market composite) that has been flagged by the
    pricing engine but is being kept at the off-market rate for a
    documented business reason (e.g., a strategic relationship rate,
    a guaranteed rate in a term agreement, or a corrective re-rate
    in progress).

  LIMIT EXCEPTIONS
    A counterparty exposure that exceeds its configured limit but has
    been approved for a specific period by the risk committee or credit
    risk team (e.g., to support a large client-driven flow that
    temporarily breaches the line).

  DATA QUALITY EXCEPTIONS
    A position or market-data record that fails data quality validation
    but has been reviewed by operations and accepted as correct-despite-
    appearing-anomalous (e.g., a freshly-issued security with no market
    rate history, treated as GC until a rate is established).

All exceptions share a common lifecycle:
    OPEN -> APPROVED (by a named approver) -> ACTIVE -> EXPIRED | CLOSED

No exception auto-approves. Approval requires a `decided_by` field and
writes an immutable audit record per docs/governance.md.

This module provides the state machine and validation logic only —
persistence is to secfi.exception_log (see sql/schemas/09_exceptions.sql,
which would be added to the schema set in production; this reference
build models the data contract, not the SQL file, to stay focused on
the logic layer).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from secfi_platform.common.enums import ApprovalStatus, BreakSeverity


class ExceptionLifecycleError(Exception):
    pass


@dataclass
class ExceptionRecord:
    """
    Universal exception record shared by all exception types.
    Type-specific detail lives in `detail` (a dict), keeping a single
    table schema without needing a discriminated union of SQL tables.
    """
    exception_id: str
    exception_type: str                             # "PRICING" | "LIMIT" | "DATA_QUALITY"
    raised_at: datetime
    raised_by: str                                   # user or engine that detected the exception
    description: str
    severity: BreakSeverity
    target_type: str                                  # "POSITION" | "COUNTERPARTY" | "SECURITY"
    target_id: str
    status: ApprovalStatus                            # PROPOSED -> APPROVED -> EXECUTED (=ACTIVE)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    expiry_date: Optional[date] = None               # date after which exception auto-expires
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    detail: dict = field(default_factory=dict)        # type-specific metadata

    def is_active(self) -> bool:
        if self.status != ApprovalStatus.EXECUTED:   # EXECUTED = ACTIVE in exception context
            return False
        if self.expiry_date and self.expiry_date < datetime.now(timezone.utc).date():
            return False
        return self.closed_at is None

    def is_expired(self) -> bool:
        if self.expiry_date is None:
            return False
        return self.expiry_date < datetime.now(timezone.utc).date()

    def approve(self, decided_by: str, expiry_date: Optional[date] = None) -> None:
        if self.status not in (ApprovalStatus.PROPOSED, ApprovalStatus.UNDER_REVIEW):
            raise ExceptionLifecycleError(
                f"Cannot approve exception in status {self.status.value}"
            )
        self.status = ApprovalStatus.EXECUTED       # using EXECUTED to mean "active, approved exception"
        self.approved_by = decided_by
        self.approved_at = datetime.now(timezone.utc)
        if expiry_date:
            self.expiry_date = expiry_date

    def close(self, reason: str) -> None:
        self.status = ApprovalStatus.REJECTED        # using REJECTED to mean "closed/cancelled"
        self.closed_at = datetime.now(timezone.utc)
        self.close_reason = reason


# ---- Factory functions for each exception type -----------------------------------

def raise_pricing_exception(
    position_id: str,
    desk_rate_bps: Decimal,
    market_rate_bps: Decimal,
    gap_bps: Decimal,
    raised_by: str = "pricing.pricing_intelligence",
) -> ExceptionRecord:
    severity = BreakSeverity.CRITICAL if abs(gap_bps) > 200 else (
        BreakSeverity.HIGH if abs(gap_bps) > 50 else BreakSeverity.MEDIUM
    )
    return ExceptionRecord(
        exception_id=str(uuid.uuid4()),
        exception_type="PRICING",
        raised_at=datetime.now(timezone.utc),
        raised_by=raised_by,
        description=(
            f"Position {position_id} priced at {float(desk_rate_bps):.1f}bps vs "
            f"market composite of {float(market_rate_bps):.1f}bps "
            f"(gap {float(gap_bps):+.1f}bps). Requires review or documentation "
            f"of business reason for off-market rate."
        ),
        severity=severity,
        target_type="POSITION",
        target_id=position_id,
        status=ApprovalStatus.PROPOSED,
        detail={
            "desk_rate_bps": float(desk_rate_bps),
            "market_rate_bps": float(market_rate_bps),
            "gap_bps": float(gap_bps),
        },
    )


def raise_limit_exception(
    counterparty_id: str,
    current_exposure_usd: Decimal,
    limit_usd: Decimal,
    overage_usd: Decimal,
    raised_by: str = "risk.limit_monitor",
) -> ExceptionRecord:
    return ExceptionRecord(
        exception_id=str(uuid.uuid4()),
        exception_type="LIMIT",
        raised_at=datetime.now(timezone.utc),
        raised_by=raised_by,
        description=(
            f"Counterparty {counterparty_id} exposure of ${current_exposure_usd:,.0f} "
            f"exceeds limit of ${limit_usd:,.0f} by ${overage_usd:,.0f} "
            f"({float(overage_usd/limit_usd):.1%}). "
            f"Requires credit risk approval before additional trading."
        ),
        severity=BreakSeverity.CRITICAL,
        target_type="COUNTERPARTY",
        target_id=counterparty_id,
        status=ApprovalStatus.PROPOSED,
        detail={
            "current_exposure_usd": float(current_exposure_usd),
            "limit_usd": float(limit_usd),
            "overage_usd": float(overage_usd),
        },
    )


def raise_data_quality_exception(
    target_id: str,
    target_type: str,
    field_errors: list,
    raised_by: str = "normalization.schema_mapping",
) -> ExceptionRecord:
    return ExceptionRecord(
        exception_id=str(uuid.uuid4()),
        exception_type="DATA_QUALITY",
        raised_at=datetime.now(timezone.utc),
        raised_by=raised_by,
        description=(
            f"Data quality issues detected on {target_type} {target_id}: "
            f"{'; '.join(field_errors)}. "
            f"Position is included in book but confidence scores are discounted."
        ),
        severity=BreakSeverity.MEDIUM,
        target_type=target_type,
        target_id=target_id,
        status=ApprovalStatus.PROPOSED,
        detail={"field_errors": field_errors},
    )


# ---- Exception management functions -----------------------------------------------

class ExceptionManager:
    """
    In-memory registry of open exceptions for a desk cycle. In production,
    backed by secfi.exception_log (postgres) — replace the in-memory dict
    with DB reads/writes here.
    """

    def __init__(self):
        self._records: dict = {}    # exception_id -> ExceptionRecord

    def add(self, record: ExceptionRecord) -> None:
        self._records[record.exception_id] = record

    def get(self, exception_id: str) -> Optional[ExceptionRecord]:
        return self._records.get(exception_id)

    def approve(self, exception_id: str, decided_by: str, expiry_date: Optional[date] = None) -> ExceptionRecord:
        record = self._records.get(exception_id)
        if record is None:
            raise ExceptionLifecycleError(f"Exception {exception_id} not found")
        record.approve(decided_by, expiry_date)
        return record

    def close(self, exception_id: str, reason: str) -> ExceptionRecord:
        record = self._records.get(exception_id)
        if record is None:
            raise ExceptionLifecycleError(f"Exception {exception_id} not found")
        record.close(reason)
        return record

    def active(self) -> list:
        return [r for r in self._records.values() if r.is_active()]

    def pending_approval(self) -> list:
        return [r for r in self._records.values()
                if r.status == ApprovalStatus.PROPOSED]

    def expired(self) -> list:
        return [r for r in self._records.values() if r.is_expired() and r.closed_at is None]

    def summary(self) -> dict:
        all_records = list(self._records.values())
        return {
            "total": len(all_records),
            "pending_approval": sum(1 for r in all_records if r.status == ApprovalStatus.PROPOSED),
            "active": sum(1 for r in all_records if r.is_active()),
            "expired_not_closed": sum(1 for r in all_records if r.is_expired() and r.closed_at is None),
            "by_type": {
                "PRICING": sum(1 for r in all_records if r.exception_type == "PRICING"),
                "LIMIT": sum(1 for r in all_records if r.exception_type == "LIMIT"),
                "DATA_QUALITY": sum(1 for r in all_records if r.exception_type == "DATA_QUALITY"),
            },
        }
