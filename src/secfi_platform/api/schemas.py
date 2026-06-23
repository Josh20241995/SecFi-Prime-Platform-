"""
API boundary schemas.

These are pydantic models used ONLY at the FastAPI request/response
boundary. They are intentionally separate from the internal dataclass
domain model in common/types.py (see that module's docstring for the
rationale). Converting between the two is the job of `api/serializers.py`.

NOTE ON RUNTIME DEPENDENCY: this module requires `pydantic` and the API
layer requires `fastapi`/`uvicorn`. They are declared in pyproject.toml.
This reference build's sandbox does not have outbound network access to
install them, so this module is syntax-validated (`python -m py_compile`)
but not import-or-execution-tested here; see docs/testing_validation.md
"Known Gaps" for what CI must run on a connected runner before merge.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class CounterpartyExposureResponse(BaseModel):
    counterparty_id: str
    as_of: str
    gross_exposure_usd: Decimal
    net_exposure_usd: Decimal
    collateralized_exposure_usd: Decimal
    uncollateralized_exposure_usd: Decimal
    utilization_pct: Optional[float]
    limit_breached: bool
    herfindahl_issuer: float
    wrong_way_risk_flags: list[str]


class RecommendationResponse(BaseModel):
    recommendation_id: str
    generated_at: datetime
    source_engine: str
    action: str
    target_type: str
    target_id: str
    estimated_pnl_impact_usd: Optional[Decimal]
    confidence: float
    data_completeness_pct: float
    priority_score: float
    approval_status: str
    rationale: list[str]


class ApprovalDecisionRequest(BaseModel):
    recommendation_id: str
    decision: str = Field(..., pattern="^(APPROVE|REJECT)$")
    decided_by: str
    comment: Optional[str] = None


class RecallQueueRowResponse(BaseModel):
    position_id: str
    security_internal_id: str
    ticker: str
    counterparty_id: str
    urgency_score: float
    buyin_risk_score: float
    recommended_action: str
    drivers: list[str]


class CorporateActionWatchlistRowResponse(BaseModel):
    event_id: str
    security_internal_id: str
    action_type: str
    composite_risk_score: float
    urgency: str
    days_to_key_date: Optional[int]
    affected_market_value_usd: Decimal


class ReconciliationBreakResponse(BaseModel):
    break_id: str
    as_of: date
    break_type: str
    severity: str
    probable_root_cause: str
    recommended_action: str
    buyin_risk_relevant: bool
    capital_misstatement_relevant: bool


class ExecutiveSummaryResponse(BaseModel):
    as_of: date
    generated_at: datetime
    book_nmv_usd: Decimal
    total_gross_exposure_usd: Decimal
    counterparties_at_or_over_limit: int
    open_critical_recon_breaks: int
    total_estimated_pnl_opportunity_usd: Decimal
    open_alerts_by_severity: dict[str, int]


class HealthCheckResponse(BaseModel):
    status: str
    environment: str
    config_sources: list[str]
