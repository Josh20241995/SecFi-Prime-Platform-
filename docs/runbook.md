# Runbook

## Scheduling

| Job | Script / function | Cadence | Depends on |
|---|---|---|---|
| Full EOD cycle | `scripts/run_daily_batch.py` -> `orchestration.scheduler.run_full_cycle` | Once daily, at batch close (after custodian EOD feed lands) | All ingestion sources, including custodian |
| Intraday fast cycle | `scripts/run_intraday_cycle.py` -> `run_full_cycle` (recon inputs omitted) | Every 15 minutes during trading hours | Internal trade capture, EquiLend/DataLend, corporate actions, settlement fails — NOT custodian |
| Corporate actions refresh | Folded into both cycles above (event data itself only needs daily refresh) | Daily pre-market | Corporate actions feed |
| Aging maintenance | `sql/procedures/sp_age_open_items.sql` | Once daily, immediately after EOD cycle completes | `secfi.settlement_fail`, `secfi.reconciliation_break` tables |

Example cron (adjust to the firm's actual scheduler — Airflow/Dagster/
Control-M DAG definitions are not included here since they are
platform-specific; the cadence and dependency graph above is what any of
them should encode):

```cron
# Full EOD cycle — 18:30 local, after custodian batch typically lands
30 18 * * 1-5  /usr/bin/python3 /app/scripts/run_daily_batch.py --as-of $(date +\%Y-\%m-\%d) --environment prod

# Intraday fast cycle — every 15 min, 07:00-18:00 local trading hours
*/15 7-18 * * 1-5  /usr/bin/python3 /app/scripts/run_intraday_cycle.py --as-of $(date +\%Y-\%m-\%d)

# Aging maintenance — 19:00 local, after EOD cycle
0 19 * * 1-5  psql -h $SECFI_DB_HOST -U $SECFI_DB_USER -d secfi_platform_prod -c "CALL secfi.sp_age_open_items();"
```

## Monitoring

| Signal | Where | Alert condition |
|---|---|---|
| Cycle completion | `secfi.cycle_run_log.status` | `FAILED` or `PARTIAL`, or no `SUCCESS` row in the expected window (missed run) |
| Data freshness | `DataQualityFlag` on ingested objects + `alerting/alert_engine.py alert_from_stale_data` | Source older than its configured threshold (`configs/base.yaml` `data_quality.*_stale_after_hours`, `alerting.thresholds.stale_data_minutes`) |
| API health | `GET /healthz` | Non-200 response, or `status != "ok"` |
| Container health | Docker `HEALTHCHECK` (`infra/docker/Dockerfile`) | 3 consecutive failures -> orchestrator restarts the container |
| Unresolved CRITICAL alerts | `secfi.alert WHERE severity='CRITICAL' AND acknowledged_at IS NULL` | Any row older than 1 hour during trading hours |
| Optimization solver health | `cycle_run_log` correlated with `OptimizationResult.solver_status` | Repeated `INFEASIBLE_OR_ERROR` across consecutive cycles — likely a constraint configuration problem, not a one-off data issue |

## Performance checks

Run `EXPLAIN ANALYZE` on `secfi.v_book_summary` and
`secfi.v_counterparty_exposure_rollup` after any schema change to the
underlying tables (`position`, `counterparty`, `market_rate_quote`) —
these views back the primary desk dashboard and must stay fast as book
size grows. Confirm the indexes listed in `sql/schemas/03_positions_book.sql`
and `sql/schemas/04_pricing_market_data.sql` are actually being used by
the query planner, not sequentially scanned.

## Incident response

### A cycle fails partway through
1. Check `secfi.cycle_run_log` for the `correlation_id` and `error_summary`
2. Grep structured logs for that `correlation_id` (every log line from
   that cycle carries it — `common/logging_setup.py`) to find the exact
   engine and exception
3. If the failure is a single bad data row, confirm it was correctly
   isolated (check `secfi.data_quality_exception_log`) rather than having
   taken down the whole batch — if the whole batch failed, that itself is
   a bug (normalization is designed to isolate row-level failures, not
   batch-level ones)
4. Re-run the cycle once root cause is fixed: `run_full_cycle` is
   idempotent given the same input vintage (verified by
   `test_cycle_is_deterministic_given_same_inputs`)

### A data source is down
1. `DataSourceUnavailableError` should have been raised and logged with
   `source_name` and `reason` — confirm in logs
2. Per-source fallback policy:
   - EquiLend/DataLend down: pricing/optimization recommendations
     degrade in confidence (staleness penalty) but the cycle continues
     using the last successfully ingested snapshot if the orchestration
     layer is configured to retain one (production hardening item — this
     reference build does not yet implement a "last known good" cache;
     see `docs/assumptions_and_limitations.md`)
   - Custodian feed down: EOD cycle should skip reconciliation for that
     day (matches `CycleInputs.book_recon_df=None` behavior) rather than
     reconcile against stale custodian data and raise false breaks
   - Internal trade capture down: **this is the most severe case** —
     escalate immediately, the book itself is unknown; do not run any
     cycle on a stale/partial book without explicit risk sign-off

### A reconciliation break count spikes unexpectedly
1. Check whether a corporate action effective today explains a wave of
   quantity breaks (`secfi.corporate_action_event` for the as-of date) —
   this is the single most common legitimate cause and the reconciliation
   engine's `probable_root_cause` text already suggests checking this first
2. If not CA-related, check whether the custodian feed itself changed
   format/schema (a normalization-layer rejection spike in
   `secfi.data_quality_exception_log` would indicate this, not a
   reconciliation break spike — distinguish the two failure modes)

### The optimizer returns INFEASIBLE
1. Check `OptimizationResult.infeasible_reason` (passed through from
   `scipy.optimize.linprog`'s message)
2. Most common cause: a counterparty limit configured below the
   currently-booked balance with that counterparty, making "keep current
   position" itself infeasible under the constraint as written — review
   `OptimizationConstraints.counterparty_limits_usd` against the actual
   book before assuming a platform bug

### A buy-in risk alert fires (severity CRITICAL, score ≥85)
This is a live operational risk situation, not a platform bug. Follow
the firm's existing buy-in/fails escalation procedure; this platform's
role is to have surfaced it early with a ranked `recommended_action`
(RECALL / RETURN / SUBSTITUTE / HEDGE) and supporting rationale — use
`GET /v1/recall-buyin/queue` for the full ranked list and
`supporting_metrics.substitute_candidates` for alternative inventory if
the recommended action is SUBSTITUTE.

## Common operational questions

**"Why did a recommendation I expected to see not appear?"**
Check the noise-suppression thresholds first —
`min_economic_pickup_bps` (optimization/pricing),
`min_actionable_gap_bps` (pricing) — small, real opportunities are
deliberately suppressed below these thresholds to avoid recommendation
fatigue. Confirm via `GET /v1/pricing/recommendations` whether the
position appears in the underlying dispersion view even without
generating a full recommendation (roadmap: dedicated dispersion endpoint,
see `docs/reporting_design.md`).

**"Why is a recommendation's confidence so low?"**
Confidence is multiplicative across data completeness, model certainty,
and data freshness (`explainability/explain.py`). A low confidence score
on an otherwise-sensible-looking recommendation almost always traces to
either a small economic signal (low `model_certainty`) or stale upstream
data (`staleness_penalty`) — check `supporting_metrics` and the
originating `MarketRateQuote.data_quality_flag` / `as_of` timestamp.

**"Can I just turn off an engine I don't need?"**
Not directly exposed as a feature flag per-engine in this reference
build (only `feature_flags.enable_optimization_engine` exists in
`configs/dev.yaml`/`configs/prod.yaml` as a placeholder). Adding
per-engine flags to `orchestration/scheduler.py run_full_cycle` is a
small, low-risk extension if needed — see README "Future Roadmap."
