"""
Canonical domain model.

Design decision (stated explicitly per skill governance requirements):
We use stdlib `dataclasses` rather than pydantic for the internal domain
model. Rationale:
  1. These objects sit on the hot path of intraday recompute (optimization,
     risk, pricing run every few minutes on tens of thousands of rows);
     dataclasses + numpy/pandas vectorized operations avoid pydantic's
     per-field validation overhead at that volume.
  2. Validation belongs at the boundary (ingestion/normalization layer),
     not scattered through every internal transform. See
     `normalization/schema_mapping.py` for the validation gate that
     constructs these objects from raw vendor payloads.
  3. The API layer (api/) DOES use pydantic, because FastAPI request/response
     contracts benefit from automatic schema generation and that is a
     boundary, not a hot loop. See api/schemas.py.

All monetary amounts are in USD unless a `currency` field says otherwise.
All rates/fees are in basis points (bps) per annum unless stated.
All dates are timezone-aware UTC `datetime.date` / `datetime.datetime`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from secfi_platform.common.enums import (
    ActionUrgency,
    ApprovalStatus,
    BreakSeverity,
    BreakType,
    CollateralType,
    CorporateActionType,
    CounterpartyTier,
    CounterpartyType,
    DataQualityFlag,
    Direction,
    LegalEntity,
    ProductType,
    RecommendationAction,
    Region,
    SpecialnessTier,
)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Security:
    """Reference data for a single security. Keyed by internal_id; cross-keyed to vendor ids."""
    internal_id: str
    cusip: Optional[str]
    isin: Optional[str]
    sedol: Optional[str]
    ticker: str
    description: str
    product_type: ProductType
    currency: str
    country_of_risk: str
    issuer_id: str
    gics_sector: Optional[str] = None
    is_adr: bool = False
    adr_ratio: Optional[Decimal] = None   # ADR shares per ordinary share, when is_adr
    index_memberships: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Counterparty:
    """A trading counterparty (the legal entity the desk faces, not the underlying fund family)."""
    counterparty_id: str
    legal_name: str
    counterparty_type: CounterpartyType
    tier: CounterpartyTier
    region: Region
    booking_entities: tuple[LegalEntity, ...]
    lei: Optional[str] = None
    parent_group_id: Optional[str] = None      # for cross-fund netting/concentration rollups
    internal_credit_rating: Optional[str] = None
    pd_1y: Optional[float] = None              # probability of default, 1yr, from credit risk system
    lgd_assumption: Optional[float] = None     # loss given default assumption
    is_netting_eligible: bool = False
    onboarding_date: Optional[date] = None
    watch_list: bool = False


# ---------------------------------------------------------------------------
# Book / positions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CollateralLeg:
    collateral_type: CollateralType
    market_value: Decimal
    currency: str
    haircut_pct: Decimal           # e.g. Decimal("0.02") for 2%


@dataclass(frozen=True)
class Position:
    """
    One securities-finance position (a loan, borrow, repo, or reverse-repo line).
    This is the canonical unit the optimization, risk, and pricing engines operate on.
    """
    position_id: str
    trade_date: date
    value_date: date
    maturity_date: Optional[date]          # None => open/evergreen
    direction: Direction
    security: Security
    counterparty_id: str
    booking_entity: LegalEntity
    quantity: Decimal                      # shares or face amount
    market_value: Decimal                  # quantity * price, in position currency
    currency: str
    rate_bps: Decimal                      # fee (LEND/BORROW) or repo rate (REPO/REVERSE_REPO), bps p.a.
    rate_type_is_rebate: bool              # True if rate_bps represents a cash rebate rather than a fee
    collateral: tuple[CollateralLeg, ...]
    term_type: str                         # "OPEN" | "TERM"
    desk_id: str
    trader_id: str
    is_rehypothecable: bool = True
    last_recall_date: Optional[date] = None
    last_return_date: Optional[date] = None
    source_system: str = "INTERNAL_TRADE_CAPTURE"
    data_quality_flag: DataQualityFlag = DataQualityFlag.OK


# ---------------------------------------------------------------------------
# Market data / pricing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketRateQuote:
    """A market composite rate observation for a security, e.g. from DataLend/EquiLend."""
    security_internal_id: str
    as_of: datetime
    source: str                       # "EQUILEND" | "DATALEND" | "INTERNAL_EXECUTED"
    avg_fee_bps: Optional[Decimal]
    weighted_avg_fee_bps: Optional[Decimal]
    min_fee_bps: Optional[Decimal]
    max_fee_bps: Optional[Decimal]
    utilization_pct: Optional[Decimal]
    total_lendable_value: Optional[Decimal]
    total_on_loan_value: Optional[Decimal]
    sample_count: Optional[int] = None
    data_quality_flag: DataQualityFlag = DataQualityFlag.OK


@dataclass(frozen=True)
class FXRate:
    base_ccy: str
    quote_ccy: str
    rate: Decimal
    as_of: datetime
    source: str = "INTERNAL_MARKET_DATA"


@dataclass(frozen=True)
class YieldCurvePoint:
    curve_id: str             # e.g. "USD_OIS", "USD_SOFR", "EUR_ESTR"
    tenor_days: int
    rate_pct: Decimal
    as_of: date


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorporateActionEvent:
    event_id: str
    security_internal_id: str
    action_type: CorporateActionType
    announce_date: date
    record_date: Optional[date]
    ex_date: Optional[date]
    effective_date: Optional[date]
    payment_date: Optional[date]
    is_mandatory: bool
    election_deadline: Optional[date] = None
    terms_summary: str = ""
    source: str = "CORP_ACTIONS_FEED"
    data_quality_flag: DataQualityFlag = DataQualityFlag.OK


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReconciliationBreak:
    break_id: str
    as_of: date
    position_id: Optional[str]
    security_internal_id: Optional[str]
    counterparty_id: Optional[str]
    break_type: BreakType
    severity: BreakSeverity
    book_value: Optional[Decimal]
    external_value: Optional[Decimal]
    external_source: str            # "CUSTODIAN" | "FIRM_BALANCE_SHEET" | "SETTLEMENT_SYSTEM"
    delta: Optional[Decimal]
    probable_root_cause: str
    recommended_action: str
    buyin_risk_relevant: bool
    capital_misstatement_relevant: bool
    age_days: int = 0


# ---------------------------------------------------------------------------
# Explainable recommendations (shared output contract across ALL engines)
# ---------------------------------------------------------------------------

@dataclass
class Recommendation:
    """
    Every engine in this platform — optimization, pricing, recall, growth,
    hedge — emits objects of this shape. This is intentional: it gives the
    desk, risk, and audit a single, consistent contract to consume,
    log, and approve/reject, regardless of which engine produced it.

    Nothing with action != HOLD/DO_NOTHING auto-executes. `approval_status`
    starts at PROPOSED and a human (or downstream rules engine bound to
    explicit delegated authority) must transition it. See docs/governance.md.
    """
    recommendation_id: str
    generated_at: datetime
    source_engine: str
    action: RecommendationAction
    target_type: str                # "POSITION" | "COUNTERPARTY" | "SECURITY" | "BOOK_SLICE"
    target_id: str
    quantity: Optional[Decimal]
    from_value: Optional[Decimal]   # e.g. current rate, current balance
    to_value: Optional[Decimal]     # e.g. proposed rate, proposed balance
    estimated_pnl_impact_usd: Optional[Decimal]
    estimated_capital_impact_usd: Optional[Decimal]
    estimated_rwa_impact_usd: Optional[Decimal]
    rationale: list[str]
    supporting_metrics: dict
    confidence: float                # 0.0 - 1.0, see explainability/explain.py
    data_completeness_pct: float     # 0.0 - 1.0
    priority_score: float            # engine-specific 0-100 ranking score
    approval_status: ApprovalStatus = ApprovalStatus.PROPOSED
    expires_at: Optional[datetime] = None


@dataclass
class Alert:
    alert_id: str
    raised_at: datetime
    severity: BreakSeverity
    category: str
    title: str
    detail: str
    related_entity_type: str
    related_entity_id: str
    requires_acknowledgement: bool = True
