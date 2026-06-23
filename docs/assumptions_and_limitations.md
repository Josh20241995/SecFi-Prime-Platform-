# Assumptions & Limitations

This document consolidates every explicit assumption made throughout the
codebase and documentation, with a stable reference code so other docs
can point here (`R-3`, `OPS-2`, etc.) instead of re-explaining. Per the
governing skill's instruction: *"Where assumptions are required, state
them clearly and proceed."* Every assumption below was made because the
underlying data would not exist in this build environment, not because
it was inconvenient to model properly.

## Capital / Regulatory

**C-1 — Capital approximation, not official capital.**
`risk/capital_rwa.py` computes a desk-decision-support approximation
modeled on the shape of Basel SA-CCR for SFTs. It is not the firm's
certified regulatory capital engine and must never be used for external
reporting. See `docs/model_risk.md` and the prominent disclaimer at the
top of that module. **This is the single most important assumption in
the entire platform** and is repeated in three places deliberately
(module docstring, `docs/risk_framework.md`, `docs/model_risk.md`) so it
cannot be missed.

**C-2 — Risk weight table is illustrative.**
`DEFAULT_RISK_WEIGHTS_PCT` in `risk/capital_rwa.py` is a reasonable
reference table, not the firm's confirmed standardized/IRB risk weights.
Treasury/Capital Management must supply the authoritative table before
any production use beyond directional decision support.

## Risk

**R-3 — Wrong-way risk is flagged, not modeled.**
`risk/counterparty_risk.py` flags WWR via a sector-concentration
heuristic (≥35% of a bank-dealer counterparty's exposure in one GICS
sector). A genuine WWR model requires joint default/exposure correlation
data from the firm's credit risk system, which this build does not
assume access to.

**R-7 — Open/evergreen positions treated as overnight for rate-risk tenor bucketing.**
`risk/rates_fx.py` buckets a position with `maturity_date=None` into the
"O/N" DV01 tenor bucket. Real evergreen books often carry an internally
modeled "sticky" effective tenor (the book doesn't actually reprice or
unwind overnight in practice even though it legally could) — a production
deployment should connect Treasury's behavioral tenor assumptions instead
of the contractual-tenor default used here.

**R-9 — Stress scenarios apply uniform shocks, not historical/Monte Carlo paths.**
The six `STANDARD_SCENARIOS` in `risk/counterparty_risk.py` are
deterministic point shocks (e.g., "equity down 10%, parallel"), not
historical scenario replay or Monte Carlo simulation. Appropriate for
desk-level directional stress awareness; a full tail-risk/VaR framework
would need a simulation engine this build does not include (see
`docs/implementation_plan.md` Phase 4 for the extension path).

## Operational

**OPS-2 — Reconciliation matching key is simplified.**
`reconciliation/recon_engine.py` matches on
`(security_internal_id, counterparty_id, trade_date, direction)`. Real
reconciliation typically also incorporates a settlement instruction/SSI
ID and a custodian trade reference number, which this build does not
assume are available in the external feed. Where duplicate keys exist on
one side, the engine falls back to taking the first row and flags a
`DUPLICATE_ENTRY` break rather than attempting fuzzy quantity-based
matching.

**OPS-4 — Buy-in notice regime is a placeholder constant.**
`recall_buyin/recall_risk_engine.py REGIME_NOTICE_DAYS_DEFAULT = 4` is a
illustrative default, not a researched value for any specific market's
actual buy-in notice period (CSDR in the EU, Reg SHO close-out
requirements in the US, and other local market rules all differ
materially). Must be reviewed and likely parameterized per security
domicile before production use of the buy-in risk score for anything
beyond internal prioritization.

**OPS-6 — "Last known good" data caching is not yet implemented.**
When a data source is unavailable, the orchestration layer raises
`DataSourceUnavailableError` and the cycle (or that cycle's dependent
stage) is skipped — it does not yet fall back to a cached "last known
good" snapshot with an elevated staleness penalty. See
`docs/runbook.md` "A data source is down" for the current behavior per
source, and treat this as a near-term hardening item.

## Architecture

**ARCH-1 — `api/state.py` is an in-memory singleton, not production storage.**
Explicitly documented as a reference-build simplification in that
module's docstring. Production reads from `sql/views/v_book_summary.sql`
and the persisted `secfi.recommendation`/`secfi.alert` tables, typically
fronted by a short-TTL cache.

**ARCH-2 — No authentication/authorization implemented.**
Out of scope for this reference architecture by design — the firm's
existing SSO/API gateway should front this service. See
`docs/governance.md` "Permissions / access control" for the intended role
model and the specific integration point
(`Depends(get_current_user)` in `api/main.py`).

**ARCH-3 — Sandbox environment had no outbound network access.**
This entire repository was built and tested in an environment without
internet access. Consequences, stated plainly:
- All Python dependencies used for executable code (`numpy`, `pandas`,
  `scipy`, `pyyaml`) were pre-installed in the sandbox and are real,
  tested dependencies.
- `pydantic`, `fastapi`, `uvicorn`, `pulp` could not be installed in this
  sandbox (no network access for `pip install`). Every file using them
  (`api/*.py`) is syntax-validated via `python -m py_compile` but not
  import-or-execution-tested in this environment. They are standard,
  widely-used, stable libraries; install and run `pytest tests/api/`
  (to be added) on a connected CI runner before merging the API layer.
- The optimization engine uses `scipy.optimize.linprog` (HiGHS) rather
  than `PuLP`/commercial MILP solvers for the same reason — it was
  available in-sandbox and is genuinely adequate for the LP relaxation
  this build solves; see `docs/algorithms.md` section 1 for the
  documented MILP upgrade path when discreteness becomes material.

## What was deliberately built thin (not a gap, a scoping decision)

These are NOT bugs or oversights — they are reference-architecture scope
boundaries documented so a reviewer doesn't mistake "not built" for
"forgotten":

- **No frontend UI.** The API layer is the deliverable; a real dashboard
  (React/whatever the firm's internal frontend stack is) consumes it. See
  `docs/reporting_design.md` for exactly what each endpoint returns.
- **No live message bus/event streaming.** Ingestion connectors are
  poll/fetch-based (`SourceConnector.fetch()`), not subscription-based.
  Production internal trade capture should be event-streamed (Kafka or
  equivalent) for true intraday latency — the connector interface
  (`ingestion/base.py`) is designed to accommodate either pattern, but
  only the poll-based reference implementation is built here.
- **No automated execution integration.** By design — see
  `docs/governance.md` "Core principle." This is not a missing feature,
  it is the control boundary.
- **No multi-tenancy / multi-desk isolation.** This build assumes a
  single desk's book. A firm running multiple securities finance desks
  on one platform instance would need to add a `desk_id` partition key
  to queries and limits (the `Position.desk_id` field already exists in
  the domain model for this purpose — it is captured but not yet used to
  partition any query).
