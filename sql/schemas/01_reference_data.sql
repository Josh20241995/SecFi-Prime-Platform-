-- ============================================================================
-- 01_reference_data.sql
-- Reference data: securities, issuers, FX rates, yield curves.
-- Target: PostgreSQL 14+. Adjust types for the firm's standard RDBMS if
-- different (Sybase/SQL Server are still common in legacy prime brokerage
-- stacks — see docs/architecture.md "Data Layer" for the portability notes).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS secfi;

CREATE TABLE IF NOT EXISTS secfi.issuer (
    issuer_id           VARCHAR(32)   PRIMARY KEY,
    issuer_name         VARCHAR(256)  NOT NULL,
    country_of_domicile CHAR(2)       NOT NULL,
    gics_sector         VARCHAR(64),
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS secfi.security (
    internal_id         VARCHAR(32)   PRIMARY KEY,
    cusip               VARCHAR(9),
    isin                VARCHAR(12),
    sedol               VARCHAR(7),
    ticker              VARCHAR(32)   NOT NULL,
    description         VARCHAR(256),
    product_type        VARCHAR(32)   NOT NULL CHECK (product_type IN (
                            'EQUITY','ETF','ADR','CORPORATE_BOND','GOVT_BOND',
                            'GC_REPO','SPECIALS_REPO','SBL')),
    currency             CHAR(3)       NOT NULL,
    country_of_risk      CHAR(2)       NOT NULL,
    issuer_id            VARCHAR(32)   NOT NULL REFERENCES secfi.issuer(issuer_id),
    is_adr                BOOLEAN       NOT NULL DEFAULT FALSE,
    adr_ratio              NUMERIC(18,6),
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_security_cusip ON secfi.security(cusip);
CREATE INDEX IF NOT EXISTS ix_security_isin ON secfi.security(isin);
CREATE INDEX IF NOT EXISTS ix_security_issuer ON secfi.security(issuer_id);

CREATE TABLE IF NOT EXISTS secfi.security_index_membership (
    internal_id    VARCHAR(32)  NOT NULL REFERENCES secfi.security(internal_id),
    index_code     VARCHAR(32)  NOT NULL,
    PRIMARY KEY (internal_id, index_code)
);

CREATE TABLE IF NOT EXISTS secfi.fx_rate (
    base_ccy     CHAR(3)      NOT NULL,
    quote_ccy    CHAR(3)      NOT NULL,
    rate          NUMERIC(20,8) NOT NULL,
    as_of          TIMESTAMPTZ   NOT NULL,
    source          VARCHAR(64)   NOT NULL,
    PRIMARY KEY (base_ccy, quote_ccy, as_of, source)
);

CREATE TABLE IF NOT EXISTS secfi.yield_curve_point (
    curve_id      VARCHAR(32)   NOT NULL,
    tenor_days    INTEGER       NOT NULL,
    rate_pct      NUMERIC(10,6) NOT NULL,
    as_of          DATE          NOT NULL,
    PRIMARY KEY (curve_id, tenor_days, as_of)
);

COMMENT ON TABLE secfi.security IS
  'Canonical security reference data. internal_id is the platform-wide key; '
  'cusip/isin/sedol are cross-reference keys to vendor/custodian systems.';
