# Architecture

## 1. Executive Summary

SecFi Prime Platform is a decision-support and optimization system for a global
securities lending, financing, repo, and prime brokerage desk. It does not
trade, book, or amend anything on its own. It ingests the desk's book and
external market/reference data, computes risk, capital, and pricing
analytics, and produces **explainable, human-approvable recommendations** and
**alerts** across ten functional areas:

| # | Capability | Primary module(s) |
|---|---|---|
| 1 | Counterparty exposure & balance sheet risk | `risk/counterparty_risk.py` |
| 2 | Interest rate & FX risk, hedge recommendations | `risk/rates_fx.py` |
| 3 | Book optimization (lend/borrow/repo) | `optimization/book_optimizer.py` |
| 4 | Recall/return/buy-in/supply risk | `recall_buyin/recall_risk_engine.py` |
| 5 | Counterparty growth/contraction opportunities | `growth/counterparty_growth.py` |
| 6 | Capital, RWA, leverage, balance-sheet consumption | `risk/capital_rwa.py` |
| 7 | Market pricing intelligence (EquiLend/DataLend) | `pricing/pricing_intelligence.py` |
| 8 | Custodian/balance-sheet reconciliation | `reconciliation/recon_engine.py` |
| 9 | Corporate action impact (60-day forward) | `corporate_actions/ca_impact_engine.py` |
| 10 | Everything tying together: alerts, reporting, audit | `alerting/`, `reporting/`, `orchestration/` |

Every analytical output — whether a repricing suggestion, a recall
recommendation, or a "grow this counterparty" call — is emitted as a
**`Recommendation`** object (`common/types.py`) carrying a rationale, a
confidence score, a data-completeness score, and a priority score. Nothing
auto-executes. A human approves or rejects via the API
(`POST /v1/recommendations/approval-decision`), and that decision is
permanently logged (`secfi.recommendation_approval_log`). See
`docs/governance.md` for the full control framework.

The platform is intentionally **boring and explainable** where it can be
(rules, thresholds, transparent linear scoring) and reaches for real
optimization (linear programming) only where the problem is genuinely a
constrained allocation problem (book optimization). See `docs/algorithms.md`
for the rationale behind each modeling choice.

## 2. Full Architecture

```
                              ┌─────────────────────────────────────────┐
                              │         EXTERNAL DATA SOURCES            │
                              │  EquiLend · DataLend · Internal Trade    │
                              │  Capture · Custodian · Market Data ·     │
                              │  Corporate Actions · Balance Sheet ·     │
                              │  Settlement/Fails                        │
                              └───────────────────┬───────────────────────┘
                                                  │
                              ┌───────────────────▼───────────────────────┐
                              │   INGESTION LAYER  (ingestion/)            │
                              │   One SourceConnector per source.          │
                              │   Raises DataSourceUnavailableError on     │
                              │   failure -> orchestration applies         │
                              │   fallback policy (stale-flag, don't crash)│
                              └───────────────────┬───────────────────────┘
                                                  │ raw dict rows
                              ┌───────────────────▼───────────────────────┐
                              │ NORMALIZATION LAYER (normalization/)       │
                              │ raw -> canonical dataclasses               │
                              │ (common/types.py). Required-field          │
                              │ validation -> DataQualityError; bad rows   │
                              │ isolated, not batch-fatal.                 │
                              └───────────────────┬───────────────────────┘
                                                  │ canonical objects
              ┌───────────────────────────────────┼───────────────────────────────────┐
              │                                   │                                   │
  ┌───────────▼───────────┐         ┌─────────────▼─────────────┐       ┌─────────────▼─────────────┐
  │   RISK LAYER (risk/)    │         │  PRICING (pricing/)        │       │ RECONCILIATION             │
  │ counterparty_risk.py    │         │  pricing_intelligence.py   │       │ (reconciliation/)           │
  │ capital_rwa.py           │         │                             │       │ recon_engine.py              │
  │ rates_fx.py                │         └─────────────┬─────────────┘       └─────────────┬─────────────┘
  └───────────┬───────────┘                       │                                   │
              │                                   │                                   │
              │                ┌──────────────────▼──────────────────┐                │
              │                │  OPTIMIZATION (optimization/)         │                │
              │                │  book_optimizer.py — LP allocation    │                │
              │                └──────────────────┬──────────────────┘                │
              │                                   │                                   │
  ┌───────────▼───────────┐         ┌─────────────▼─────────────┐       ┌─────────────▼─────────────┐
  │  GROWTH (growth/)        │         │ RECALL/BUY-IN (recall_buyin/) │     │ CORPORATE ACTIONS            │
  │  counterparty_growth.py   │         │ recall_risk_engine.py         │     │ (corporate_actions/)          │
  └───────────┬───────────┘         └─────────────┬─────────────┘       │ ca_impact_engine.py            │
              │                                   │                     └─────────────┬─────────────┘
              └───────────────────┬───────────────┴───────────────────────────────────┘
                                  │  every engine emits Recommendation / Alert objects
                  ┌───────────────▼───────────────┐
                  │  EXPLAINABILITY (explainability/)│   shared confidence + priority scoring,
                  │  explain.py                       │   used by every recommendation-producing engine
                  └───────────────┬───────────────┘
                                  │
                  ┌───────────────▼───────────────┐
                  │ ALERTING (alerting/)             │   threshold + event-driven Alert objects
                  │ alert_engine.py                   │
                  └───────────────┬───────────────┘
                                  │
                  ┌───────────────▼───────────────┐
                  │ REPORTING (reporting/)           │   executive summary, queues, drill-downs
                  │ daily_summary.py                  │
                  └───────────────┬───────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
┌───────▼────────┐     ┌──────────▼──────────┐     ┌────────▼────────┐
│ SQL PERSISTENCE  │     │  API LAYER (api/)      │     │ AUDIT LOG          │
│ (sql/schemas/)    │     │  FastAPI, read-mostly,  │     │ cycle_run_log,      │
│ Postgres 14+       │     │  approval endpoint      │     │ recommendation_     │
│                     │     │                          │     │ approval_log         │
└─────────────────┘     └─────────────────────┘     └─────────────────┘
                                  │
                  ┌───────────────▼───────────────┐
                  │ ORCHESTRATION (orchestration/)   │   wires every stage above together;
                  │ scheduler.py — run_full_cycle()   │   the function a real DAG scheduler calls
                  └────────────────────────────────┘
```

### Layer responsibilities (one sentence each)

- **Ingestion** — gets bytes from somewhere external into Python dicts; knows
  about vendor formats; knows NOTHING about risk/pricing/optimization logic.
- **Normalization** — converts dicts into validated, typed, canonical domain
  objects; the only place vendor field names appear.
- **Risk** — pure functions of (positions, counterparties, market data) ->
  exposure/capital/rate-risk numbers; no recommendations, just measurement
  (apart from rates_fx.py's hedge suggestions, which are still descriptive,
  not optimized).
- **Pricing / Optimization / Recall-Buyin / Corporate Actions / Growth** —
  the five "decision support" engines; each consumes risk/normalized data and
  emits `Recommendation` objects via the shared explainability contract.
- **Explainability** — shared confidence/priority scoring so the five engines
  above are comparable in one unified queue.
- **Alerting** — turns selected engine outputs into human-facing `Alert`
  objects with severity and routing.
- **Reporting** — pure aggregation of everything above into desk-facing
  artifacts; computes nothing new.
- **Orchestration** — the job graph; the only module that knows the call
  order between every other module.
- **API** — the desk's window into the latest cycle's output, plus the
  approval-decision endpoint. Read-mostly by design.
- **SQL persistence** — the durable system of record; what the API reads
  from in production (this reference build uses an in-memory cycle-output
  cache for tests/local dev — see `api/state.py` docstring).

### Why this layering

Every arrow in the diagram only points one direction. Risk/Pricing/
Optimization/etc. never import from Reporting or API. This means:
1. Any engine can be unit tested with plain Python objects, no HTTP server,
   no database — see `tests/unit/`.
2. A new consumer (e.g., a Slack bot, a second dashboard, a Python notebook)
   can sit alongside the API and read the same `CycleOutputs` without any
   engine code changing.
3. Swapping the in-memory store (`api/state.py`) for real Postgres reads is
   a change isolated to one file.

## Technology choices, by layer (and why)

| Layer | Technology | Why |
|---|---|---|
| Domain model | Python `dataclasses` | Hot-path objects recomputed every cycle; validation belongs at the ingestion boundary, not scattered through every transform. See `common/types.py` docstring. |
| API DTOs | `pydantic` (FastAPI) | Boundary code benefits from automatic schema generation / OpenAPI docs; this is NOT a hot loop. |
| Optimization | `scipy.optimize.linprog` (HiGHS) | Open-source, no license dependency, genuinely solves the LP relaxation of the book allocation problem at institutional scale. MILP upgrade path documented in `optimization/book_optimizer.py` and `docs/algorithms.md`. |
| Reconciliation | `pandas` | Set-based join/matching problem over potentially hundreds of thousands of rows — exactly pandas's sweet spot. |
| Config | YAML + environment-variable secret resolution | Versioned, environment-layered, secrets externalized — matches how a bank platform team manages config. |
| Logging | Structured JSON + correlation IDs | Every cycle run reconstructable end-to-end for incident review; joins to `secfi.cycle_run_log`. |
| Persistence | PostgreSQL 14+ | Default firm-neutral choice; `docs/architecture.md` portability note covers Sybase/SQL Server legacy stacks still common in prime brokerage. |
| API | FastAPI + uvicorn | Async-capable, auto-documented, standard for new internal bank services. |
| Containerization | Docker, multi-stage build, non-root runtime user | Matches standard bank container security baselines. |

No Java/Scala/.NET component was introduced — there was no integration
requirement in this build that a Python service boundary (FastAPI + a
message/queue client where needed) couldn't satisfy. If the firm's existing
trade capture system only exposes a Java RMI or .NET WCF interface, the
ingestion layer's `SourceConnector` interface (`ingestion/base.py`) is the
correct seam to add a thin adapter, written in whichever language speaks
that legacy protocol most naturally, that drops normalized rows onto a
queue this platform reads — not a reason to rewrite this platform.

## C++ note

No component of this reference build required C++. The optimization engine
is not on a hard real-time/HFT-style latency budget (it runs once per cycle,
every 1-15 minutes, not per-tick), so a compiled extension would add
deployment complexity without a measurable benefit here. If the desk later
adds a latency-sensitive component (e.g., real-time rate-stream processing
at >10K msgs/sec with single-digit-millisecond SLAs), that component should
be isolated as a separate service behind a queue, written in C++ or Rust,
publishing normalized events this platform's ingestion layer consumes — not
embedded inside the analytics engines.
