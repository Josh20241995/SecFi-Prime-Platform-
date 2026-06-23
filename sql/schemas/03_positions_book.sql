-- ============================================================================
-- 03_positions_book.sql
-- The canonical securities-finance position ledger. This is the central
-- fact table every analytics engine ultimately reads from (directly or via
-- the v_book_summary view in sql/views/).
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.position (
    position_id           VARCHAR(64)   PRIMARY KEY,
    trade_date              DATE          NOT NULL,
    value_date                DATE          NOT NULL,
    maturity_date              DATE,                     -- NULL = open/evergreen
    direction                    VARCHAR(16)   NOT NULL CHECK (direction IN
                                  ('LEND','BORROW','REPO','REVERSE_REPO')),
    security_internal_id          VARCHAR(32)   NOT NULL REFERENCES secfi.security(internal_id),
    counterparty_id                  VARCHAR(32)   NOT NULL REFERENCES secfi.counterparty(counterparty_id),
    booking_entity                    VARCHAR(32)   NOT NULL,
    quantity                            NUMERIC(24,4) NOT NULL,
    market_value                         NUMERIC(20,2) NOT NULL,
    currency                              CHAR(3)       NOT NULL,
    rate_bps                               NUMERIC(10,2) NOT NULL,
    rate_type_is_rebate                     BOOLEAN       NOT NULL DEFAULT FALSE,
    term_type                                VARCHAR(8)    NOT NULL CHECK (term_type IN ('OPEN','TERM')),
    desk_id                                   VARCHAR(32)   NOT NULL,
    trader_id                                  VARCHAR(32)   NOT NULL,
    is_rehypothecable                           BOOLEAN       NOT NULL DEFAULT TRUE,
    last_recall_date                             DATE,
    last_return_date                              DATE,
    source_system                                  VARCHAR(64)   NOT NULL DEFAULT 'INTERNAL_TRADE_CAPTURE',
    data_quality_flag                               VARCHAR(32)   NOT NULL DEFAULT 'OK',
    is_active                                        BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at                                          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at                                          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_position_security ON secfi.position(security_internal_id);
CREATE INDEX IF NOT EXISTS ix_position_counterparty ON secfi.position(counterparty_id);
CREATE INDEX IF NOT EXISTS ix_position_trade_date ON secfi.position(trade_date);
CREATE INDEX IF NOT EXISTS ix_position_active ON secfi.position(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS ix_position_maturity ON secfi.position(maturity_date);

CREATE TABLE IF NOT EXISTS secfi.collateral_leg (
    collateral_leg_id     BIGSERIAL     PRIMARY KEY,
    position_id              VARCHAR(64)   NOT NULL REFERENCES secfi.position(position_id),
    collateral_type             VARCHAR(32)   NOT NULL CHECK (collateral_type IN (
                                    'CASH','GOVT_SECURITIES','AGENCY_SECURITIES','EQUITIES',
                                    'LETTER_OF_CREDIT','NON_CASH_OTHER')),
    market_value                  NUMERIC(20,2) NOT NULL,
    currency                        CHAR(3)       NOT NULL,
    haircut_pct                      NUMERIC(6,4)  NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_collateral_position ON secfi.collateral_leg(position_id);

-- Lifecycle events: recalls, returns, rolls, rate changes — the immutable
-- event log that lets the platform reconstruct "what did the book look
-- like at time T" for replay/incident reconstruction (skill section 15).
CREATE TABLE IF NOT EXISTS secfi.position_lifecycle_event (
    event_id        BIGSERIAL     PRIMARY KEY,
    position_id        VARCHAR(64)   NOT NULL REFERENCES secfi.position(position_id),
    event_type            VARCHAR(32)   NOT NULL CHECK (event_type IN (
                              'OPEN','RECALL','RETURN','RATE_CHANGE','ROLL','PARTIAL_RETURN',
                              'CORPORATE_ACTION_ADJUSTMENT','UNWIND')),
    event_timestamp           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    quantity_delta               NUMERIC(24,4),
    rate_bps_before                NUMERIC(10,2),
    rate_bps_after                  NUMERIC(10,2),
    initiated_by                      VARCHAR(64)   NOT NULL,
    source_system                       VARCHAR(64)   NOT NULL,
    correlation_id                        VARCHAR(64),
    notes                                   TEXT
);

CREATE INDEX IF NOT EXISTS ix_lifecycle_position ON secfi.position_lifecycle_event(position_id);
CREATE INDEX IF NOT EXISTS ix_lifecycle_timestamp ON secfi.position_lifecycle_event(event_timestamp);

COMMENT ON TABLE secfi.position_lifecycle_event IS
  'Immutable append-only event log. NEVER update or delete rows here; '
  'corrections are new events, not edits, to preserve full audit/replay capability.';
