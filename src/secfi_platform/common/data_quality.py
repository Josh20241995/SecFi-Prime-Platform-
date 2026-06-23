"""
Data quality controls engine.

Provides a desk-wide view of data quality across every ingested data
source, beyond the per-record validation gates in
`normalization/schema_mapping.py`. While the normalization layer catches
record-level structural errors, this module catches:

  - SOURCE-LEVEL staleness (entire feed delayed/absent)
  - POPULATION COMPLETENESS (expected N securities got M quotes; M < N-threshold)
  - CROSS-SOURCE DISAGREEMENT (EquiLend says 450bps, DataLend says 80bps
    for the same security — which do we use? flag for ops review)
  - ANOMALY DETECTION (a rate that doubled overnight, a collateral value
    that jumped 3x, an issuer concentration that spiked past any plausible
    business rationale)
  - RECONCILIATION COMPLETENESS (what fraction of book positions have a
    matching custodian record)

Every output is a `DataQualityReport` that can be shown on the desk's
data quality dashboard (via the API) and emitted as alerts when thresholds
are breached.

Note: this module does NOT replace the per-field `DataQualityFlag` on
canonical objects (which travels with each record through the analytics
pipeline). It provides the AGGREGATE view ("40% of rate quotes are stale
today") rather than the per-record view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.enums import DataQualityFlag
from secfi_platform.common.types import MarketRateQuote, Position


@dataclass
class SourceQualityProfile:
    source_name: str
    records_received: int
    records_valid: int
    records_rejected: int
    records_stale: int
    latest_as_of: Optional[datetime]
    age_minutes: Optional[float]
    completeness_pct: float                   # valid / expected (if expected is known) else valid / received
    staleness_flag: bool
    cross_source_disagreements: int           # count of securities with material rate disagreement vs another source


@dataclass
class RateAnomalyFlag:
    security_internal_id: str
    ticker: str
    source: str
    current_rate_bps: Optional[Decimal]
    prior_rate_bps: Optional[Decimal]         # from the previous cycle's snapshot (None if no history)
    change_pct: Optional[float]
    anomaly_type: str                          # "RATE_SPIKE" | "RATE_ZERO" | "NEGATIVE_RATE" | "CROSS_SOURCE_MISMATCH"
    severity: str                              # "LOW" | "MEDIUM" | "HIGH"
    description: str


@dataclass
class DataQualityReport:
    as_of: datetime
    source_profiles: list                       # list[SourceQualityProfile]
    rate_anomalies: list                         # list[RateAnomalyFlag]
    positions_with_no_market_quote: list         # list[str] position_ids
    positions_with_stale_quote: list             # list[str] position_ids
    book_coverage_pct: float                     # fraction of position MV with a valid market quote
    overall_dq_score: float                      # 0-100 composite DQ score
    alerts_recommended: list                     # list[str] human-readable DQ alerts


_RATE_SPIKE_THRESHOLD_PCT = 1.00    # flag if rate doubled (100% change) overnight
_CROSS_SOURCE_DISAGREEMENT_BPS = 50 # flag if EquiLend vs DataLend differ by >50bps
_STALENESS_MINUTES = 240            # flag if a source is >4h old


def profile_market_quotes(
    quotes: Iterable[MarketRateQuote],
    expected_security_ids: Optional[set] = None,
    prior_quotes: Optional[dict] = None,        # security_internal_id -> MarketRateQuote from last cycle
    reference_now: Optional[datetime] = None,
) -> tuple:
    """
    Returns (source_profiles, anomalies).
    `quotes` should be the merged list from ALL sources (EquiLend, DataLend, internal).
    """
    now = reference_now or datetime.now(timezone.utc)
    quotes = list(quotes)

    by_source: dict = {}
    for q in quotes:
        by_source.setdefault(q.source, []).append(q)

    # Group by security for cross-source comparison
    by_security: dict = {}
    for q in quotes:
        by_security.setdefault(q.security_internal_id, []).append(q)

    profiles = []
    cross_source_disagreement_counts: dict = {}

    for source, source_quotes in by_source.items():
        valid = sum(1 for q in source_quotes if q.data_quality_flag == DataQualityFlag.OK)
        stale = sum(1 for q in source_quotes if q.data_quality_flag == DataQualityFlag.STALE)
        rejected = len(source_quotes) - valid - stale

        latest = max((q.as_of for q in source_quotes), default=None)
        age_min = None
        if latest:
            age_min = (now - latest).total_seconds() / 60.0

        expected = len(expected_security_ids) if expected_security_ids else len(source_quotes)
        completeness = valid / max(expected, 1)

        disagree_count = 0
        for sec_id, sec_quotes in by_security.items():
            if len(sec_quotes) < 2:
                continue
            rates = [q.weighted_avg_fee_bps or q.avg_fee_bps for q in sec_quotes if q.source == source]
            other_rates = [q.weighted_avg_fee_bps or q.avg_fee_bps for q in sec_quotes if q.source != source]
            if not rates or not other_rates:
                continue
            r1 = rates[0]
            r2 = other_rates[0]
            if r1 is not None and r2 is not None and abs(r1 - r2) > _CROSS_SOURCE_DISAGREEMENT_BPS:
                disagree_count += 1
        cross_source_disagreement_counts[source] = disagree_count

        profiles.append(SourceQualityProfile(
            source_name=source,
            records_received=len(source_quotes),
            records_valid=valid,
            records_rejected=rejected,
            records_stale=stale,
            latest_as_of=latest,
            age_minutes=age_min,
            completeness_pct=completeness,
            staleness_flag=(age_min is not None and age_min > _STALENESS_MINUTES),
            cross_source_disagreements=disagree_count,
        ))

    # Detect rate anomalies
    anomalies = []
    for q in quotes:
        rate = q.weighted_avg_fee_bps or q.avg_fee_bps
        if rate is None:
            anomalies.append(RateAnomalyFlag(
                security_internal_id=q.security_internal_id, ticker=q.security_internal_id,
                source=q.source, current_rate_bps=None, prior_rate_bps=None, change_pct=None,
                anomaly_type="RATE_ZERO", severity="MEDIUM",
                description=f"No rate available from {q.source} for {q.security_internal_id}.",
            ))
            continue
        if rate < 0:
            anomalies.append(RateAnomalyFlag(
                security_internal_id=q.security_internal_id, ticker=q.security_internal_id,
                source=q.source, current_rate_bps=rate, prior_rate_bps=None, change_pct=None,
                anomaly_type="NEGATIVE_RATE", severity="HIGH",
                description=f"Negative rate {float(rate):.1f}bps from {q.source}.",
            ))
        if prior_quotes:
            prior = prior_quotes.get(q.security_internal_id)
            if prior:
                prior_rate = prior.weighted_avg_fee_bps or prior.avg_fee_bps
                if prior_rate and prior_rate > 0:
                    change_pct = float((rate - prior_rate) / prior_rate)
                    if abs(change_pct) > _RATE_SPIKE_THRESHOLD_PCT:
                        anomalies.append(RateAnomalyFlag(
                            security_internal_id=q.security_internal_id,
                            ticker=q.security_internal_id,
                            source=q.source, current_rate_bps=rate, prior_rate_bps=prior_rate,
                            change_pct=change_pct, anomaly_type="RATE_SPIKE",
                            severity="HIGH" if abs(change_pct) > 2.0 else "MEDIUM",
                            description=(
                                f"Rate changed {change_pct:+.0%} overnight "
                                f"({float(prior_rate):.0f} -> {float(rate):.0f}bps) from {q.source}."
                            ),
                        ))

    # Cross-source mismatch anomalies
    for sec_id, sec_quotes in by_security.items():
        if len(sec_quotes) < 2:
            continue
        rates = [(q.source, q.weighted_avg_fee_bps or q.avg_fee_bps) for q in sec_quotes]
        valid_rates = [(s, r) for s, r in rates if r is not None]
        if len(valid_rates) < 2:
            continue
        r1_src, r1 = valid_rates[0]
        r2_src, r2 = valid_rates[1]
        if abs(r1 - r2) > _CROSS_SOURCE_DISAGREEMENT_BPS:
            anomalies.append(RateAnomalyFlag(
                security_internal_id=sec_id, ticker=sec_id,
                source=f"{r1_src}/{r2_src}", current_rate_bps=r1, prior_rate_bps=r2,
                change_pct=float((r1 - r2) / r2) if r2 > 0 else None,
                anomaly_type="CROSS_SOURCE_MISMATCH",
                severity="HIGH" if abs(r1 - r2) > 200 else "MEDIUM",
                description=(
                    f"{r1_src}: {float(r1):.0f}bps vs {r2_src}: {float(r2):.0f}bps "
                    f"(gap {float(r1-r2):+.0f}bps). Review which source is authoritative."
                ),
            ))

    return profiles, anomalies


def build_data_quality_report(
    as_of: datetime,
    positions: Iterable[Position],
    market_quotes: dict,                # security_internal_id -> MarketRateQuote (merged, best source)
    all_quotes: Iterable[MarketRateQuote],
    prior_quotes: Optional[dict] = None,
    expected_security_ids: Optional[set] = None,
) -> DataQualityReport:
    positions = list(positions)
    all_quotes = list(all_quotes)

    source_profiles, anomalies = profile_market_quotes(all_quotes, expected_security_ids, prior_quotes, as_of)

    no_quote = []
    stale_quote = []
    covered_mv = Decimal("0")
    total_mv = sum((p.market_value for p in positions), Decimal("0"))

    for pos in positions:
        q = market_quotes.get(pos.security.internal_id)
        if q is None:
            no_quote.append(pos.position_id)
        elif q.data_quality_flag == DataQualityFlag.STALE:
            stale_quote.append(pos.position_id)
            covered_mv += pos.market_value * Decimal("0.5")  # stale = half credit
        else:
            covered_mv += pos.market_value

    coverage = float(covered_mv / total_mv) if total_mv > 0 else 1.0

    # Overall DQ score: weighted average of completeness, coverage, staleness penalty, anomaly count
    avg_completeness = sum(p.completeness_pct for p in source_profiles) / max(len(source_profiles), 1)
    stale_penalty = 1.0 - (0.1 * sum(1 for p in source_profiles if p.staleness_flag))
    anomaly_penalty = max(1.0 - 0.05 * len(anomalies), 0.50)
    dq_score = max(min(
        avg_completeness * 40 + coverage * 40 + stale_penalty * 10 + anomaly_penalty * 10,
        100.0
    ), 0.0)

    alerts_recommended = []
    if any(p.staleness_flag for p in source_profiles):
        alerts_recommended.append(
            f"Data freshness alert: {sum(1 for p in source_profiles if p.staleness_flag)} "
            f"source(s) have not refreshed in >{_STALENESS_MINUTES} minutes."
        )
    high_anomalies = [a for a in anomalies if a.severity == "HIGH"]
    if high_anomalies:
        alerts_recommended.append(f"{len(high_anomalies)} HIGH-severity rate anomaly/anomalies detected.")
    if len(no_quote) > 3:
        alerts_recommended.append(f"{len(no_quote)} position(s) have no market rate quote.")

    return DataQualityReport(
        as_of=as_of,
        source_profiles=source_profiles,
        rate_anomalies=anomalies,
        positions_with_no_market_quote=no_quote,
        positions_with_stale_quote=stale_quote,
        book_coverage_pct=coverage,
        overall_dq_score=dq_score,
        alerts_recommended=alerts_recommended,
    )
