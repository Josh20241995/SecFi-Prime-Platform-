-- ============================================================================
-- 02_counterparty.sql
-- Counterparty master, credit attributes, and exposure limits.
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.counterparty (
    counterparty_id          VARCHAR(32)   PRIMARY KEY,
    legal_name                  VARCHAR(256)  NOT NULL,
    counterparty_type           VARCHAR(32)   NOT NULL CHECK (counterparty_type IN (
                                    'HEDGE_FUND','ASSET_MANAGER','BANK_DEALER',
                                    'PENSION_INSURANCE','CCP','SOVEREIGN_SUPRANATIONAL','OTHER')),
    tier                          VARCHAR(32)   NOT NULL CHECK (tier IN (
                                    'TIER_1_PRIME','TIER_2_STANDARD','TIER_3_WATCH','TIER_4_RESTRICTED')),
    region                         VARCHAR(16)   NOT NULL CHECK (region IN ('AMERICAS','EMEA','APAC')),
    lei                             CHAR(20),
    parent_group_id                VARCHAR(32),
    internal_credit_rating         VARCHAR(16),
    pd_1y                           NUMERIC(8,6),
    lgd_assumption                  NUMERIC(8,6),
    is_netting_eligible              BOOLEAN       NOT NULL DEFAULT FALSE,
    onboarding_date                  DATE,
    watch_list                        BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at                          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at                          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_counterparty_parent_group ON secfi.counterparty(parent_group_id);
CREATE INDEX IF NOT EXISTS ix_counterparty_watch_list ON secfi.counterparty(watch_list) WHERE watch_list = TRUE;

CREATE TABLE IF NOT EXISTS secfi.counterparty_booking_entity (
    counterparty_id  VARCHAR(32)  NOT NULL REFERENCES secfi.counterparty(counterparty_id),
    legal_entity      VARCHAR(32)  NOT NULL CHECK (legal_entity IN (
                          'US_BROKER_DEALER','UK_BROKER_DEALER','EU_BANK_ENTITY','APAC_BROKER_DEALER')),
    PRIMARY KEY (counterparty_id, legal_entity)
);

CREATE TABLE IF NOT EXISTS secfi.counterparty_limit (
    counterparty_id      VARCHAR(32)   PRIMARY KEY REFERENCES secfi.counterparty(counterparty_id),
    limit_usd               NUMERIC(20,2) NOT NULL,
    limit_source              VARCHAR(32)   NOT NULL DEFAULT 'CREDIT_RISK_SYSTEM',
    effective_date              DATE          NOT NULL,
    approved_by                  VARCHAR(64)   NOT NULL,
    last_reviewed_at              TIMESTAMPTZ
);

COMMENT ON TABLE secfi.counterparty_limit IS
  'Authoritative exposure limit per counterparty. This table should be '
  'populated/owned by the credit risk system feed, NOT edited directly by '
  'desk users. Platform code falls back to configs/risk_limits.yaml '
  'limit_templates_usd by tier ONLY when a row is absent here, and that '
  'fallback must be visibly flagged in any output that uses it.';
