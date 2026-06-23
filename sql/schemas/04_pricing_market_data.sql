-- ============================================================================
-- 04_pricing_market_data.sql
-- Persisted market rate observations from EquiLend, DataLend, and internal
-- executed-trade history, plus the dispersion/specialness classification
-- computed each cycle (a queryable history of pricing/pricing_intelligence.py
-- outputs for trend analysis and model validation).
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.market_rate_quote (
    quote_id                  BIGSERIAL     PRIMARY KEY,
    security_internal_id          VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    as_of                            TIMESTAMPTZ   NOT NULL,
    source                              VARCHAR(32)   NOT NULL CHECK (source IN
                                          ('EQUILEND','DATALEND','INTERNAL_EXECUTED')),
    avg_fee_bps                          NUMERIC(10,2),
    weighted_avg_fee_bps                    NUMERIC(10,2),
    min_fee_bps                              NUMERIC(10,2),
    max_fee_bps                                NUMERIC(10,2),
    utilization_pct                              NUMERIC(6,4),
    total_lendable_value                          NUMERIC(20,2),
    total_on_loan_value                              NUMERIC(20,2),
    sample_count                                       INTEGER,
    data_quality_flag                                     VARCHAR(32)   NOT NULL DEFAULT 'OK',
    ingested_at                                              TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_market_rate_security_asof
    ON secfi.market_rate_quote(security_internal_id, as_of DESC);
CREATE INDEX IF NOT EXISTS ix_market_rate_source ON secfi.market_rate_quote(source);

CREATE TABLE IF NOT EXISTS secfi.pricing_dispersion_snapshot (
    snapshot_id                BIGSERIAL     PRIMARY KEY,
    as_of_cycle                    TIMESTAMPTZ   NOT NULL,
    security_internal_id              VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    desk_rate_bps                        NUMERIC(10,2) NOT NULL,
    market_weighted_avg_bps                  NUMERIC(10,2),
    gap_bps                                    NUMERIC(10,2),
    specialness_tier                              VARCHAR(32)   NOT NULL CHECK (specialness_tier IN
                                                    ('GC','WARM','SPECIALS_IN_WAITING','SPECIAL','HTB','DEEP_SPECIAL')),
    z_score_within_tier                              NUMERIC(10,4)
);

CREATE INDEX IF NOT EXISTS ix_dispersion_cycle ON secfi.pricing_dispersion_snapshot(as_of_cycle);
CREATE INDEX IF NOT EXISTS ix_dispersion_tier ON secfi.pricing_dispersion_snapshot(specialness_tier);

COMMENT ON TABLE secfi.pricing_dispersion_snapshot IS
  'One row per security per cycle run. Retained historically (not just '
  'latest) so model_risk.md backtesting can measure realized P&L vs. '
  'predicted repricing opportunity over time.';
