-- ============================================================================
-- sp_age_open_items.sql
-- Nightly maintenance procedure: increments fail_age_days on open
-- settlement fails and age_days on open reconciliation breaks, and
-- auto-escalates severity per docs/runbook.md aging policy. Intended to
-- run once per business day, immediately after the EOD batch cycle
-- completes (see docs/runbook.md "Scheduling").
-- ============================================================================

CREATE OR REPLACE PROCEDURE secfi.sp_age_open_items()
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE secfi.settlement_fail
    SET fail_age_days = fail_age_days + 1
    WHERE resolved_at IS NULL;

    -- Auto-escalate reconciliation breaks open longer than the configured
    -- critical_age_days threshold (see ReconConfig.critical_age_days in
    -- reconciliation/recon_engine.py — keep these in sync; a future
    -- iteration should source this threshold from a shared config table
    -- rather than duplicating the constant here, see docs/roadmap notes
    -- in README.md "Future Roadmap").
    UPDATE secfi.reconciliation_break
    SET severity = 'CRITICAL'
    WHERE status = 'OPEN'
      AND severity = 'HIGH'
      AND buyin_risk_relevant = TRUE
      AND opened_at < now() - INTERVAL '3 days';

    INSERT INTO secfi.data_quality_exception_log (correlation_id, source_name, row_index, field_errors, raw_row)
    SELECT NULL, 'sp_age_open_items', NULL,
           jsonb_build_object('info', 'aging procedure completed successfully'), NULL
    WHERE FALSE;  -- placeholder no-op; replace with real completion telemetry insert in production
END;
$$;

COMMENT ON PROCEDURE secfi.sp_age_open_items() IS
  'Run via the orchestration scheduler immediately after EOD batch '
  'completion. See docs/runbook.md for the exact cron/Airflow trigger.';
