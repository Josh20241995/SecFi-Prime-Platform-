# Testing & Validation Framework

## Current test suite status (verified, not aspirational)

```
$ PYTHONPATH=src:. python3 -m unittest discover -s tests -p "test_*.py"
Ran 97 tests in 0.319s
OK
```

97 tests, 0 failures, 0 errors, 0 skips, across 11 unit test modules + 1
integration test module. Every number quoted anywhere in this
documentation set (e.g., "GME tops the buy-in queue," "the LP solver
respects a tight counterparty limit") is independently verified by a
passing test against the bundled realistic fixture dataset
(`tests/fixtures/`), not asserted from confidence.

Run it yourself:
```bash
cd secfi-prime-platform
PYTHONPATH=src:. python3 -m unittest discover -s tests -p "test_*.py" -v
# or, once pytest is installed (pip install -e ".[dev]"):
pytest tests/ -v --cov=secfi_platform
```

## Test pyramid

| Level | Location | Count | What it proves |
|---|---|---|---|
| Unit | `tests/unit/*.py` | 83 | Each engine's pure functions behave correctly in isolation against constructed/fixture inputs |
| Integration | `tests/integration/test_full_cycle.py` | 14 | Every engine wires together correctly through `orchestration.scheduler.run_full_cycle` — the actual production call path |

### Unit test coverage by module

| Module | Test file | Key things proven |
|---|---|---|
| `risk/counterparty_risk.py` | `test_counterparty_risk.py` | Exposure non-negative; uncollateralized positions correctly bucketed; limit breach/headroom math; WWR flag fires for a genuinely concentrated bank-dealer; all 6 stress scenarios present; equity-down shock moves signed exposure in the documented direction |
| `risk/capital_rwa.py` | `test_capital_rwa.py` | Higher-risk counterparty -> higher risk weight -> higher RWA for identical exposure; EAD non-negative; netting-eligible counterparty gets leverage relief; rebate-type position reports negative revenue without a configured reinvestment spread (by design, not a bug); capital cost scales with target CET1 ratio |
| `risk/rates_fx.py` | `test_rates_fx.py` | DV01 only includes cash-rate-sensitive legs; REPO vs. REVERSE_REPO sign convention; FX exposure excludes base currency and is empty for an all-USD book; funding gap buckets are internally consistent; hedge recommendations respect materiality thresholds |
| `pricing/pricing_intelligence.py` | `test_pricing_intelligence.py` | Specialness classification (GC/specials-in-waiting/deep-special) against real fixture rates; z-score present when a tier has multiple members; missing quote flagged not dropped; GME generates a large, positive-P&L reprice recommendation; **regression test that REVERSE_REPO is not misclassified as the "paying" side** (this caught a real bug during development — see `docs/algorithms.md` section 2) |
| `optimization/book_optimizer.py` | `test_book_optimizer.py` | Solver reaches `OPTIMAL` on the real fixture book; empty candidate list handled gracefully; allocations never exceed a position's market value; a tight counterparty limit genuinely constrains routed balance; noise threshold suppresses sub-threshold repricing recommendations; every recommendation carries rationale/confidence/priority |
| `recall_buyin/recall_risk_engine.py` | `test_recall_risk_engine.py` | Fail-to-deliver on a deep special tops the queue; fail-to-receive scores lower buy-in risk than fail-to-deliver; substitute availability genuinely reduces buy-in score (using a moderate-severity synthetic fail to avoid the 100-point cap masking the effect — **a real saturation edge case caught during test development**); CA-driven return increases urgency; only actionable rows generate recommendations; queue sorted descending |
| `reconciliation/recon_engine.py` | `test_recon_engine.py` | Self-reconciliation produces zero breaks (sanity check); all four deliberately-injected break types in the fixture (quantity mismatch, price mismatch, missing-at-custodian, missing-on-book) are correctly detected and classified; breaks sorted by severity; missing required column raises `ValueError`; tolerance is configurable |
| `corporate_actions/ca_impact_engine.py` | `test_ca_impact_engine.py` | All events within the 60-day window appear on the watchlist; a 2-day-out reverse split is IMMEDIATE/ACT_TODAY; near-term high-impact event outranks a far-out lower-impact one; window filter genuinely excludes out-of-horizon events; affected positions correctly linked by security; watchlist sorted descending; informational events generate zero recommendations |
| `growth/counterparty_growth.py` | `test_growth_engine.py` | Watch-list counterparty always recommended REDUCE; unresolved CRITICAL breaks force REDUCE even when economics look good; every action is a valid enum value; rationale always present; priority score bounded; custom thresholds genuinely change the outcome |
| `explainability/explain.py` | `test_explainability.py` | Staleness penalty boundary behavior (0 age = 1.0, half-life = 0.5, floored at 0.10); confidence bounded [0,1] under adversarial inputs (certainty=5.0, negative age); rationale builder dedupes and strips empties; priority score increases with P&L, is damped by low confidence, stays bounded; custom weights respected |
| `normalization/schema_mapping.py` | `test_normalization.py` | Valid row round-trips correctly; missing required field raises `DataQualityError`; invalid enum value raises; `parse_rows` isolates a bad row without failing the whole batch; all 8 real fixture securities parse cleanly |

### Integration test coverage (`test_full_cycle.py`)

Runs `run_full_cycle()` against the full realistic fixture book (12
positions, 5 counterparties, 8 securities, 5 corporate action events, 3
settlement fails, 4 deliberately-injected reconciliation breaks) and
proves:
- Every counterparty has both an exposure AND a capital summary (no stage
  silently drops a counterparty)
- Reconciliation breaks are detected (not just "the function ran")
- Pricing recommendations are generated
- The optimizer reaches `OPTIMAL`
- GME (the deep-special, aged-fail, no-substitute name) is correctly the
  #1 item in the buy-in/recall queue
- All 5 corporate action events appear on the watchlist
- All 5 counterparties get a growth/contraction opportunity assessment
- The watch-list counterparty (CPTY004) is specifically flagged REDUCE
- Alerts fire for both recall/buy-in and reconciliation categories
- The executive summary's `open_critical_recon_breaks` field matches an
  independent recount from the raw breaks list (no double-aggregation bug)
- The executive summary renders to Markdown without error
- **Every** recommendation across **every** engine carries a non-empty
  rationale, confidence in [0,1], completeness in [0,1], and starts
  `PROPOSED` (never pre-approved)
- **The cycle is deterministic**: running it twice against identical
  inputs produces identical book NMV, break count, and solver status —
  critical for the replay/reproducibility guarantee in
  `docs/governance.md`

## What's deliberately NOT tested in this reference build (and why)

| Gap | Reason | Mitigation before production |
|---|---|---|
| `api/` layer (FastAPI routes, pydantic schemas) | This sandbox has no outbound network access to install `fastapi`/`pydantic`/`uvicorn`; every `api/` file is `python -m py_compile` syntax-checked but not import-or-execution-tested here | CI (`.github/workflows/ci.yml`) runs on a connected runner with these installed — add `tests/api/` with FastAPI's `TestClient` before merging the API layer to `main` |
| Live vendor connector behavior (real EquiLend/DataLend/custodian calls) | `ingestion/connectors.py` implementations in this build are file-based by design (no live credentials in this environment) | Phase 0 of `docs/implementation_plan.md` — contract/integration tests against a vendor sandbox environment before Phase 1 pilot |
| SQL schema execution against a real Postgres instance | No database available in this sandbox | `infra/docker/docker-compose.yml` provisions Postgres + auto-runs `sql/schemas/` on container init; run `docker compose up` and verify before Phase 0 sign-off |
| Load/performance testing at full desk scale (thousands of positions) | Fixture book is 12 positions by design (readable, hand-verifiable) | `docs/algorithms.md` documents the expected scaling behavior (HiGHS LP, pandas joins) — synthetic large-N load test recommended before Phase 3 |

## Backtesting (model risk requirement, see `docs/model_risk.md`)

Once Phase 2 approval-and-execution data exists: for every APPROVED and
EXECUTED recommendation, compare `estimated_pnl_impact_usd` against
realized P&L over the following period. Track this by `source_engine` so
systematic over/under-estimation in one engine (e.g., optimization vs.
pricing) is visible independently. This requires production execution
data this reference build does not have access to — see
`docs/assumptions_and_limitations.md`.

## Sensitivity / scenario tests

Already exercised by unit tests for the risk engines (6 standard
scenarios, `test_stress_scenarios_present_for_all_standard_scenarios`,
`test_equity_down_shock_increases_lend_exposure`). For a production
hardening pass, add parameterized sweep tests across the full
`configs/risk_limits.yaml` `stress_scenarios` range plus desk-specific
ad-hoc scenarios (e.g., "what if our top 3 specials all get recalled
simultaneously") as named integration tests.

## False-positive / false-negative review

Built into the design, not bolted on after: every threshold that drives
an alert or a REDUCE/RETURN/RECALL recommendation is a named constant in
`configs/base.yaml` or a dataclass default (`AlertThresholds`,
`GrowthThresholds`, `SpecialnessThresholds`, `ReconConfig`), not a magic
number buried in logic — a desk head reviewing false-positive complaints
can locate and adjust the exact threshold without a code change, and
every threshold change is itself auditable via standard config-file
version control.

## Exception handling

- `normalization/schema_mapping.py` `parse_rows()` isolates bad rows —
  proven by `test_parse_rows_isolates_bad_rows_without_failing_batch`
- `reconciliation/recon_engine.py` raises `ValueError` on missing
  required columns rather than producing silently wrong output — proven
  by `test_missing_required_column_raises`
- `ingestion/base.py` `DataSourceUnavailableError` is the canonical
  failure mode for every connector, caught explicitly in
  `scripts/run_daily_batch.py main()` rather than an unhandled crash

## Governance approval workflow for model changes

See `docs/model_risk.md` for the full checklist. Summary: any change to
`risk/`, `optimization/`, `pricing/`, or `recall_buyin/` requires the
`MODEL_RISK_CHECKLIST_ACKNOWLEDGED` marker in the PR description before
CI allows merge to `main` (`.github/workflows/ci.yml`
`model-risk-checklist-gate` job).
