# SecFi Prime Platform

**Institutional Securities Lending, Financing, Repo & Prime Brokerage Decision-Support Engine**

SecFi Prime Platform is an end-to-end decision-support and optimization engine for a global securities lending, financing, repo, and prime brokerage desk. Built to institutional standards, it combines ten integrated analytical engines — covering counterparty exposure, capital and RWA approximation, book optimization, pricing intelligence, recall and buy-in risk, corporate action impact, custodian reconciliation, collateral optimization, inventory management, and growth analysis — into a single, auditable, human-in-the-loop platform. Built for the desk head and their full team — traders, risk managers, funding strategists, operations, and senior management — with a governance framework, model risk documentation, and operational runbook suitable for a top-tier prime brokerage environment.

---

## Who This Is For

This platform is built for the **head of a global securities lending and financing desk** inside a prime brokerage business at a top-tier bank — and every member of their team:

| Role | What they use it for |
|---|---|
| Desk head | Morning P&L opportunity briefing, capital usage, counterparty risk heatmap |
| Trader | Repricing queue, recall/buy-in urgency queue, intraday limit checks |
| Risk manager | Stress exposure, wrong-way risk flags, limit utilization dashboard |
| Funding/treasury strategist | DV01, FX exposure, funding gap, hedge recommendations |
| Operations | Reconciliation break dashboard, settlement fail aging, exception queue |
| Technology | Health checks, observability, CI/CD pipeline |
| Senior management | Daily executive summary, capital usage, overall desk risk posture |

---

## What It Does

Ten integrated analytical engines, wired through a single orchestration layer:

| # | Capability | Engine |
|---|---|---|
| 1 | Counterparty exposure & balance-sheet risk | `risk/counterparty_risk.py` |
| 2 | Capital, RWA, leverage, return on capital | `risk/capital_rwa.py` |
| 3 | Interest rate risk (DV01) & FX exposure | `risk/rates_fx.py` |
| 4 | Market pricing intelligence & repricing queue | `pricing/pricing_intelligence.py` |
| 5 | Book optimization (LP allocation engine) | `optimization/book_optimizer.py` |
| 6 | Recall / return / buy-in urgency ranking | `recall_buyin/recall_risk_engine.py` |
| 7 | Corporate action impact (60-day forward scan) | `corporate_actions/ca_impact_engine.py` |
| 8 | Custodian & balance-sheet reconciliation | `reconciliation/recon_engine.py` |
| 9 | Counterparty growth / contraction analysis | `growth/counterparty_growth.py` |
| 10 | Collateral optimization & inventory management | `risk/collateral_optimizer.py`, `risk/inventory_manager.py` |

**Plus the control layer:**
- Intraday limit monitoring with what-if simulation (`risk/limit_monitor.py`)
- Standalone scenario & reverse-stress engine (`risk/scenario_engine.py`)
- Pricing, limit, and data-quality exception management with approval workflow (`risk/exception_manager.py`)
- Cross-source data quality controls & anomaly detection (`common/data_quality.py`)
- Intelligent alert prioritization, deduplication & routing (`alerting/prioritizer.py`)
- Realized P&L backtesting framework (`reporting/backtester.py`)

**The platform does not trade, book, or execute anything.** Every recommendation requires explicit human approval via the desk-facing API before it is acted on. See [Governance](#governance).

---

## Architecture

```
External sources  ──►  Ingestion  ──►  Normalization  ──►  Analytics engines
                                                               │
                                                    ┌──────────┼──────────┐
                                                    │          │          │
                                                  Risk      Price     Optimize
                                                  layer    layer      layer
                                                    │          │          │
                                                    └──────────┼──────────┘
                                                               │
                                                         Orchestration
                                                               │
                                              ┌────────────────┼────────────────┐
                                              │                │                │
                                          Alerting         Reporting          API
                                              │                │                │
                                           Alerts       Daily summary     Desk UI
                                              │           + queues         /v1/...
                                              │
                                          SQL persistence
                                       (secfi.* schema, PostgreSQL)
```

Full architecture diagram and layer-by-layer description: **[docs/architecture.md](docs/architecture.md)**

### Technology choices

| Layer | Technology | Rationale |
|---|---|---|
| Analytics core | Python 3.11 | Hot-path recomputation every cycle; `dataclasses` over pydantic for internal objects |
| Book optimization | scipy HiGHS LP | Open-source LP solver, no license dependency; MILP upgrade path documented |
| Reconciliation joins | pandas | Set-based matching at institutional row counts is pandas's sweet spot |
| API | FastAPI + uvicorn | Auto-documented, async-capable, standard for new internal bank services |
| Configuration | YAML + `${ENV_VAR}` secret refs | Versioned, environment-layered, secrets externalized |
| Logging | Structured JSON + correlation IDs | Full cycle reconstructable from logs alone |
| Persistence | PostgreSQL 14+ | Standard; SQL schemas portable to Sybase/SQL Server if needed |
| Containerization | Docker multi-stage, non-root | Matches bank container security baselines |
| CI/CD | GitHub Actions | lint → unit tests → integration tests → container build |

---

## Repository Structure

```
secfi-prime-platform/
├── src/secfi_platform/
│   ├── common/
│   │   ├── config.py              # YAML config loader with env-var secret resolution
│   │   ├── data_quality.py        # Cross-source DQ profiling, anomaly detection
│   │   ├── enums.py               # Controlled vocabularies (Direction, ProductType, etc.)
│   │   ├── logging_setup.py       # Structured JSON logging with correlation IDs
│   │   └── types.py               # Canonical dataclasses (Position, Counterparty, etc.)
│   ├── ingestion/
│   │   ├── base.py                # SourceConnector ABC + DataSourceUnavailableError
│   │   └── connectors.py          # One connector per source (file-backed for dev)
│   ├── normalization/
│   │   └── schema_mapping.py      # raw dict → canonical objects, with per-field validation
│   ├── risk/
│   │   ├── capital_rwa.py         # EAD / RWA / leverage / RoC approximation
│   │   ├── collateral_optimizer.py# CTD scoring, eligibility, substitution recommendations
│   │   ├── counterparty_risk.py   # Gross/net exposure, stress scenarios, WWR, HHI
│   │   ├── exception_manager.py   # Pricing/limit/DQ exception lifecycle
│   │   ├── inventory_manager.py   # Lendable pool, locate resolution, rehypothecation
│   │   ├── limit_monitor.py       # Intraday utilization dashboard, what-if simulation
│   │   ├── rates_fx.py            # DV01, FX exposure, funding gap, hedge recommendations
│   │   └── scenario_engine.py     # Named event scenarios, comparison matrix, reverse stress
│   ├── optimization/
│   │   └── book_optimizer.py      # Constrained LP: reprice + reroute, capital-aware objective
│   ├── pricing/
│   │   └── pricing_intelligence.py# Specialness classification, dispersion z-score, REPRICE queue
│   ├── recall_buyin/
│   │   └── recall_risk_engine.py  # Buy-in risk score, urgency score, substitute resolution
│   ├── reconciliation/
│   │   └── recon_engine.py        # Pandas-based book↔custodian matching, break classification
│   ├── corporate_actions/
│   │   └── ca_impact_engine.py    # 60-day forward scan, event-type weights, proximity scaling
│   ├── growth/
│   │   └── counterparty_growth.py # GROW/HOLD/REDUCE/REPRICE decision tree
│   ├── explainability/
│   │   └── explain.py             # Shared confidence/priority scoring used by every engine
│   ├── alerting/
│   │   ├── alert_engine.py        # Threshold → Alert objects from each engine's output
│   │   └── prioritizer.py         # Deduplication, throttle, mass-collapse, rank, route
│   ├── reporting/
│   │   ├── backtester.py          # Predicted vs realized P&L calibration framework
│   │   └── daily_summary.py       # Assemble all engine outputs into executive summary + MD
│   ├── orchestration/
│   │   └── scheduler.py           # run_full_cycle() — the DAG scheduler's task body
│   └── api/
│       ├── main.py                # FastAPI app entry point
│       ├── schemas.py             # Pydantic DTOs (boundary only, not internal model)
│       ├── state.py               # Latest cycle output store (in-memory ref build)
│       └── routers/
│           ├── desk.py            # Core desk endpoints (exposure, recs, queues, approvals)
│           └── risk_extended.py   # Limits, scenarios, DQ, capital, rates/FX, alerts
├── sql/
│   ├── schemas/                   # 01-08: reference data, counterparty, book, pricing, recon, CA, capital, audit
│   ├── views/                     # v_book_summary, v_counterparty_exposure_rollup
│   └── procedures/                # sp_age_open_items (nightly aging)
├── tests/
│   ├── fixtures/                  # 8 realistic CSV fixtures (securities, positions, rates, fails, CA, FX...)
│   ├── _helpers.py                # Shared fixture loading → canonical objects
│   ├── unit/                      # 11 unit test modules (121 tests)
│   └── integration/               # 1 full-cycle integration test module (14 tests)
├── scripts/
│   ├── run_daily_batch.py         # EOD full cycle entry point
│   └── run_intraday_cycle.py      # Intraday fast cycle (no recon)
├── examples/
│   └── example_daily_run.py       # Ad-hoc engine usage (notebook-style)
├── configs/
│   ├── base.yaml                  # Environment-agnostic defaults
│   ├── dev.yaml                   # Dev overrides
│   ├── prod.yaml                  # Prod overrides (secrets as ${ENV_VAR_NAME} refs)
│   └── risk_limits.yaml           # Haircuts, stress scenarios, counterparty tier limits
├── infra/
│   └── docker/
│       ├── Dockerfile             # Multi-stage prod image (non-root runtime)
│       └── docker-compose.yml     # Local dev stack: API + PostgreSQL
├── docs/
│   ├── architecture.md            # Full system architecture + technology choices
│   ├── algorithms.md              # Mathematical formulations for every engine
│   ├── data_model.md              # Data schema, required inputs, fallback policy
│   ├── module_specs.md            # Module-by-module API contract reference
│   ├── risk_framework.md          # Risk governance, what this platform IS and IS NOT
│   ├── reporting_design.md        # Output/dashboard specifications
│   ├── governance.md              # Approval workflow, permissions, escalation
│   ├── model_risk.md              # Model inventory, change governance checklist, backtesting plan
│   ├── runbook.md                 # Scheduling, monitoring, incident response
│   ├── testing_validation.md      # Full test pyramid, coverage, known gaps
│   ├── implementation_plan.md     # 4-phase rollout plan, deployment topology
│   ├── assumptions_and_limitations.md  # Every assumption with a stable reference code
│   └── sample_outputs/
│       └── daily_executive_summary_2026-06-18.md  # Real output from a full cycle run
├── .github/workflows/ci.yml       # lint → unit → integration → container build → model-risk gate
├── .env.example                   # Required environment variable documentation
├── .gitignore
├── pyproject.toml                 # Dependencies, tool config
└── README.md                      # This file
```

**Total: 135 tests, 0 failures, across 55+ Python modules.**

---

## Data Sources

| Source | What it provides | Connector | Cadence |
|---|---|---|---|
| **Internal trade capture** | Live book positions, lifecycle events | `InternalTradeCaptureConnector` | Real-time/intraday (event stream in prod) |
| **EquiLend** | Market composite rates, utilization | `EquiLendConnector` | Intraday (REST API in prod) |
| **DataLend** | Second-source market rates for cross-validation | `DataLendConnector` | Intraday |
| **Custodian feed** | Independent position confirmation | `CustodianFeedConnector` | T+1 EOD |
| **Market data platform** | Prices, FX rates, yield curves | `MarketDataConnector` | Intraday |
| **Corporate actions feed** | 60+ day forward event calendar | `CorporateActionsFeedConnector` | Daily pre-market |
| **Settlement system** | Fails, aging, settlement status | `SettlementFailsConnector` | Intraday |
| **Firm balance sheet** | EOD balance-sheet positions | `BalanceSheetFeedConnector` | EOD |

All connectors implement `SourceConnector` (`ingestion/base.py`). The reference build uses local CSV fixtures (`tests/fixtures/`). Production: swap each connector class's `fetch()` implementation to call the real vendor API or internal message bus — no analytics code changes.

---

## Configuration

```yaml
# configs/base.yaml — key configurable thresholds
pricing:
  specialness_thresholds:
    gc_fee_ceiling_bps: 25         # <25bps = GC
    special_fee_floor_bps: 100     # ≥100bps = Special
    htb_fee_floor_bps: 300         # ≥300bps + high util = HTB
    deep_special_fee_floor_bps: 1000
  min_actionable_gap_bps: 5        # suppress noise repricings below this

capital:
  target_cet1_ratio: 0.115          # 11.5% CET1 target
  cost_of_capital_pct: 0.12         # 12% hurdle rate

alerting:
  thresholds:
    limit_utilization_warn_pct: 0.85    # AMBER threshold
    limit_utilization_breach_pct: 1.00  # BREACH threshold
    buyin_risk_alert_score: 70           # 0-100 scale
```

All thresholds are desk-configurable **without code changes** — modify `configs/base.yaml` (or `configs/prod.yaml` for environment-specific overrides) and restart the service. Config changes are version-controlled and auditable.

Secrets are never in YAML — only `${ENV_VAR_NAME}` references resolved at runtime. See `.env.example`.

---

## Installation

### Prerequisites
- Python 3.11+
- Docker & Docker Compose (for local stack)
- PostgreSQL 14+ (for full persistent deployment)

### Local development

```bash
# 1. Clone and set up
git clone <internal-repo-url> secfi-prime-platform
cd secfi-prime-platform

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Copy and configure environment
cp .env.example .env
# Edit .env: set SECFI_DB_* values for local Postgres if needed

# 4. Run the full test suite
PYTHONPATH=src:. python3 -m unittest discover -s tests -p "test_*.py"
# Expected: Ran 135 tests in ~0.5s, OK

# 5. Run a cycle against the bundled reference dataset
python3 scripts/run_daily_batch.py --as-of 2026-06-18 --environment dev
# Output: structured JSON logs + daily_executive_summary_2026-06-18.md in outputs/

# 6. Start the API service
uvicorn secfi_platform.api.main:app --reload --port 8000
# Health check: curl http://localhost:8000/healthz

# 7. Optional: local stack with Postgres (applies SQL schemas automatically)
docker compose -f infra/docker/docker-compose.yml up
```

### First-time Postgres setup

```bash
# Apply all schemas in order (docker-compose does this automatically on first run)
psql -h localhost -U secfi_dev -d secfi_platform_dev \
    -f sql/schemas/01_reference_data.sql \
    -f sql/schemas/02_counterparty.sql \
    -f sql/schemas/03_positions_book.sql \
    -f sql/schemas/04_pricing_market_data.sql \
    -f sql/schemas/05_recon.sql \
    -f sql/schemas/06_corporate_actions.sql \
    -f sql/schemas/07_capital_rwa.sql \
    -f sql/schemas/08_audit_log.sql

# Apply views and procedures
psql -h localhost -U secfi_dev -d secfi_platform_dev \
    -f sql/views/v_book_summary.sql \
    -f sql/views/v_counterparty_exposure_rollup.sql \
    -f sql/procedures/sp_age_open_items.sql
```

---

## Running the Platform

### Daily EOD cycle (full — includes reconciliation)

```bash
python3 scripts/run_daily_batch.py \
    --as-of $(date +%Y-%m-%d) \
    --environment prod \
    --output-dir /data/secfi/outputs
```

### Intraday fast cycle (every 15 min — no reconciliation)

```bash
python3 scripts/run_intraday_cycle.py --as-of $(date +%Y-%m-%d)
```

### API service

```bash
# Development
uvicorn secfi_platform.api.main:app --reload --port 8000

# Production (via Docker)
docker build -f infra/docker/Dockerfile -t secfi-prime-platform:$(git rev-parse --short HEAD) .
docker run -p 8000:8000 --env-file .env secfi-prime-platform:<tag>
```

### Ad-hoc engine queries (notebook/interactive style)

```bash
python3 examples/example_daily_run.py
```

---

## API Reference

Base URL: `http://<host>:8000/v1`  
Auto-generated OpenAPI docs: `http://<host>:8000/docs`

### Core desk endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/risk/counterparty` | All counterparty exposures (gross, net, stress, WWR, HHI) |
| GET | `/risk/counterparty/{id}` | Single counterparty drill-down |
| GET | `/risk/rates-fx` | DV01 by tenor bucket, FX exposure, hedge recommendations |
| GET | `/capital/usage` | RWA, leverage exposure, RoC, RoB by counterparty |
| GET | `/optimization/recommendations` | Book optimization REPRICE/REROUTE queue |
| GET | `/pricing/recommendations` | Specialness-ranked pricing opportunity queue |
| GET | `/growth/recommendations` | GROW/HOLD/REDUCE/REPRICE counterparty assessments |
| GET | `/recall-buyin/queue` | Buy-in risk + urgency ranked queue |
| GET | `/corporate-actions/watchlist` | 60-day CA impact ranked list |
| GET | `/reconciliation/breaks` | Break dashboard with optional severity filter |
| GET | `/reports/daily-summary` | Executive summary (machine-readable) |
| POST | `/recommendations/approval-decision` | **Approve or reject** a recommendation (audit-logged, human only) |

### Risk-extended endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/limits/dashboard` | Intraday GREEN/AMBER/RED/BREACH utilization |
| GET | `/limits/simulate-incremental` | What-if: add $X to counterparty Y |
| GET | `/scenarios/standard` | All standard scenarios, comparison matrix |
| GET | `/alerts/feed` | Prioritized, deduplicated, routed alert feed |
| GET | `/data-quality/report` | Source quality profiles, anomalies, coverage |

### Health

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Service health, environment, config sources |

**All POST endpoints require a `decided_by` field and write an immutable audit record to `secfi.recommendation_approval_log`. Nothing auto-executes.**

---

## Module Descriptions

### Book Optimization Engine (`optimization/book_optimizer.py`)

Formulates the desk's reallocation problem as a **constrained linear program** (scipy HiGHS). Maximizes net economics (fee revenue minus capital cost) subject to:
- Counterparty exposure limits
- Issuer concentration caps (max 15% of book NMV per issuer)
- Desk RWA budget (optional)
- Position-level move caps (risk control: max 100% of position per cycle)

Capital cost enters the objective directly via `risk/capital_rwa.py` — the optimizer is capital-aware by construction, not by post-hoc adjustment.

Outputs: REPRICE (same counterparty, market rate) and REROUTE (alternative counterparty) recommendations, ranked by P&L impact.

### Pricing Intelligence (`pricing/pricing_intelligence.py`)

Classifies every security as GC → Warm → Specials-in-Waiting → Special → HTB → Deep Special based on market composite rate and utilization (EquiLend/DataLend). Computes a z-score of the desk's rate vs. market *within* the security's specialness tier — so a 5bp gap on a GC name scores very differently from a 5bp gap on a 4,000bp deep special.

**Specials-in-Waiting** (utilization >85%, fee not yet repriced) is the highest-value early detection signal — this is where the desk captures the spread *before* the market catches up.

### Recall / Buy-In Engine (`recall_buyin/recall_risk_engine.py`)

Produces two independent scores per open settlement fail:
- **Urgency (0-100)**: how soon someone needs to act
- **Buy-in risk (0-100)**: likelihood this specific fail escalates to a forced buy-in

Buy-in risk uses a multiplicative model: base score (fail age vs. regime notice days) × specialness multiplier (1.5× for HTB, 2.2× for Deep Special) × substitute penalty (1.25× if no substitute inventory). Correctly distinguishes fail-to-deliver (the desk owes securities, the actually buy-in-able direction) from fail-to-receive.

### Collateral Optimizer (`risk/collateral_optimizer.py`)

Scores every rehypothecatable position by cheapest-to-deliver rank (equities → agency → gov → cash, weighted by opportunity cost and haircut burden). Identifies collateral substitution opportunities where a lower-cost eligible substitute exists in inventory, saving the annual opportunity cost of posting expensive HQLA when equities would satisfy the counterparty's schedule.

### Limit Monitor (`risk/limit_monitor.py`)

Intraday GREEN/AMBER/RED/BREACH dashboard. Includes `simulate_incremental_exposure()` — used by the optimization engine before proposing a REROUTE and exposed via the API for ad-hoc pre-trade checks. Uses gross MV (conservative, no haircut assumptions needed) rather than net exposure for monitoring, so the limit is never overstated as "safe" due to collateral assumptions being wrong intraday.

### Scenario Engine (`risk/scenario_engine.py`)

Four standard named scenarios (equity crash, repo rate spike, FX devaluation, combined regulatory stress) plus user-defined event scenarios. Reverse-stress analysis: find the minimum shock magnitude that breaches a configurable P&L loss threshold. All scenarios produce a `ScenarioResult` with consistent shape — the same fields regardless of scenario type.

### Exception Management (`risk/exception_manager.py`)

Lifecycle-tracked pricing, limit, and data-quality exceptions: PROPOSED → APPROVED (ACTIVE) → CLOSED. Every exception requires a named approver (`decided_by`). Expired exceptions are detected automatically. Designed as an auditable override mechanism — exceptions are documented, not hidden.

### Reconciliation Engine (`reconciliation/recon_engine.py`)

Pandas-based matching on `(security_internal_id, counterparty_id, trade_date, direction)`. Detects and classifies: quantity mismatch, price/rate mismatch, missing-at-custodian, missing-on-book, duplicate entries. Flags buy-in-risk-relevant and capital-misstatement-relevant breaks separately. Self-reconciliation produces zero breaks (regression-tested).

---

## Testing

```bash
# Full test suite
PYTHONPATH=src:. python3 -m unittest discover -s tests -p "test_*.py"
# Expected: Ran 135 tests in ~0.5s, OK

# Unit tests only
PYTHONPATH=src:. python3 -m unittest discover -s tests/unit -p "test_*.py"

# Integration test only (full cycle, all engines wired together)
PYTHONPATH=src:. python3 -m unittest tests.integration.test_full_cycle -v

# With coverage (requires pytest, pip install -e ".[dev]")
pytest tests/ --cov=secfi_platform --cov-report=term-missing
```

### Test coverage by module (135 tests total)

| Module group | Tests | Key behaviors proven |
|---|---|---|
| Counterparty risk | 8 | WWR flags, stress scenarios signed correctly, HHI bounds |
| Capital/RWA | 6 | Risk-weight ordering, netting relief, rebate revenue sign |
| Rates/FX | 7 | DV01 sign convention REPO vs REVERSE_REPO, empty FX book |
| Pricing intelligence | 9 | Specialness classification, REVERSE_REPO economic-side regression |
| Book optimizer | 7 | LP feasibility, limit constraints, noise threshold |
| Recall/buy-in | 6 | Queue ordering, buy-in score saturation edge case |
| Reconciliation | 9 | All 4 break types detected, self-recon = 0 breaks |
| Corporate actions | 7 | Window filter, urgency classification, position linking |
| Growth engine | 6 | Watch-list forces REDUCE, threshold sensitivity |
| Explainability | 11 | Confidence bounds, staleness decay, priority damping |
| Normalization | 6 | Isolation of bad rows, missing fields |
| New capability modules | 38 | Collateral CTD, locate resolution, limit simulation, scenarios, exceptions, DQ, backtest, alert prioritization |
| Full integration | 14 | All engines wired, determinism, every recommendation PROPOSED |

Full validation plan: **[docs/testing_validation.md](docs/testing_validation.md)**

---

## Governance

> **This platform is advisory and analytical only. It does not trade, book, or execute anything without explicit human approval.**

```
Recommendation generated (approval_status = PROPOSED)
         │
         ▼
Trader / risk manager reviews via dashboard or GET /v1/...
         │
         ▼
POST /v1/recommendations/approval-decision
     { decision: "APPROVE" | "REJECT", decided_by: "trader_id", comment: "..." }
         │
         ▼
Immutable audit record → secfi.recommendation_approval_log
         │  (if APPROVED)
         ▼
Human trader executes in the FIRM'S EXISTING trade booking system
```

**No recommendation auto-approves. No endpoint auto-executes.**

Model change governance checklist (required before merging any change to a risk/optimization/pricing module): see **[docs/model_risk.md](docs/model_risk.md)**.

CI gate: any PR touching `risk/`, `optimization/`, `pricing/`, or `recall_buyin/` must contain `MODEL_RISK_CHECKLIST_ACKNOWLEDGED` in the PR description or the CI pipeline blocks merge.

---

## Permissions

See **[docs/governance.md](docs/governance.md)** for the full role matrix. Summary:

- **Desk head, traders**: full read access + approval-decision endpoint
- **Risk managers**: full read access, no approval authority (by this build's design — adjust per the firm's delegation-of-authority policy)
- **Operations**: reconciliation, settlement fails, recall queue
- **Technology**: health checks, observability
- **Senior management**: executive summary

Authentication/authorization is intentionally not implemented in this reference build — the firm's existing SSO/OAuth2 provider and API gateway should front this service. See **[docs/assumptions_and_limitations.md](docs/assumptions_and_limitations.md)** item ARCH-2.

---

## Limitations & Known Failure Modes

| Item | Detail | Mitigation |
|---|---|---|
| **Capital is desk-decision-support only** | `risk/capital_rwa.py` is an approximation modeled on SA-CCR shape, NOT the firm's certified regulatory capital engine | See module docstring; Treasury/Capital Management must confirm risk weights before production use |
| In-memory API state | `api/state.py` uses a thread-unsafe in-memory store | Wire to PostgreSQL reads backed by short-TTL cache before production — seam is isolated to one file |
| No authentication | JWT/SSO not implemented | Firm's API gateway + `Depends(get_current_user)` injection point in `api/main.py` |
| Reconciliation matching key simplified | Matches on `(security, counterparty, trade_date, direction)` only | Add SSI/trade-reference-number key for production — see OPS-2 in assumptions doc |
| Buy-in regime notice days = 4 (placeholder) | Must be reviewed per market/regulatory regime (CSDR, Reg SHO, etc.) | Configure per security domicile in `configs/risk_limits.yaml` |
| No "last known good" cache | Stale source = cycle skipped for that stage, not degraded | Phase 0 hardening item per implementation plan |
| No live vendor connectors | EquiLend/DataLend connectors read CSV in dev | Replace `fetch()` implementations in `ingestion/connectors.py` |

Full list: **[docs/assumptions_and_limitations.md](docs/assumptions_and_limitations.md)**

---

## Escalation Paths

| Condition | Escalation path |
|---|---|
| Counterparty limit BREACH | Immediate: desk head + counterparty risk team; Alert category `COUNTERPARTY_LIMIT` severity `CRITICAL` |
| Buy-in risk score ≥85 | Immediate: ops/settlements + trader; Alert category `RECALL_BUYIN` severity `CRITICAL` |
| CRITICAL reconciliation break | Same-day: ops recon team; flag `buyin_risk_relevant` or `capital_misstatement_relevant` |
| Platform cycle failure | Platform engineering on-call; check `secfi.cycle_run_log` for `status = FAILED` |
| Suspected capital-output misuse | Model Risk Management + Legal; outputs labeled "decision support approximation" everywhere |
| Data source down >4h | Platform engineering; fallback policy per source in docs/runbook.md |

Full runbook: **[docs/runbook.md](docs/runbook.md)**

---

## Sample Outputs

### Daily executive summary (Markdown, generated from the reference fixture book)

See **[docs/sample_outputs/daily_executive_summary_2026-06-18.md](docs/sample_outputs/daily_executive_summary_2026-06-18.md)** — produced by a real full-cycle run against the bundled fixtures, showing:
- Book NMV $116.6mm across 12 positions
- 4 reconciliation breaks (1 quantity mismatch P004, 1 price mismatch P009, 1 missing-at-custodian P010, 1 missing-on-book for CPTY004 position)
- GME (deep special, 6-day-old fail-to-deliver) at the top of the buy-in queue with buy-in risk 100/100
- 7 pricing recommendations with estimated P&L opportunities
- CPTY004 (watch-list counterparty) recommended REDUCE
- 3 corporate action watchlist items requiring desk action

### Buy-in queue (from `GET /v1/recall-buyin/queue`)

```json
[
  {
    "position_id": "P003",
    "ticker": "GME",
    "counterparty_id": "CPTY003",
    "urgency_score": 95.0,
    "buyin_risk_score": 100.0,
    "recommended_action": "RETURN",
    "drivers": [
      "Settlement fail aged 6 day(s); fail-to-deliver (desk owes).",
      "Locate shortage of 18000 shares against open requests.",
      "Security classified DEEP_SPECIAL; scarce replacement supply.",
      "Upcoming corporate action creates an imminent return obligation on this name."
    ]
  }
]
```

### Pricing recommendation (from `GET /v1/pricing/recommendations`)

```json
{
  "recommendation_id": "3f7a9b21-...",
  "action": "REPRICE",
  "target_id": "P003",
  "from_value": "300.00",
  "to_value": "2220.00",
  "estimated_pnl_impact_usd": "192455.00",
  "confidence": 0.865,
  "priority_score": 78.4,
  "approval_status": "PROPOSED",
  "rationale": [
    "GME is classified DEEP_SPECIAL (market weighted-avg 2220.0bps, desk rate 300.0bps, gap -1920.0bps).",
    "Estimated annualized P&L improvement from repricing: $192,455."
  ]
}
```

---

## Integration Points

| System | How the platform integrates |
|---|---|
| Internal trade capture | `InternalTradeCaptureConnector` → replace `fetch()` with Kafka consumer or DB replica read |
| EquiLend / DataLend | `EquiLendConnector` / `DataLendConnector` → replace with OAuth2 REST API client |
| Custodian | `CustodianFeedConnector` → replace with SWIFT MT5xx parser or custodian API client |
| Firm market data platform | `MarketDataConnector` → replace with internal pricing service client |
| Corporate actions feed | `CorporateActionsFeedConnector` → replace with ICE/Bloomberg DRSE/SIX API client |
| Firm secret manager | `configs/prod.yaml` already uses `${ENV_VAR_NAME}` pattern; inject at runtime |
| Firm SSO/OAuth2 | Add `Depends(get_current_user)` in `api/main.py`; wire to firm identity provider |
| Firm container orchestrator | `infra/docker/Dockerfile` produces the portable image; add K8s/ECS manifests |
| Firm DAG scheduler (Airflow/Dagster/Control-M) | `scripts/run_daily_batch.py` and `run_intraday_cycle.py` are the task bodies; no other changes |
| Firm trade booking system | APPROVAL endpoint records the decision; the trader then executes in the firm's booking system |

---

## Future Roadmap

| Item | Phase | Priority |
|---|---|---|
| MILP upgrade for optimization engine (lot-size / all-or-nothing moves) | Phase 4 | If lot-size effects prove material on the real book |
| Live message-bus ingestion for trade capture (Kafka consumer) | Phase 0 | High — reduces intraday data latency |
| Real-time rate streaming (EquiLend intraday API, when available) | Phase 3 | High |
| Multi-desk isolation (desk_id partition) | Phase 4 | If platform is shared across desks |
| True realized-P&L backtest loop (execution-system webhook integration) | Phase 2 | Required for model-risk sign-off |
| Non-parallel rate-shock scenarios (twist, steepener, flattener) | Phase 4 | Risk team request |
| Per-engine feature-flag toggles in orchestration/scheduler.py | Phase 3 | Ops convenience |
| Excess collateral / margin call prediction | Phase 4 | Extension of collateral_optimizer.py |
| Per-counterparty behavioral scoring (historical recall/dispute frequency) | Phase 4 | Requires 1+ year of execution history |
| Full CSDR / Reg SHO regime-specific buy-in notice tables | Phase 3 | Replace OPS-4 assumption |
| React/Next.js dashboard consuming the API | Phase 3 | Separate repo; API contract already stable |
| PostgreSQL → Redis hot-path caching for API | Phase 2 | Replace api/state.py in-memory store |

---

## Development Notes

### Adding a new engine

1. Create `src/secfi_platform/<your_module>/<engine>.py`
2. Import from `common/types.py` and `explainability/explain.py` — do not invent new output shapes
3. Emit `Recommendation` or `Alert` objects using the shared contract
4. Wire into `orchestration/scheduler.py run_full_cycle()` at the appropriate stage
5. Add an API endpoint in `api/routers/desk.py` or `risk_extended.py`
6. Write unit tests; run the full suite; confirm 0 regressions

### Adding a new data source

1. Create a connector class in `ingestion/connectors.py` extending `SourceConnector`
2. Add a `parse_*` function in `normalization/schema_mapping.py`
3. Add the source name to `ingestion.sources` in `configs/base.yaml`
4. Wire into `scripts/run_daily_batch.py _load_demo_dataset()`
5. Add fixture CSV to `tests/fixtures/` and update `tests/_helpers.py`

### Config-only threshold changes (no code deploy needed)

All risk/alert thresholds live in `configs/base.yaml`. Changes to these — including specialness tiers, growth/reduce thresholds, alert severity thresholds — are config changes, not code changes. They go through standard config version control but do NOT require the model-risk governance checklist.

---

## Citation and Governance

- **Model owner**: Securities Finance Quant Engineering (assign before production deployment)
- **Last validation date**: Populate `configs/prod.yaml` `governance.last_validation_date` at each release
- **Capital approximation**: `risk/capital_rwa.py` is desk-decision-support only — never for regulatory reporting
- **Model risk tier**: High (optimization, buy-in scoring, capital approximation), Medium (pricing, growth, scenarios), Low (explainability scoring)

See **[docs/model_risk.md](docs/model_risk.md)** for the complete model inventory and governance checklist.

---

Built by Securities Finance Quant Engineering for the Global Securities Lending & Financing Desk, Prime Brokerage Division.
