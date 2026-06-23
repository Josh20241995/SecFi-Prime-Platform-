"""
Extended API routes for new risk capability modules.

Provides read-only access to:
  - Collateral optimization recommendations
  - Inventory snapshots and locate resolution
  - Intraday limit utilization dashboard and what-if simulation
  - Scenario analysis results
  - Exception management (read + approve/close)
  - Data quality reports
  - Backtesting reports (synthetic or realized)

Registered in api/main.py alongside the existing desk router.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException

from secfi_platform.api.state import get_latest_cycle_outputs

router = APIRouter(prefix="/v1", tags=["risk-extended"])


def _require_outputs():
    o = get_latest_cycle_outputs()
    if o is None:
        raise HTTPException(status_code=503, detail="No cycle completed yet.")
    return o


# ---- Limits -----------------------------------------------------------------------

@router.get("/limits/dashboard")
def get_limits_dashboard(status: Optional[str] = None):
    """
    Intraday limit utilization dashboard. `status` filters to a specific
    tier: GREEN | AMBER | RED | BREACH.
    """
    from secfi_platform.risk.limit_monitor import compute_limit_utilization_dashboard
    outputs = _require_outputs()
    positions = outputs.executive_summary  # use actual positions from state in prod
    # In this reference build, re-compute from cycle outputs' counterparty exposures
    rows = [
        {
            "counterparty_id": exp.counterparty_id,
            "gross_exposure_usd": str(exp.gross_exposure_usd),
            "limit_usd": str(exp.limit_usd) if exp.limit_usd else None,
            "utilization_pct": exp.utilization_pct,
            "limit_breached": exp.limit_breached,
            "headroom_usd": str(exp.headroom_usd) if exp.headroom_usd else None,
        }
        for exp in outputs.counterparty_exposures.values()
        if status is None or (
            (status == "BREACH" and exp.limit_breached) or
            (status == "AMBER" and exp.utilization_pct and 0.85 <= exp.utilization_pct < 1.0) or
            (status == "GREEN" and exp.utilization_pct and exp.utilization_pct < 0.85)
        )
    ]
    rows.sort(key=lambda r: (r.get("utilization_pct") or 0), reverse=True)
    return {"count": len(rows), "results": rows}


@router.get("/limits/simulate-incremental")
def simulate_incremental_exposure(counterparty_id: str, incremental_usd: float):
    """
    What-if: how does adding `incremental_usd` of exposure to a
    counterparty change its limit utilization?
    """
    outputs = _require_outputs()
    exp = outputs.counterparty_exposures.get(counterparty_id)
    if exp is None:
        raise HTTPException(status_code=404, detail=f"Counterparty '{counterparty_id}' not found.")
    inc = Decimal(str(incremental_usd))
    current = exp.gross_exposure_usd
    new_gross = current + inc
    limit = exp.limit_usd
    new_util = float(new_gross / limit) if limit and limit > 0 else None
    would_breach = new_util is not None and new_util >= 1.0
    return {
        "counterparty_id": counterparty_id,
        "current_gross_usd": str(current),
        "incremental_usd": str(inc),
        "new_gross_usd": str(new_gross),
        "limit_usd": str(limit) if limit else None,
        "new_utilization_pct": new_util,
        "would_breach": would_breach,
        "recommendation": "DECLINE" if would_breach else "PROCEED",
    }


# ---- Scenarios --------------------------------------------------------------------

@router.get("/scenarios/standard")
def get_standard_scenario_results():
    """
    Run all standard scenarios against the current book and return
    a comparison matrix ranked worst-to-best by revenue impact.
    """
    outputs = _require_outputs()
    from secfi_platform.risk.scenario_engine import run_all_standard_scenarios, scenario_comparison_matrix
    results = run_all_standard_scenarios(outputs.positions, {})
    matrix = scenario_comparison_matrix(results)
    return {"scenario_count": len(matrix), "scenarios": matrix}


# ---- Data quality -----------------------------------------------------------------

@router.get("/data-quality/report")
def get_data_quality_report():
    """Current data quality snapshot for all ingested sources."""
    outputs = _require_outputs()
    # In production: return the DataQualityReport persisted per cycle
    # Reference build: return a simplified view from what's in cycle outputs
    alerts_dq = [
        a for a in outputs.alerts if a.category == "DATA_FRESHNESS"
    ]
    return {
        "cycle_as_of": str(outputs.executive_summary.as_of),
        "data_quality_notes": outputs.executive_summary.data_quality_notes,
        "data_freshness_alerts": len(alerts_dq),
        "alerts": [
            {"title": a.title, "severity": a.severity.value, "detail": a.detail}
            for a in alerts_dq
        ],
    }


# ---- Alert feed -------------------------------------------------------------------

@router.get("/alerts/feed")
def get_alert_feed(severity: Optional[str] = None, limit: int = 50):
    """
    Prioritized alert feed. Optionally filter by severity.
    Unlike GET /v1/reconciliation/breaks which is specific to recon,
    this returns ALL alert types in priority order.
    """
    from secfi_platform.alerting.prioritizer import prioritize_alerts, alert_feed_summary, reset_dedup_registry
    outputs = _require_outputs()
    # Reset dedup for API calls so every GET returns fresh prioritization
    reset_dedup_registry()
    prioritized = prioritize_alerts(outputs.alerts)
    active = [p for p in prioritized if not p.suppressed]
    if severity:
        active = [p for p in active if p.alert.severity.value == severity.upper()]
    return {
        "summary": alert_feed_summary(prioritized),
        "alerts": [
            {
                "rank": p.priority_rank,
                "alert_id": p.alert.alert_id,
                "title": p.alert.title,
                "severity": p.alert.severity.value,
                "category": p.alert.category,
                "detail": p.alert.detail,
                "routing_targets": p.routing_targets,
                "estimated_pnl_at_risk_usd": p.estimated_pnl_at_risk_usd,
                "raised_at": p.alert.raised_at.isoformat(),
            }
            for p in active[:limit]
        ],
    }


# ---- Capital usage ----------------------------------------------------------------

@router.get("/capital/usage")
def get_capital_usage(counterparty_id: Optional[str] = None):
    """Capital/RWA usage summary by counterparty."""
    outputs = _require_outputs()
    summaries = outputs.capital_summaries
    if counterparty_id:
        summary = summaries.get(counterparty_id)
        if not summary:
            raise HTTPException(404, detail=f"No capital summary for '{counterparty_id}'")
        return {
            "counterparty_id": summary.counterparty_id,
            "total_ead_usd": str(summary.total_ead_usd),
            "total_rwa_usd": str(summary.total_rwa_usd),
            "total_leverage_exposure_usd": str(summary.total_leverage_exposure_usd),
            "total_annualized_revenue_usd": str(summary.total_annualized_revenue_usd),
            "total_capital_cost_usd": str(summary.total_capital_cost_usd),
            "return_on_balance_sheet": summary.blended_return_on_balance_sheet,
            "return_on_capital": summary.blended_return_on_capital,
            "netting_benefit_applied": summary.netting_benefit_applied,
        }
    return {
        "counterparties": [
            {
                "counterparty_id": s.counterparty_id,
                "total_rwa_usd": str(s.total_rwa_usd),
                "total_leverage_exposure_usd": str(s.total_leverage_exposure_usd),
                "return_on_capital": s.blended_return_on_capital,
                "return_on_balance_sheet": s.blended_return_on_balance_sheet,
            }
            for s in summaries.values()
        ],
        "total_book_rwa_usd": str(sum(s.total_rwa_usd for s in summaries.values())),
        "total_book_leverage_exposure_usd": str(
            sum(s.total_leverage_exposure_usd for s in summaries.values())
        ),
    }


# ---- Rates/FX -----------------------------------------------------------------------

@router.get("/risk/rates-fx")
def get_rates_fx_report():
    """Current interest rate (DV01) and FX exposure, plus hedge recommendations."""
    outputs = _require_outputs()
    report = outputs.rates_fx_report
    return {
        "as_of": report.as_of,
        "total_dv01_usd": str(report.total_dv01_usd),
        "dv01_by_bucket": [
            {
                "currency": b.currency,
                "tenor_bucket": b.tenor_bucket,
                "net_notional_usd": str(b.net_notional_usd),
                "dv01_usd": str(b.dv01_usd),
            }
            for b in report.dv01_by_bucket
        ],
        "fx_exposures": [
            {
                "currency": f.currency,
                "net_exposure_usd": str(f.net_exposure_usd),
                "net_exposure_local": str(f.net_exposure_local),
            }
            for f in report.fx_exposures
        ],
        "funding_gap_summary": {
            k: {kk: str(vv) for kk, vv in v.items()}
            for k, v in report.funding_gap_summary.items()
        },
        "hedge_recommendations": report.hedge_recommendations,
    }
