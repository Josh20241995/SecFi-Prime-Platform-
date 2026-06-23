# Implementation Plan & Deployment Design

## Phased rollout plan

### Phase 0 — Foundations (weeks 1-3)
- Stand up Postgres schema (`sql/schemas/`), CI pipeline (`.github/workflows/ci.yml`)
- Wire real `ingestion/connectors.py` implementations for internal trade
  capture (highest priority — everything depends on it) and EquiLend/DataLend
- Replace `api/state.py` in-memory store with reads against
  `sql/views/v_book_summary.sql` and friends
- Confirm `risk/capital_rwa.py` risk-weight table with Treasury/Capital
  Management and load into `configs/prod.yaml` (do not ship the
  illustrative `DEFAULT_RISK_WEIGHTS_PCT` table to production unconfirmed)

### Phase 1 — Read-only desk pilot (weeks 4-8)
- Deploy API + dashboards to a small pilot group (2-3 traders, desk head)
- Run the full cycle daily, but **alerts/recommendations visible only,
  no approval workflow live yet** — this phase is about validating that
  the analytics match what the desk already knows to be true, before
  asking anyone to act on a recommendation
- Daily reconciliation between this platform's exposure/capital numbers
  and the desk's existing spreadsheet/legacy tool, logged and reviewed
- Model risk sign-off checklist (`docs/model_risk.md`) initiated in parallel

### Phase 2 — Approval workflow live (weeks 9-12)
- Enable `POST /v1/recommendations/approval-decision` for the pilot group
- Trace every APPROVED recommendation through to actual execution in the
  firm's existing trade booking system (manual correlation initially;
  this platform does not auto-execute, see `docs/governance.md`)
- Begin backtesting realized P&L from approved-and-executed recommendations
  against the recommendation's `estimated_pnl_impact_usd` (see
  `docs/model_risk.md` "Backtesting")

### Phase 3 — Desk-wide rollout + intraday cadence (weeks 13-16)
- Full desk access
- Intraday fast-cycle (`scripts/run_intraday_cycle.py` job) scheduled every
  15 minutes during trading hours
- Full EOD cycle (reconciliation included) scheduled at batch close
- Alert routing wired to real Slack/email channels
  (`feature_flags.enable_auto_alert_dispatch = true` in `configs/prod.yaml`)

### Phase 4 — Hardening & extension (ongoing)
- MILP upgrade for the optimization engine if lot-size effects prove
  material (see `docs/algorithms.md` section 1 upgrade path)
- Expand stress scenario set with firm-standard regulatory scenarios
  (CCAR/DFAST-style, if relevant to this desk's reporting obligations)
- Build the roadmap items listed in `docs/reporting_design.md` and
  `README.md` "Future Roadmap"

## Deployment topology

```
                        ┌─────────────────────┐
                        │   Internal Gateway     │   (firm SSO, network policy)
                        └───────────┬───────────┘
                                    │
                        ┌───────────▼───────────┐
                        │  API service (FastAPI)   │   N replicas, behind firm's
                        │  infra/docker/Dockerfile  │   standard container orchestrator
                        └───────────┬───────────┘
                                    │ reads
                        ┌───────────▼───────────┐
                        │   PostgreSQL 14+          │   sql/schemas/, firm-managed
                        │   (primary + read replica) │   instance, standard backup/DR policy
                        └───────────▲───────────┘
                                    │ writes
                        ┌───────────┴───────────┐
                        │  Orchestration cycle      │   scripts/run_daily_batch.py +
                        │  (Airflow/Dagster/        │   run_intraday_cycle.py as task bodies,
                        │   Control-M task)          │   triggered per docs/runbook.md cadence
                        └───────────┬───────────┘
                                    │ reads from
              ┌─────────────────────┼─────────────────────┐
   ┌──────────▼─────────┐ ┌─────────▼─────────┐ ┌─────────▼─────────┐
   │ Internal trade        │ │ EquiLend/DataLend     │ │ Custodian/balance-     │
   │ capture (event stream  │ │ APIs                  │ │ sheet feeds             │
   │ or DB replica)          │ │                        │ │                          │
   └─────────────────────┘ └─────────────────────┘ └─────────────────────┘
```

## Container build & deploy

```bash
# Build
docker build -f infra/docker/Dockerfile -t secfi-prime-platform:<tag> .

# Local stack (API + Postgres) for development
docker compose -f infra/docker/docker-compose.yml up

# Production: push to the firm's internal registry, deploy via the firm's
# standard container orchestrator (Kubernetes/ECS/internal PaaS) using
# this same image. No orchestration-platform-specific manifests are
# included here since that choice is firm-specific — infra/docker/
# provides the portable artifact; platform teams attach their own
# deployment manifests on top.
```

## Configuration & secrets

- `configs/base.yaml` — environment-agnostic defaults
- `configs/dev.yaml` / `configs/prod.yaml` — environment overlay
  (`common/config.py` deep-merges base + environment + optional
  `configs/local.yaml`, gitignored, for individual engineer overrides)
- Secrets are NEVER stored in YAML — only `${ENV_VAR_NAME}` references,
  resolved at load time from the process environment (injected by the
  firm's secret manager in production, from `.env` locally — see
  `.env.example`)
- `load_config()` fails loudly (raises `ConfigError`) if a required env
  var is referenced but unset — no silent fallback to an empty credential

## CI/CD pipeline (`.github/workflows/ci.yml`)

```
lint-and-typecheck  (ruff, black --check, mypy)
        │
unit-tests  (pytest tests/unit, coverage report)
        │
integration-tests  (pytest tests/integration)
        │
container-build  (docker build + smoke test)

[on pull_request only]
model-risk-checklist-gate  — blocks merge if a PR touches
  risk/, optimization/, pricing/, or recall_buyin/ without the
  MODEL_RISK_CHECKLIST_ACKNOWLEDGED marker in the PR description
  (see docs/model_risk.md)
```

## Rollback strategy

- Every container image is tagged with the Git SHA; rollback = redeploy
  the previous tag, no data migration required for analytics-only changes
- Database migrations (new columns/tables) follow standard additive-first
  practice: new nullable columns ship before code that writes them; code
  that reads them ships after backfill; destructive changes (drop column)
  only after the reading code is fully retired — gives a clean rollback
  window at every step
- `secfi.cycle_run_log` records `code_version` + `config_snapshot_hash`
  per cycle run, so any historical recommendation or alert can be
  reproduced byte-for-byte by checking out that exact code version and
  config snapshot and re-running `run_full_cycle` against the same input
  vintage — this is the platform's incident-reconstruction/replay
  capability
