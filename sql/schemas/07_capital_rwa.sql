-- ============================================================================
-- 07_capital_rwa.sql
-- Persisted capital/RWA profiles (desk-approximation, see
-- risk/capital_rwa.py governance note) and the unified recommendation
-- queue every engine writes into.
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.position_capital_profile (
    profile_id                BIGSERIAL     PRIMARY KEY,
    as_of_cycle                  TIMESTAMPTZ   NOT NULL,
    position_id                      VARCHAR(64)   NOT NULL REFERENCES secfi.position(position_id),
    ead_usd                              NUMERIC(20,2) NOT NULL,
    risk_weight_pct                          NUMERIC(8,4)  NOT NULL,
    rwa_usd                                      NUMERIC(20,2) NOT NULL,
    leverage_exposure_usd                            NUMERIC(20,2) NOT NULL,
    annualized_revenue_usd                              NUMERIC(20,2) NOT NULL,
    capital_cost_usd                                        NUMERIC(20,2) NOT NULL,
    return_on_balance_sheet                                    NUMERIC(10,6),
    return_on_capital                                              NUMERIC(10,6)
);

CREATE INDEX IF NOT EXISTS ix_capital_profile_position ON secfi.position_capital_profile(position_id);
CREATE INDEX IF NOT EXISTS ix_capital_profile_cycle ON secfi.position_capital_profile(as_of_cycle);

CREATE TABLE IF NOT EXISTS secfi.recommendation (
    recommendation_id          UUID          PRIMARY KEY,
    generated_at                  TIMESTAMPTZ   NOT NULL,
    source_engine                    VARCHAR(64)   NOT NULL,
    action                              VARCHAR(32)   NOT NULL,
    target_type                          VARCHAR(16)   NOT NULL CHECK (target_type IN
                                          ('POSITION','COUNTERPARTY','SECURITY','BOOK_SLICE')),
    target_id                                VARCHAR(64)   NOT NULL,
    quantity                                    NUMERIC(20,2),
    from_value                                    NUMERIC(20,4),
    to_value                                        NUMERIC(20,4),
    estimated_pnl_impact_usd                            NUMERIC(20,2),
    estimated_capital_impact_usd                            NUMERIC(20,2),
    estimated_rwa_impact_usd                                    NUMERIC(20,2),
    rationale                                                      TEXT          NOT NULL,
    supporting_metrics                                                JSONB,
    confidence                                                          NUMERIC(5,4)  NOT NULL,
    data_completeness_pct                                                  NUMERIC(5,4)  NOT NULL,
    priority_score                                                            NUMERIC(6,2)  NOT NULL,
    approval_status                                                              VARCHAR(16)   NOT NULL DEFAULT 'PROPOSED'
                                                                                    CHECK (approval_status IN
                                                                                    ('PROPOSED','UNDER_REVIEW','APPROVED','REJECTED','EXPIRED','EXECUTED')),
    expires_at                                                                       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_recommendation_status ON secfi.recommendation(approval_status);
CREATE INDEX IF NOT EXISTS ix_recommendation_engine ON secfi.recommendation(source_engine);
CREATE INDEX IF NOT EXISTS ix_recommendation_priority ON secfi.recommendation(priority_score DESC);

CREATE TABLE IF NOT EXISTS secfi.recommendation_approval_log (
    log_id                BIGSERIAL     PRIMARY KEY,
    recommendation_id        UUID          NOT NULL REFERENCES secfi.recommendation(recommendation_id),
    decision                    VARCHAR(16)   NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
    decided_by                    VARCHAR(64)   NOT NULL,
    decided_at                      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    comment                            TEXT
);

COMMENT ON TABLE secfi.recommendation_approval_log IS
  'Immutable audit trail of every human approval/rejection decision. '
  'Required by docs/governance.md control framework — every production '
  'change must be traceable.';
