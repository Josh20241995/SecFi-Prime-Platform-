# Governance

## Core principle

This platform is **advisory and analytical only**. It measures risk,
computes economics, and proposes recommendations. It never executes a
trade, amends a rate, changes a limit, or takes any action that moves
real money or real risk without an explicit, logged, human decision. This
is enforced architecturally (the API is read-mostly; see
`docs/risk_framework.md` "What this platform IS and IS NOT authorized to
do"), not merely by policy.

## Approval workflow

```
Engine generates Recommendation (approval_status = PROPOSED)
        │
        ▼
Desk/risk user reviews via dashboard or GET /v1/.../recommendations
        │
        ▼
POST /v1/recommendations/approval-decision  { decision: APPROVE | REJECT, decided_by, comment }
        │
        ▼
approval_status -> APPROVED | REJECTED   (immutable log row written to
                                           secfi.recommendation_approval_log)
        │  (if APPROVED)
        ▼
Human trader executes through the FIRM'S EXISTING trade booking / rate
amendment workflow — this platform does NOT call any execution system.
        │
        ▼
(Phase 2+, see docs/implementation_plan.md) approval_status -> EXECUTED,
correlated manually or via a future execution-system webhook integration.
```

No recommendation transitions to APPROVED without a `decided_by` value —
the API schema (`ApprovalDecisionRequest`) requires it. No code path
auto-transitions a recommendation to APPROVED.

## Permissions / access control

This reference build does not implement authentication/authorization
(out of scope for a reference architecture — the firm's existing identity
provider and API gateway should front this service). The **intended**
permission model, to be enforced at the gateway/middleware layer in
production:

| Role | Read access | Approval-decision access |
|---|---|---|
| Trading desk head | All recommendations, all counterparties | Yes, all queues |
| Trader | All recommendations, all counterparties (desk-wide visibility is standard for this size of desk) | Yes, all queues |
| Risk manager | All exposure/capital/stress output | No — risk reviews and can flag, but approval authority sits with the desk per this build's design; adjust per the firm's actual delegation of authority policy |
| Portfolio/funding strategist | Capital/RWA, rates/FX views | No |
| Operations | Reconciliation breaks, settlement fails, recall/buy-in queue | No (can mark breaks resolved via direct SQL/ops tooling, not via the approval-decision endpoint, which is recommendation-specific) |
| Technology/platform engineering | Health checks, logs, `cycle_run_log` | No |
| Senior management | Daily executive summary | No |

**Action item for the security/IAM team before Phase 1 (see
`docs/implementation_plan.md`):** wire the firm's SSO/OAuth2 provider
into `api/main.py` (FastAPI dependency injection is the natural seam —
add an `Depends(get_current_user)` to every router function) and replace
the open `CORSMiddleware allow_origins=["*"]` dev default with the
firm's actual internal gateway origin (`configs/prod.yaml`
`api.cors_allowed_origins` already has the placeholder).

## Reference data change control

Enum changes (`common/enums.py`) ripple into SQL CHECK constraints
(`sql/schemas/*.sql`), config thresholds, and the explainability layer's
rationale templates. Any new enum value requires:
1. A reviewed PR updating the Python enum AND every SQL CHECK constraint
   referencing it (search for the enum's string values across `sql/schemas/`)
2. A test added or updated proving the new value is handled
3. Sign-off from the model owner if the value affects risk/pricing/
   optimization logic (see `docs/model_risk.md`)

## Auditability

Every cycle run is logged to `secfi.cycle_run_log` with `correlation_id`,
`config_snapshot_hash`, and `code_version`. Every recommendation traces
back to the exact cycle that produced it. Every human decision is logged
immutably to `secfi.recommendation_approval_log`. Structured JSON logging
(`common/logging_setup.py`) tags every log line with the active
correlation ID so an entire cycle's execution — every engine, every
warning, every data-quality rejection — can be reconstructed from logs
alone, independent of the database.

## Escalation paths

See `docs/risk_framework.md` "Escalation paths" for the full table
(limit breaches, buy-in risk, critical recon breaks, suspected capital-
output misuse, stale data, model change requests).

## What requires model governance sign-off

Any change to:
- `risk/counterparty_risk.py` (exposure/stress/WWR logic)
- `risk/capital_rwa.py` (capital approximation methodology or risk weights)
- `risk/rates_fx.py` (DV01/FX/hedge logic)
- `optimization/book_optimizer.py` (objective function or constraints)
- `pricing/pricing_intelligence.py` (specialness thresholds or mispricing scoring)
- `recall_buyin/recall_risk_engine.py` (urgency/buy-in scoring)
- `corporate_actions/ca_impact_engine.py` (event impact weights)
- `growth/counterparty_growth.py` (growth/contraction decision tree)
- `explainability/explain.py` (confidence/priority scoring — affects every engine)

...must follow the checklist in `docs/model_risk.md` and is gated in CI
(`.github/workflows/ci.yml` `model-risk-checklist-gate`).

## Known failure modes and how the platform responds

| Failure | Platform response |
|---|---|
| A data source is unavailable | `DataSourceUnavailableError` raised, caught at the orchestration boundary, cycle does not silently produce wrong output — see `scripts/run_daily_batch.py main()` |
| A single row of data is malformed | Isolated by `normalization/schema_mapping.py parse_rows()`, logged to `secfi.data_quality_exception_log`, does not fail the batch |
| A required column is missing from a reconciliation input | `reconciliation/recon_engine.py reconcile()` raises `ValueError` immediately — fails loudly rather than silently reconciling against the wrong shape |
| The LP solver is infeasible | `optimize_book()` returns `solver_status` containing the diagnostic message and `infeasible_reason`, with an empty recommendation list — never silently returns garbage |
| A counterparty has no configured limit | Falls back to the tier-based template in `configs/risk_limits.yaml`, with `utilization_pct=None`/`limit_breached=False` returned when even the fallback is absent — the absence of a limit is visible in the output shape, not hidden |
| Confidence inputs are degenerate (negative age, certainty > 1) | `explainability/explain.py compute_confidence()` clamps all inputs — bounded-output property test in `test_explainability.py test_confidence_bounded_zero_to_one` |
