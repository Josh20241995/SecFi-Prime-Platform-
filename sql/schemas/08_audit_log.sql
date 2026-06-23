-- ============================================================================
-- 08_audit_log.sql
-- Platform-wide audit log and alert history.
-- ============================================================================

CREATE TABLE IF NOT EXISTS secfi.cycle_run_log (
    correlation_id        VARCHAR(64)   PRIMARY KEY,
    cycle_type                VARCHAR(16)   NOT NULL CHECK (cycle_type IN ('FULL','FAST','CORPORATE_ACTIONS_ONLY')),
    as_of                        DATE          NOT NULL,
    started_at                      TIMESTAMPTZ   NOT NULL,
    completed_at                      TIMESTAMPTZ,
    status                                VARCHAR(16)   NOT NULL DEFAULT 'RUNNING'
                                          CHECK (status IN ('RUNNING','SUCCESS','FAILED','PARTIAL')),
    config_snapshot_hash                    VARCHAR(64)   NOT NULL,
    code_version                              VARCHAR(32)   NOT NULL,
    positions_processed                          INTEGER,
    recommendations_generated                       INTEGER,
    alerts_raised                                      INTEGER,
    error_summary                                          TEXT
);

CREATE TABLE IF NOT EXISTS secfi.alert (
    alert_id                UUID          PRIMARY KEY,
    raised_at                  TIMESTAMPTZ   NOT NULL,
    correlation_id                VARCHAR(64)   REFERENCES secfi.cycle_run_log(correlation_id),
    severity                        VARCHAR(16)   NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    category                          VARCHAR(32)   NOT NULL,
    title                                VARCHAR(256)  NOT NULL,
    detail                                  TEXT          NOT NULL,
    related_entity_type                        VARCHAR(32)   NOT NULL,
    related_entity_id                              VARCHAR(64)   NOT NULL,
    requires_acknowledgement                            BOOLEAN       NOT NULL DEFAULT TRUE,
    acknowledged_by                                        VARCHAR(64),
    acknowledged_at                                            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_alert_severity ON secfi.alert(severity);
CREATE INDEX IF NOT EXISTS ix_alert_category ON secfi.alert(category);
CREATE INDEX IF NOT EXISTS ix_alert_unacked
    ON secfi.alert(requires_acknowledgement) WHERE acknowledged_at IS NULL;

CREATE TABLE IF NOT EXISTS secfi.data_quality_exception_log (
    exception_id        BIGSERIAL     PRIMARY KEY,
    correlation_id          VARCHAR(64)   REFERENCES secfi.cycle_run_log(correlation_id),
    source_name                VARCHAR(64)   NOT NULL,
    row_index                      INTEGER,
    field_errors                      JSONB         NOT NULL,
    raw_row                              JSONB,
    logged_at                              TIMESTAMPTZ   NOT NULL DEFAULT now()
);

COMMENT ON TABLE secfi.cycle_run_log IS
  'One row per orchestration cycle run (see orchestration/scheduler.py). '
  'config_snapshot_hash + code_version make every analytics output '
  'reproducible: given a correlation_id, an engineer can check out the '
  'exact code version and config snapshot and re-run the cycle against '
  'the same input vintage to reproduce any historical recommendation or '
  'alert byte-for-byte (replay tooling requirement, skill section 15).';
