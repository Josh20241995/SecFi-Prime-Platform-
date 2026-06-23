"""
Explainability and confidence scoring framework.

Every recommendation-producing engine (optimization, pricing, recall/buy-in,
growth, hedge) must call into this module rather than inventing its own
ad-hoc confidence number. That keeps confidence scores comparable across
engines, which matters because the daily executive summary and the
unified recommendation queue (see reporting/daily_summary.py) rank items
from DIFFERENT engines side by side.

Confidence model
-----------------
confidence = data_completeness_score * model_certainty_score * staleness_penalty

  data_completeness_score : fraction of required inputs that were present
                             and passed validation (vs. fell back to a
                             default/proxy).
  model_certainty_score   : engine-specific measure of how decisive the
                             underlying signal is (e.g., z-score magnitude
                             for pricing dispersion, distance from a limit
                             for risk-based recommendations). Passed in by
                             the caller, expected in [0, 1].
  staleness_penalty       : multiplicative penalty for input data age,
                             configured per data source in configs/base.yaml
                             under `data_quality.staleness_penalty_curve`.

This is a deliberately simple, auditable model (not a black-box ML
ensemble) because every number here has to be defensible in front of risk
and model governance committees. See docs/model_risk.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class DataCompletenessReport:
    required_fields: int
    present_and_valid_fields: int
    fallback_fields: int

    @property
    def completeness_pct(self) -> float:
        if self.required_fields == 0:
            return 1.0
        return self.present_and_valid_fields / self.required_fields


def staleness_penalty(age_minutes: float, half_life_minutes: float = 240.0) -> float:
    """
    Exponential decay penalty. At age = half_life_minutes, penalty = 0.5.
    Floored at 0.1 so a stale-but-present data point still contributes some
    information rather than zeroing out the recommendation entirely
    (zeroing it out would just suppress the alert the desk most needs to
    see — e.g., recall urgency on a name with a delayed custodian feed).
    """
    if age_minutes <= 0:
        return 1.0
    decay = 0.5 ** (age_minutes / half_life_minutes)
    return max(decay, 0.10)


def compute_confidence(
    completeness: DataCompletenessReport,
    model_certainty: float,
    data_age_minutes: float,
    half_life_minutes: float = 240.0,
) -> float:
    model_certainty = min(max(model_certainty, 0.0), 1.0)
    penalty = staleness_penalty(data_age_minutes, half_life_minutes)
    score = completeness.completeness_pct * model_certainty * penalty
    return round(min(max(score, 0.0), 1.0), 4)


def build_rationale(*clauses: str) -> list[str]:
    """Normalize rationale clauses: strip, drop empties, dedupe preserving order."""
    seen = set()
    out = []
    for c in clauses:
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


@dataclass(frozen=True)
class PriorityWeights:
    pnl_weight: float = 0.35
    risk_weight: float = 0.30
    urgency_weight: float = 0.25
    confidence_weight: float = 0.10


def compute_priority_score(
    *,
    pnl_score_0_100: float,
    risk_score_0_100: float,
    urgency_score_0_100: float,
    confidence_0_1: float,
    weights: PriorityWeights = PriorityWeights(),
) -> float:
    """
    A single 0-100 score used to rank items within and across queues
    (recall queue, pricing queue, growth queue, etc.). Confidence enters
    multiplicatively at the end so a high-impact but low-confidence
    recommendation is demoted rather than competing equally with a
    well-evidenced one — without being hidden, since it still surfaces in
    its queue, just lower.
    """
    base = (
        weights.pnl_weight * pnl_score_0_100
        + weights.risk_weight * risk_score_0_100
        + weights.urgency_weight * urgency_score_0_100
    )
    confidence_component = weights.confidence_weight * (confidence_0_1 * 100)
    raw = base + confidence_component
    # Confidence also damps the non-confidence portion so a 0-confidence
    # item cannot rank highly purely on assumed P&L/risk/urgency.
    damped = raw * (0.5 + 0.5 * confidence_0_1)
    return round(min(max(damped, 0.0), 100.0), 2)


def rank_recommendations(items: Sequence, key=lambda r: r.priority_score) -> list:
    return sorted(items, key=key, reverse=True)
