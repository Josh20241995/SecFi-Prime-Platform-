# Module-by-Module Specification

For each module: purpose, inputs, outputs, key functions, and what a
production hardening pass must add. Full algorithmic detail is in
`docs/algorithms.md`; this document is the API-level contract reference.

---

## `common/` — shared kernel

| File | Purpose |
|---|---|
| `enums.py` | Controlled vocabularies (Direction, ProductType, BreakSeverity, etc.). Changes go through reference-data change control — see `docs/governance.md`. |
| `types.py` | Canonical dataclasses: `Security`, `Counterparty`, `Position`, `MarketRateQuote`, `CorporateActionEvent`, `ReconciliationBreak`, `Recommendation`, `Alert`. Every engine's input/output shape. |
| `config.py` | YAML config loader with base/environment overlay and `${ENV_VAR}` secret resolution. |
| `logging_setup.py` | Structured JSON logging with correlation IDs for cross-engine tracing. |

## `ingestion/` — external data acquisition

| File | Purpose |
|---|---|
| `base.py` | `SourceConnector` ABC, `DataSourceUnavailableError`, `DataQualityError`. |
| `connectors.py` | One connector class per source (EquiLend, DataLend, internal trade capture, custodian, market data, corporate actions, balance sheet, settlement fails). This reference build's implementations read local CSV snapshots; each class's docstring specifies exactly what a production implementation replaces it with (REST API, SFTP, Kafka topic, etc.). |

**Production hardening required:** real auth (OAuth2 client credentials via
firm secret manager), retry/backoff with circuit breaker, intraday vs.
EOD cadence per source, schema-version detection for vendor format changes.

## `normalization/` — validation gate

| File | Purpose |
|---|---|
| `schema_mapping.py` | `parse_security`, `parse_counterparty`, `parse_position`, `parse_market_rate_quote`, `parse_corporate_action_event`, `parse_fx_rate`. Each raises `DataQualityError` with field-level detail on a bad required field; `parse_rows()` isolates bad rows so one malformed record never fails an entire batch. |

**Production hardening required:** richer reconciliation matching keys
(see `docs/assumptions_and_limitations.md` OPS-2), schema drift detection,
a dead-letter table for rejected rows (`secfi.data_quality_exception_log`
already modeled in SQL for this).

## `risk/` — measurement, not recommendation

| File | Key functions | Purpose |
|---|---|---|
| `counterparty_risk.py` | `compute_counterparty_exposure`, `compute_book_exposure_by_counterparty` | Gross/net/collateralized exposure, stress scenarios, concentration (HHI), wrong-way-risk flags, limit utilization. |
| `capital_rwa.py` | `compute_position_capital_profile`, `compute_counterparty_capital_summary`, `marginal_capital_impact` | EAD/RWA/leverage-exposure approximation, return on balance sheet/capital, RAROC. **Desk decision-support approximation only — see governance note in the module docstring.** |
| `rates_fx.py` | `compute_dv01`, `compute_fx_exposure`, `compute_funding_gap`, `recommend_hedges`, `build_rates_fx_report` | Interest rate and FX risk measurement plus descriptive hedge suggestions (not optimized — see `docs/algorithms.md`). |

## `pricing/pricing_intelligence.py` — market vs. desk comparison

`classify_specialness`, `build_pricing_dispersion`,
`generate_pricing_recommendations`. Compares desk rates against market
composite (EquiLend/DataLend), classifies GC -> Deep Special, z-scores
mispricing within tier, emits REPRICE recommendations with estimated P&L.

## `optimization/book_optimizer.py` — the LP allocation engine

`optimize_book` (entry point), `OptimizationCandidate`,
`OptimizationConstraints`. Builds and solves a linear program over
(position, candidate-counterparty) reallocation variables. See
`docs/algorithms.md` for the full mathematical formulation and the MILP
upgrade path.

## `recall_buyin/recall_risk_engine.py` — urgency ranking

`compute_urgency_queue`, `compute_buyin_risk_score`,
`queue_to_recommendations`. Ranks open settlement fails by buy-in risk and
urgency, factoring specialness tier, locate shortages, CA-driven return
obligations, and substitute-inventory availability.

## `corporate_actions/ca_impact_engine.py` — 60-day forward scan

`build_corporate_action_watchlist`, `assess_corporate_action`,
`watchlist_to_recommendations`. Event-type-specific impact weights
(`EVENT_IMPACT_WEIGHTS`) scaled by proximity to the event's key date,
producing a composite risk score and an `ActionUrgency` classification.

## `growth/counterparty_growth.py` — grow/hold/reduce/reprice

`assess_counterparty_opportunity`, `opportunities_to_recommendations`.
Deterministic, explainable decision tree over return-on-capital, limit
utilization, watch-list status, and unresolved critical reconciliation
breaks.

## `explainability/explain.py` — shared contract

`compute_confidence`, `compute_priority_score`, `build_rationale`,
`rank_recommendations`. Used by every recommendation-producing engine so
outputs are comparable across engines in one unified queue. See module
docstring for the full confidence model.

## `reconciliation/recon_engine.py` — break detection

`reconcile`, `summarize_breaks`. Pandas-based set matching between book
and external (custodian/balance-sheet) records; classifies break type,
assigns severity, and flags buy-in-risk/capital-misstatement relevance.

## `alerting/alert_engine.py` — thresholds to human notifications

One `alert_from_*` function per upstream engine output type, plus
`collect_alerts` to merge and severity-sort. Routing (which Slack
channel/team) lives in config (`configs/base.yaml` `alerting.routing`),
not code.

## `reporting/daily_summary.py` — desk-facing aggregation

`build_daily_executive_summary`, `render_markdown`. Deliberately contains
zero new analytics — pure aggregation/ranking of upstream engine outputs.

## `orchestration/scheduler.py` — the job graph

`run_full_cycle` — the function a real DAG scheduler (Airflow/Dagster/
Control-M) invokes as its task body. Full job graph and recommended
cadence documented in the module docstring.

## `api/` — desk-facing service

| File | Purpose |
|---|---|
| `schemas.py` | Pydantic request/response DTOs (API boundary only). |
| `state.py` | In-memory "latest cycle outputs" store — **explicitly a reference-build simplification**, see docstring; production reads `sql/views/v_book_summary.sql` and friends. |
| `routers/desk.py` | All desk-facing GET endpoints + the one approval-decision POST endpoint. Read-mostly by design — see module docstring. |
| `main.py` | FastAPI app, CORS, health check. |

## `sql/` — persistence

Eight schema files (`01_reference_data.sql` through `08_audit_log.sql`),
two views, one maintenance procedure. See `docs/data_model.md` for the
full entity-relationship narrative.
