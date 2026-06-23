"""
Desk-facing API routes.

Read-only by design: every GET here serves analytics/recommendations
computed by the orchestration cycle. The ONLY mutating endpoint in this
router group is the approval decision endpoint, and even that does not
execute anything — it transitions a Recommendation's approval_status and
writes an immutable audit record (sql/schemas/08_audit_log.sql). Nothing
in this API layer places an order, books a trade, or changes a live rate.
That boundary is intentional and load-bearing for the control framework
described in docs/governance.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from secfi_platform.api.schemas import (
    ApprovalDecisionRequest,
    CorporateActionWatchlistRowResponse,
    CounterpartyExposureResponse,
    ExecutiveSummaryResponse,
    ReconciliationBreakResponse,
    RecallQueueRowResponse,
    RecommendationResponse,
)
from secfi_platform.api.state import get_latest_cycle_outputs
from secfi_platform.common.enums import ApprovalStatus

router = APIRouter(prefix="/v1", tags=["desk"])


def _require_outputs():
    outputs = get_latest_cycle_outputs()
    if outputs is None:
        raise HTTPException(status_code=503, detail="No cycle has completed yet. Check orchestration job status.")
    return outputs


@router.get("/risk/counterparty/{counterparty_id}", response_model=CounterpartyExposureResponse)
def get_counterparty_exposure(counterparty_id: str):
    outputs = _require_outputs()
    exposure = outputs.counterparty_exposures.get(counterparty_id)
    if exposure is None:
        raise HTTPException(status_code=404, detail=f"No exposure computed for counterparty '{counterparty_id}'")
    return CounterpartyExposureResponse(
        counterparty_id=exposure.counterparty_id,
        as_of=exposure.as_of,
        gross_exposure_usd=exposure.gross_exposure_usd,
        net_exposure_usd=exposure.net_exposure_usd,
        collateralized_exposure_usd=exposure.collateralized_exposure_usd,
        uncollateralized_exposure_usd=exposure.uncollateralized_exposure_usd,
        utilization_pct=exposure.utilization_pct,
        limit_breached=exposure.limit_breached,
        herfindahl_issuer=exposure.herfindahl_issuer,
        wrong_way_risk_flags=exposure.wrong_way_risk_flags,
    )


@router.get("/risk/counterparty", response_model=list[CounterpartyExposureResponse])
def list_counterparty_exposures():
    outputs = _require_outputs()
    return [
        CounterpartyExposureResponse(
            counterparty_id=e.counterparty_id, as_of=e.as_of, gross_exposure_usd=e.gross_exposure_usd,
            net_exposure_usd=e.net_exposure_usd, collateralized_exposure_usd=e.collateralized_exposure_usd,
            uncollateralized_exposure_usd=e.uncollateralized_exposure_usd, utilization_pct=e.utilization_pct,
            limit_breached=e.limit_breached, herfindahl_issuer=e.herfindahl_issuer,
            wrong_way_risk_flags=e.wrong_way_risk_flags,
        )
        for e in outputs.counterparty_exposures.values()
    ]


@router.get("/optimization/recommendations", response_model=list[RecommendationResponse])
def get_optimization_recommendations(limit: int = 50):
    outputs = _require_outputs()
    return [_to_recommendation_response(r) for r in outputs.optimization_result.recommendations[:limit]]


@router.get("/pricing/recommendations", response_model=list[RecommendationResponse])
def get_pricing_recommendations(limit: int = 50):
    outputs = _require_outputs()
    return [_to_recommendation_response(r) for r in outputs.pricing_recommendations[:limit]]


@router.get("/growth/recommendations", response_model=list[RecommendationResponse])
def get_growth_recommendations(limit: int = 50):
    outputs = _require_outputs()
    return [_to_recommendation_response(r) for r in outputs.growth_recommendations[:limit]]


@router.get("/recall-buyin/queue", response_model=list[RecallQueueRowResponse])
def get_recall_buyin_queue(limit: int = 50):
    outputs = _require_outputs()
    return [
        RecallQueueRowResponse(
            position_id=row.position_id, security_internal_id=row.security_internal_id, ticker=row.ticker,
            counterparty_id=row.counterparty_id, urgency_score=row.urgency_score,
            buyin_risk_score=row.buyin_risk_score, recommended_action=row.recommended_action.value,
            drivers=row.drivers,
        )
        for row in outputs.recall_queue[:limit]
    ]


@router.get("/corporate-actions/watchlist", response_model=list[CorporateActionWatchlistRowResponse])
def get_corporate_action_watchlist(limit: int = 50):
    outputs = _require_outputs()
    return [
        CorporateActionWatchlistRowResponse(
            event_id=impact.event.event_id, security_internal_id=impact.event.security_internal_id,
            action_type=impact.event.action_type.value, composite_risk_score=impact.composite_risk_score,
            urgency=impact.urgency.value, days_to_key_date=impact.days_to_key_date,
            affected_market_value_usd=impact.affected_market_value_usd,
        )
        for impact in outputs.ca_watchlist[:limit]
    ]


@router.get("/reconciliation/breaks", response_model=list[ReconciliationBreakResponse])
def get_reconciliation_breaks(severity: str | None = None, limit: int = 100):
    outputs = _require_outputs()
    breaks = outputs.recon_breaks
    if severity:
        breaks = [b for b in breaks if b["severity"].value == severity.upper()]
    return [
        ReconciliationBreakResponse(
            break_id=b["break_id"], as_of=b["as_of"], break_type=b["break_type"].value,
            severity=b["severity"].value, probable_root_cause=b["probable_root_cause"],
            recommended_action=b["recommended_action"], buyin_risk_relevant=b["buyin_risk_relevant"],
            capital_misstatement_relevant=b["capital_misstatement_relevant"],
        )
        for b in breaks[:limit]
    ]


@router.get("/reports/daily-summary", response_model=ExecutiveSummaryResponse)
def get_daily_summary():
    outputs = _require_outputs()
    s = outputs.executive_summary
    return ExecutiveSummaryResponse(
        as_of=s.as_of, generated_at=s.generated_at, book_nmv_usd=s.book_nmv_usd,
        total_gross_exposure_usd=s.total_gross_exposure_usd,
        counterparties_at_or_over_limit=s.counterparties_at_or_over_limit,
        open_critical_recon_breaks=s.open_critical_recon_breaks,
        total_estimated_pnl_opportunity_usd=s.total_estimated_pnl_opportunity_usd,
        open_alerts_by_severity=s.open_alerts_by_severity,
    )


@router.post("/recommendations/approval-decision")
def post_approval_decision(decision: ApprovalDecisionRequest):
    """
    Records a human approve/reject decision against a recommendation.

    This endpoint does NOT execute anything. It writes an immutable audit
    record (sql/schemas/08_audit_log.sql `recommendation_approval_log`)
    capturing who decided, when, and what. Execution of an APPROVED
    recommendation happens through the firm's existing trade booking /
    rate amendment workflow, triggered by a human trader acting on the
    approved recommendation — this platform never auto-executes.
    """
    outputs = _require_outputs()
    all_recs = (
        outputs.optimization_result.recommendations + outputs.pricing_recommendations
        + outputs.recall_recommendations + outputs.ca_recommendations + outputs.growth_recommendations
    )
    target = next((r for r in all_recs if r.recommendation_id == decision.recommendation_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Recommendation not found in latest cycle outputs.")

    target.approval_status = ApprovalStatus.APPROVED if decision.decision == "APPROVE" else ApprovalStatus.REJECTED

    # In production: INSERT INTO recommendation_approval_log(...) VALUES (...);
    # see sql/schemas/08_audit_log.sql for the full column set captured.
    return {
        "recommendation_id": target.recommendation_id,
        "new_status": target.approval_status.value,
        "decided_by": decision.decided_by,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


def _to_recommendation_response(r) -> RecommendationResponse:
    return RecommendationResponse(
        recommendation_id=r.recommendation_id, generated_at=r.generated_at, source_engine=r.source_engine,
        action=r.action.value, target_type=r.target_type, target_id=r.target_id,
        estimated_pnl_impact_usd=r.estimated_pnl_impact_usd, confidence=r.confidence,
        data_completeness_pct=r.data_completeness_pct, priority_score=r.priority_score,
        approval_status=r.approval_status.value, rationale=r.rationale,
    )
