"""
Backtesting and realized-P&L validation framework.

Answers the model-risk question: "are our recommendations actually
improving P&L when they are acted on?" This module compares
`estimated_pnl_impact_usd` (predicted at recommendation generation time)
against `realized_pnl_usd` (computed after the position is repriced or
reallocated), producing calibration statistics by engine, action type,
and confidence bucket.

Assumption BT-1: "realized P&L" for a repricing recommendation is
computed as (new_rate_bps - old_rate_bps) / 10000 * market_value,
annualized, measured at the next mark date after execution. This is a
simplified proxy; a production system would use the actual executed
economics from the firm's P&L attribution system, crossed against the
specific trade reference captured in the recommendation's
`supporting_metrics`. See docs/model_risk.md "Backtesting plan."

Assumption BT-2: This module cannot run until Phase 2 of the
implementation plan (execution data exists). In Phase 1 (parallel-run),
it runs with synthetic realized P&L generated from small random
perturbations for calibration framework validation only — clearly labeled
as synthetic in all outputs.

Usage:
    backtester = Backtester(recommendation_log, execution_log)
    report = backtester.run()
    print(report.calibration_summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
import statistics


@dataclass(frozen=True)
class ExecutedRecommendation:
    """
    An approved-and-executed recommendation paired with its realized outcome.
    Created when an execution event is linked back to the originating
    recommendation via recommendation_id.
    """
    recommendation_id: str
    source_engine: str
    action: str
    target_id: str
    executed_at: datetime
    estimated_pnl_usd: Decimal
    realized_pnl_usd: Decimal                # measured at mark date following execution
    confidence_at_generation: float
    data_completeness_at_generation: float
    is_synthetic: bool = False                # True during Phase 1 parallel-run validation


@dataclass
class EngineCalibrationStats:
    source_engine: str
    sample_count: int
    mean_prediction_error_usd: float           # mean(realized - estimated)
    mae_usd: float                              # mean absolute error
    rmse_usd: float                             # root mean squared error
    prediction_bias: str                        # "OVER" | "UNDER" | "NEUTRAL"
    mean_signed_error_pct: float                # mean((realized - estimated) / abs(estimated))
    correlation_predicted_realized: float       # how linearly predictive estimates are
    confidence_calibration_notes: str


@dataclass
class BacktestReport:
    generated_at: datetime
    total_recommendations_evaluated: int
    total_executed: int
    synthetic_count: int
    total_estimated_pnl_usd: Decimal
    total_realized_pnl_usd: Decimal
    overall_bias_pct: float                    # (realized - estimated) / abs(estimated)
    engine_calibration: list                    # list[EngineCalibrationStats]
    calibration_by_confidence_bucket: list      # list[dict] — stats per confidence quartile
    worst_predictions: list                     # list[ExecutedRecommendation] — top 5 biggest misses
    best_predictions: list                      # list[ExecutedRecommendation] — top 5 most accurate
    overall_model_quality_score: float          # 0-100

    def calibration_summary(self) -> str:
        lines = [
            f"Backtest Report — {self.generated_at.date().isoformat()}",
            f"Recommendations evaluated: {self.total_recommendations_evaluated}",
            f"  of which executed: {self.total_executed}",
            f"  of which SYNTHETIC (Phase 1 parallel-run only): {self.synthetic_count}",
            f"Total estimated P&L: ${float(self.total_estimated_pnl_usd):,.0f}",
            f"Total realized P&L:  ${float(self.total_realized_pnl_usd):,.0f}",
            f"Overall bias: {self.overall_bias_pct:+.1%} "
            f"({'over-estimated' if self.overall_bias_pct > 0 else 'under-estimated'})",
            f"Model quality score: {self.overall_model_quality_score:.0f}/100",
            "",
            "Calibration by engine:",
        ]
        for stat in self.engine_calibration:
            lines.append(
                f"  {stat.source_engine}: MAE=${stat.mae_usd:,.0f}, "
                f"bias={stat.mean_signed_error_pct:+.1%} ({stat.prediction_bias}), "
                f"n={stat.sample_count}"
            )
        return "\n".join(lines)


def _engine_calibration(records: list) -> EngineCalibrationStats:
    if not records:
        return EngineCalibrationStats(
            source_engine="N/A", sample_count=0, mean_prediction_error_usd=0.0,
            mae_usd=0.0, rmse_usd=0.0, prediction_bias="NEUTRAL",
            mean_signed_error_pct=0.0, correlation_predicted_realized=0.0,
            confidence_calibration_notes="Insufficient data.",
        )
    engine = records[0].source_engine
    errors = [float(r.realized_pnl_usd - r.estimated_pnl_usd) for r in records]
    abs_errors = [abs(e) for e in errors]
    signed_pcts = [
        (float(r.realized_pnl_usd - r.estimated_pnl_usd) / abs(float(r.estimated_pnl_usd)))
        for r in records
        if r.estimated_pnl_usd != 0
    ]
    mae = statistics.mean(abs_errors)
    rmse = (statistics.mean([e**2 for e in errors]) ** 0.5)
    mean_err = statistics.mean(errors)
    mean_signed = statistics.mean(signed_pcts) if signed_pcts else 0.0
    bias = "OVER" if mean_signed > 0.05 else ("UNDER" if mean_signed < -0.05 else "NEUTRAL")

    # Pearson correlation between predicted and realized
    preds = [float(r.estimated_pnl_usd) for r in records]
    reals = [float(r.realized_pnl_usd) for r in records]
    corr = 0.0
    if len(preds) >= 2 and statistics.stdev(preds) > 0 and statistics.stdev(reals) > 0:
        n = len(preds)
        mean_p, mean_r = statistics.mean(preds), statistics.mean(reals)
        cov = sum((preds[i] - mean_p) * (reals[i] - mean_r) for i in range(n)) / n
        corr = cov / (statistics.stdev(preds) * statistics.stdev(reals))

    notes = []
    if bias == "OVER":
        notes.append("Recommendations systematically over-estimate P&L impact — review whether market-rate assumptions are too optimistic.")
    elif bias == "UNDER":
        notes.append("Recommendations systematically under-estimate P&L — consider whether additional revenue sources are not being captured in the model.")
    if corr < 0.5:
        notes.append("Low predicted-realized correlation suggests confidence scores need recalibration — see docs/model_risk.md.")

    return EngineCalibrationStats(
        source_engine=engine, sample_count=len(records),
        mean_prediction_error_usd=mean_err, mae_usd=mae, rmse_usd=rmse,
        prediction_bias=bias, mean_signed_error_pct=mean_signed,
        correlation_predicted_realized=corr,
        confidence_calibration_notes=" ".join(notes) if notes else "Calibration looks reasonable.",
    )


def _calibration_by_confidence(records: list) -> list:
    buckets = {"LOW (0-0.25)": [], "MED (0.25-0.5)": [], "GOOD (0.5-0.75)": [], "HIGH (0.75-1.0)": []}
    for r in records:
        c = r.confidence_at_generation
        if c < 0.25:
            buckets["LOW (0-0.25)"].append(r)
        elif c < 0.50:
            buckets["MED (0.25-0.5)"].append(r)
        elif c < 0.75:
            buckets["GOOD (0.5-0.75)"].append(r)
        else:
            buckets["HIGH (0.75-1.0)"].append(r)
    result = []
    for label, recs in buckets.items():
        if not recs:
            continue
        errors = [abs(float(r.realized_pnl_usd - r.estimated_pnl_usd)) for r in recs]
        result.append({
            "confidence_bucket": label,
            "count": len(recs),
            "mean_mae_usd": statistics.mean(errors),
            "note": (
                "Higher-confidence recommendations should show lower MAE — "
                "verify this relationship holds in your actual execution data."
            ),
        })
    return result


class Backtester:
    def __init__(self, executed_recommendations: list):
        """
        `executed_recommendations` is a list[ExecutedRecommendation] — pairs
        of recommendations with their realized outcomes. In production, this
        is built by joining secfi.recommendation against the firm's P&L
        attribution system on recommendation_id (stored in recommendation
        detail/supporting_metrics). See docs/model_risk.md "Backtesting plan."
        """
        self.records = executed_recommendations

    def run(self) -> BacktestReport:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        records = self.records
        if not records:
            return BacktestReport(
                generated_at=now, total_recommendations_evaluated=0,
                total_executed=0, synthetic_count=0,
                total_estimated_pnl_usd=Decimal("0"),
                total_realized_pnl_usd=Decimal("0"),
                overall_bias_pct=0.0, engine_calibration=[], calibration_by_confidence_bucket=[],
                worst_predictions=[], best_predictions=[], overall_model_quality_score=0.0,
            )

        by_engine: dict = {}
        for r in records:
            by_engine.setdefault(r.source_engine, []).append(r)

        engine_stats = [_engine_calibration(recs) for recs in by_engine.values()]
        conf_buckets = _calibration_by_confidence(records)

        total_est = sum((r.estimated_pnl_usd for r in records), Decimal("0"))
        total_real = sum((r.realized_pnl_usd for r in records), Decimal("0"))
        bias = float((total_real - total_est) / abs(total_est)) if total_est != 0 else 0.0

        abs_errors = [abs(float(r.realized_pnl_usd - r.estimated_pnl_usd)) for r in records]
        sorted_by_error = sorted(records, key=lambda r: abs(float(r.realized_pnl_usd - r.estimated_pnl_usd)), reverse=True)
        worst = sorted_by_error[:5]
        best = sorted_by_error[-5:] if len(sorted_by_error) >= 5 else sorted_by_error

        # Quality score: 100 = perfect, penalized for bias, MAE, and low correlation
        avg_corr = statistics.mean([s.correlation_predicted_realized for s in engine_stats]) if engine_stats else 0.0
        bias_penalty = min(abs(bias) * 50, 30)
        avg_mae_pct = statistics.mean([s.mean_signed_error_pct for s in engine_stats]) if engine_stats else 0
        mae_penalty = min(abs(avg_mae_pct) * 40, 40)
        corr_score = max(avg_corr * 30, 0)
        quality = max(min(100 - bias_penalty - mae_penalty + corr_score, 100), 0)

        return BacktestReport(
            generated_at=now,
            total_recommendations_evaluated=len(records),
            total_executed=len(records),
            synthetic_count=sum(1 for r in records if r.is_synthetic),
            total_estimated_pnl_usd=total_est,
            total_realized_pnl_usd=total_real,
            overall_bias_pct=bias,
            engine_calibration=engine_stats,
            calibration_by_confidence_bucket=conf_buckets,
            worst_predictions=worst,
            best_predictions=best,
            overall_model_quality_score=quality,
        )


def generate_synthetic_executed_recommendations(
    recommendations: list,
    noise_pct: float = 0.25,
    seed: int = 42,
) -> list:
    """
    Generate synthetic ExecutedRecommendation objects by perturbing
    estimated P&L with configurable noise. Used ONLY for Phase 1
    framework validation — clearly flags is_synthetic=True so no one
    mistakes synthetic data for real backtesting results.
    """
    import random
    rng = random.Random(seed)
    from datetime import timezone
    now = datetime.now(timezone.utc)
    result = []
    for rec in recommendations:
        if rec.estimated_pnl_impact_usd is None:
            continue
        noise = 1.0 + rng.uniform(-noise_pct, noise_pct)
        realized = rec.estimated_pnl_impact_usd * Decimal(str(round(noise, 4)))
        result.append(ExecutedRecommendation(
            recommendation_id=rec.recommendation_id,
            source_engine=rec.source_engine,
            action=rec.action.value,
            target_id=rec.target_id,
            executed_at=now,
            estimated_pnl_usd=rec.estimated_pnl_impact_usd,
            realized_pnl_usd=realized,
            confidence_at_generation=rec.confidence,
            data_completeness_at_generation=rec.data_completeness_pct,
            is_synthetic=True,
        ))
    return result
