# Data Model & Input Requirements

## Canonical domain model

All analytics engines operate on the dataclasses in `src/secfi_platform/common/types.py`.
This is the single contract every ingestion source must be normalized into.

```
Security ──────┐
               │ referenced by
Counterparty ──┼──> Position <── (1..N) ── CollateralLeg
               │
MarketRateQuote ──> keyed by Security.internal_id

CorporateActionEvent ──> keyed by Security.internal_id

ReconciliationBreak ──> references Position / Security / Counterparty

Recommendation ──> references any target (POSITION | COUNTERPARTY | SECURITY | BOOK_SLICE)
Alert ──> references any related entity
```

### `Security`
Reference data for one instrument. `internal_id` is the platform key;
`cusip`/`isin`/`sedol` are cross-reference keys to vendor/custodian
systems. `is_adr` + `adr_ratio` support ADR-specific corporate action
logic (ratio changes).

### `Counterparty`
The legal entity the desk faces. `tier` drives the fallback capital risk
weight (`risk/capital_rwa.py`) and fallback exposure limit
(`configs/risk_limits.yaml` `limit_templates_usd`) when no firm-set limit
exists. `parent_group_id` enables rollup across multiple legal entities of
the same fund family (see `sql/views/v_counterparty_exposure_rollup.sql`).
`pd_1y`/`lgd_assumption` feed RAROC-style expected-loss adjustment when
present; absent gracefully (expected_loss defaults to 0, documented in
`capital_rwa.py`).

### `Position`
One securities-finance line item — a loan, borrow, repo, or reverse repo.
`maturity_date = None` means open/evergreen. `collateral` is a tuple of
`CollateralLeg` (supports multiple legs per position, though this
reference build's fixtures use at most one). `rate_type_is_rebate`
distinguishes a securities-lending fee from a cash-collateral rebate rate
— this changes the sign of the revenue calculation in `capital_rwa.py`.

### `MarketRateQuote`
A market composite observation (EquiLend/DataLend/internal-executed) for
one security at one point in time. `utilization_pct` plus fee fields
drive specialness classification (`pricing/pricing_intelligence.py`).

### `CorporateActionEvent`
One corporate action. Multiple date fields are optional because different
action types populate different ones (a cash dividend has a record/ex/pay
date; a merger may have only a record date and election deadline). See
`corporate_actions/ca_impact_engine.py` `_key_date()` for the date
precedence used when multiple are present.

### `ReconciliationBreak`
Output of the reconciliation engine, not an input — included here because
it's persisted and re-read across cycles for aging (`age_days`,
`sql/procedures/sp_age_open_items.sql`).

### `Recommendation` / `Alert`
The shared output contract — see `docs/module_specs.md` "explainability"
section and `common/types.py` docstring for the full rationale.

## Required data per capability (and exact fallback when unavailable)

| Capability | Required data | If unavailable |
|---|---|---|
| Counterparty exposure | Position, Counterparty, CollateralLeg.haircut_pct | Haircut falls back to `configs/risk_limits.yaml` `haircuts_bps` by collateral type (see `risk/counterparty_risk.py` `DEFAULT_HAIRCUTS_BPS`) |
| Capital/RWA | Counterparty.tier, Counterparty.counterparty_type | Falls back to the most conservative risk weight in `DEFAULT_RISK_WEIGHTS_PCT` table for an unmapped (tier, type) pair |
| Counterparty limit | `secfi.counterparty_limit` row (credit risk system feed) | Falls back to `configs/risk_limits.yaml` `limit_templates_usd` by tier — **must be visibly flagged downstream as a fallback, never silently treated as a real limit** |
| Pricing dispersion | MarketRateQuote per security | Position excluded from dispersion scoring, flagged `MISSING` data quality — never silently assumed "fairly priced" |
| Recall/buy-in | SettlementFail, LocateShortage, SubstituteInventory | Missing substitute inventory increases (not decreases) the buy-in risk multiplier — absence of information is treated as elevated risk, not as "no problem" |
| Reconciliation | Book position extract + one external source (custodian or balance sheet) per cycle | If only one side present, no comparison possible — reconciliation step is skipped for that cycle, not silently passed |
| Corporate actions | CorporateActionEvent with at least one date field | Event with no date fields at all is excluded (cannot compute proximity); treated as `days_to_key_date=None`, proximity multiplier 0.5 (moderate, not zero) if at least one date exists |
| Wrong-way risk | Counterparty.counterparty_type, Security.gics_sector | Sector-less securities (e.g., government bonds) excluded from sector-concentration WWR check by design — see `risk/counterparty_risk.py` assumption A3 |

## SQL schema overview

See `sql/schemas/*.sql` for full DDL. Summary:

| File | Tables |
|---|---|
| `01_reference_data.sql` | `issuer`, `security`, `security_index_membership`, `fx_rate`, `yield_curve_point` |
| `02_counterparty.sql` | `counterparty`, `counterparty_booking_entity`, `counterparty_limit` |
| `03_positions_book.sql` | `position`, `collateral_leg`, `position_lifecycle_event` (immutable event log) |
| `04_pricing_market_data.sql` | `market_rate_quote`, `pricing_dispersion_snapshot` (historized per cycle) |
| `05_recon.sql` | `reconciliation_break`, `settlement_fail`, `locate_shortage`, `substitute_inventory` |
| `06_corporate_actions.sql` | `corporate_action_event`, `corporate_action_impact` (historized per cycle) |
| `07_capital_rwa.sql` | `position_capital_profile`, `recommendation`, `recommendation_approval_log` |
| `08_audit_log.sql` | `cycle_run_log`, `alert`, `data_quality_exception_log` |

Two views (`sql/views/`) provide the primary dashboard read models;
`sql/procedures/sp_age_open_items.sql` is the one piece of scheduled SQL
maintenance (run nightly, see `docs/runbook.md`).

## Data freshness expectations

| Source | Expected cadence | Staleness alert threshold |
|---|---|---|
| Internal trade capture | Real-time/intraday event stream in production | 15 min (fast cycle) |
| EquiLend / DataLend | Intraday composite refresh (vendor-dependent, typically every few hours) | 24h (`configs/base.yaml` `data_quality.market_rates_stale_after_hours`) |
| Custodian feed | T+1 EOD (most custodians) | 30h (`custodian_feed_stale_after_hours`) |
| Corporate actions feed | Daily pre-market | 24h |
| FX rates / yield curves | Intraday | 4h (`alerting.thresholds.stale_data_minutes` = 240) |
