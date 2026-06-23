-- ============================================================================
-- 06_corporate_actions.sql
-- Corporate action events and computed impact assessments.
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.corporate_action_event (
    event_id                VARCHAR(64)   PRIMARY KEY,
    security_internal_id        VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    action_type                    VARCHAR(32)   NOT NULL CHECK (action_type IN (
                                    'CASH_DIVIDEND','STOCK_DIVIDEND','SPLIT','REVERSE_SPLIT','MERGER',
                                    'TENDER_OFFER','SPIN_OFF','REDEMPTION','BOND_CALL','COUPON_PAYMENT',
                                    'UST_AUCTION_SETTLEMENT','ADR_RATIO_CHANGE','INDEX_REBALANCE',
                                    'VOLUNTARY_ELECTION','MANDATORY_OTHER')),
    announce_date                    DATE          NOT NULL,
    record_date                        DATE,
    ex_date                              DATE,
    effective_date                        DATE,
    payment_date                            DATE,
    is_mandatory                              BOOLEAN       NOT NULL,
    election_deadline                            DATE,
    terms_summary                                  TEXT,
    source                                            VARCHAR(64)   NOT NULL DEFAULT 'CORP_ACTIONS_FEED',
    data_quality_flag                                    VARCHAR(32)   NOT NULL DEFAULT 'OK',
    ingested_at                                              TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ca_security ON secfi.corporate_action_event(security_internal_id);
CREATE INDEX IF NOT EXISTS ix_ca_record_date ON secfi.corporate_action_event(record_date);
CREATE INDEX IF NOT EXISTS ix_ca_ex_date ON secfi.corporate_action_event(ex_date);

CREATE TABLE IF NOT EXISTS secfi.corporate_action_impact (
    impact_id                  BIGSERIAL     PRIMARY KEY,
    event_id                      VARCHAR(64)   NOT NULL REFERENCES secfi.corporate_action_event(event_id),
    as_of_cycle                      TIMESTAMPTZ   NOT NULL,
    supply_risk_score                    NUMERIC(6,2)  NOT NULL,
    recall_risk_score                        NUMERIC(6,2)  NOT NULL,
    rate_dislocation_risk_score                  NUMERIC(6,2)  NOT NULL,
    settlement_fail_risk_score                       NUMERIC(6,2)  NOT NULL,
    balance_sheet_impact_score                           NUMERIC(6,2)  NOT NULL,
    composite_risk_score                                    NUMERIC(6,2)  NOT NULL,
    urgency                                                    VARCHAR(16)   NOT NULL CHECK (urgency IN
                                                                  ('INFORMATIONAL','MONITOR','ACT_THIS_WEEK','ACT_TODAY','IMMEDIATE')),
    affected_market_value_usd                                      NUMERIC(20,2) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ca_impact_event ON secfi.corporate_action_impact(event_id);
CREATE INDEX IF NOT EXISTS ix_ca_impact_cycle ON secfi.corporate_action_impact(as_of_cycle);
