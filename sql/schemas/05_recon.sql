-- ============================================================================
-- 05_recon.sql
-- Reconciliation breaks, settlement fails, and locate shortages.
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.reconciliation_break (
    break_id                    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of                          DATE          NOT NULL,
    position_id                      VARCHAR(64)   REFERENCES secfi.position(position_id),
    security_internal_id                VARCHAR(32)   REFERENCES secfi.security(internal_id),
    counterparty_id                        VARCHAR(32)   REFERENCES secfi.counterparty(counterparty_id),
    break_type                                VARCHAR(32)   NOT NULL CHECK (break_type IN (
                                                'QUANTITY_MISMATCH','PRICE_RATE_MISMATCH','MISSING_AT_CUSTODIAN',
                                                'MISSING_ON_BOOK','DUPLICATE_ENTRY','STALE_POSITION',
                                                'SETTLEMENT_TIMING','COLLATERAL_MISMATCH',
                                                'CORPORATE_ACTION_UNADJUSTED')),
    severity                                    VARCHAR(16)   NOT NULL CHECK (severity IN
                                                  ('LOW','MEDIUM','HIGH','CRITICAL')),
    book_value                                    NUMERIC(20,2),
    external_value                                  NUMERIC(20,2),
    external_source                                    VARCHAR(64)   NOT NULL,
    delta                                                NUMERIC(20,2),
    probable_root_cause                                    TEXT          NOT NULL,
    recommended_action                                        TEXT          NOT NULL,
    buyin_risk_relevant                                          BOOLEAN       NOT NULL DEFAULT FALSE,
    capital_misstatement_relevant                                  BOOLEAN       NOT NULL DEFAULT FALSE,
    status                                                            VARCHAR(16)   NOT NULL DEFAULT 'OPEN'
                                                                       CHECK (status IN ('OPEN','IN_PROGRESS','RESOLVED','ACCEPTED_RISK')),
    opened_at                                                            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    resolved_at                                                            TIMESTAMPTZ,
    resolution_notes                                                          TEXT
);

CREATE INDEX IF NOT EXISTS ix_recon_break_status ON secfi.reconciliation_break(status);
CREATE INDEX IF NOT EXISTS ix_recon_break_severity ON secfi.reconciliation_break(severity);
CREATE INDEX IF NOT EXISTS ix_recon_break_asof ON secfi.reconciliation_break(as_of);

CREATE TABLE IF NOT EXISTS secfi.settlement_fail (
    fail_id                BIGSERIAL     PRIMARY KEY,
    position_id                VARCHAR(64)   NOT NULL REFERENCES secfi.position(position_id),
    security_internal_id          VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    counterparty_id                  VARCHAR(32)   NOT NULL REFERENCES secfi.counterparty(counterparty_id),
    fail_date                            DATE          NOT NULL,
    fail_quantity                          NUMERIC(24,4) NOT NULL,
    is_desk_receiving                        BOOLEAN       NOT NULL,
    fail_age_days                              INTEGER       NOT NULL,
    resolved_at                                  TIMESTAMPTZ,
    created_at                                      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_fail_position ON secfi.settlement_fail(position_id);
CREATE INDEX IF NOT EXISTS ix_fail_age ON secfi.settlement_fail(fail_age_days DESC);

CREATE TABLE IF NOT EXISTS secfi.locate_shortage (
    shortage_id              BIGSERIAL     PRIMARY KEY,
    security_internal_id        VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    as_of                            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    requested_quantity                  NUMERIC(24,4) NOT NULL,
    available_quantity                      NUMERIC(24,4) NOT NULL
);

CREATE TABLE IF NOT EXISTS secfi.substitute_inventory (
    security_internal_id        VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    substitute_security_id          VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    PRIMARY KEY (security_internal_id, substitute_security_id)
);
