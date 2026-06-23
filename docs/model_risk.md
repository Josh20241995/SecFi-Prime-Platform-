# Model Risk Management

## Model inventory

| Model | Type | Module | Risk tier (suggested) |
|---|---|---|---|
| Counterparty exposure & stress | Deterministic calculation | `risk/counterparty_risk.py` | Medium — informs limit monitoring |
| Capital/RWA approximation | Deterministic calculation, SA-CCR-shaped | `risk/capital_rwa.py` | **High** — see disclaimer below; must never be mistaken for official capital |
| Rates/FX risk (DV01, FX exposure) | Deterministic calculation | `risk/rates_fx.py` | Medium |
| Specialness classification & mispricing scoring | Deterministic rule/threshold + z-score | `pricing/pricing_intelligence.py` | Medium — drives P&L-impacting recommendations |
| Book optimization | Linear program | `optimization/book_optimizer.py` | **High** — directly proposes balance reallocation across counterparties |
| Recall/buy-in urgency & risk scoring | Deterministic weighted scoring | `recall_buyin/recall_risk_engine.py` | High — informs operational risk prioritization |
| Corporate action impact scoring | Deterministic weighted scoring | `corporate_actions/ca_impact_engine.py` | Medium |
| Counterparty growth/contraction decision tree | Deterministic rule tree | `growth/counterparty_growth.py` | Medium |
| Confidence/priority scoring | Deterministic calculation | `explainability/explain.py` | Low individually, but affects ranking across every other model |

**Why every model here is deterministic/rule-based rather than learned
(ML):** every number this platform produces must be explainable to a
trader in one sentence and defensible to a risk/model-governance
committee without reference to a training run, a feature importance plot,
or a black box. A threshold table a desk head can read and adjust in
`configs/base.yaml` is more auditable, more correctable, and more
appropriate for a regulated capital-markets desk than a model that would
need retraining to reflect a one-line policy change. This is a considered
design choice, not an absence of ML expertise — see
`docs/architecture.md` "Technology choices."

## Capital approximation disclaimer (repeated here deliberately — this is
## the single most important governance boundary in the platform)

`risk/capital_rwa.py` is a **desk-decision-support approximation**, not
the firm's official regulatory capital engine. See the prominent comment
block at the top of that module and `docs/risk_framework.md` "Capital
approximation — explicit governance boundary." Any production deployment
must:
1. Have Treasury/Capital Management review and supply the authoritative
   risk-weight table (replacing `DEFAULT_RISK_WEIGHTS_PCT`)
2. Confirm with Model Risk Management that this approximation is
   appropriately labeled in every UI surface that displays it
3. Never connect this module's output to any official regulatory
   reporting pipeline

## Model change governance checklist

Required before merging any change to a module listed in
`docs/governance.md` "What requires model governance sign-off":

- [ ] **Rationale documented** — why is this change needed, what problem
      does it solve, in the PR description
- [ ] **Backward-compatibility assessed** — does this change the meaning
      of an existing config key, enum value, or output field that other
      systems/dashboards may depend on?
- [ ] **Tests updated or added** — every behavior change must have a
      corresponding test change; a model PR with no test diff should be
      treated as suspicious, not approved
- [ ] **Full test suite passes** — `pytest tests/ -v` (or
      `python -m unittest discover -s tests`), zero failures
- [ ] **Threshold/weight changes justified** — if the change adjusts a
      numeric constant (e.g., `special_fee_floor_bps`,
      `target_roc`, `wwr_concentration_threshold_pct`), the PR description
      states the basis for the new value (desk feedback, observed false-
      positive rate, a specific incident, etc.) — not just "tuned it"
- [ ] **Reviewed by model owner** — Securities Finance Quant Engineering
      (or designated successor team)
- [ ] **Reviewed by a second technical reviewer** — standard four-eyes
      principle for anything touching `risk/`, `optimization/`, `pricing/`
- [ ] **PR description contains `MODEL_RISK_CHECKLIST_ACKNOWLEDGED`** —
      mechanically enforced by CI
      (`.github/workflows/ci.yml` `model-risk-checklist-gate`)

## Validation requirements before initial production deployment

1. **Parallel run** — run the platform alongside the desk's existing
   process (spreadsheet, legacy tool, whatever currently exists) for a
   minimum of 4 weeks (Phase 1 of `docs/implementation_plan.md`), with
   daily reconciliation of headline numbers (book NMV, gross exposure by
   counterparty, top-line capital/RWA approximation) between the two.
2. **Independent code review** — a reviewer who did not write the
   analytics code walks every formula in `docs/algorithms.md` against the
   actual implementation line by line.
3. **Threshold sensitivity review** — for every configurable threshold
   (specialness tiers, growth/reduce thresholds, alert thresholds), the
   desk head and risk management jointly confirm the defaults are
   sensible for THIS desk's actual book, not just the reference fixture
   values shipped in `configs/base.yaml`.
4. **Explainability spot-check** — pull 20 random recommendations from a
   parallel-run cycle and have an experienced trader independently assess
   whether the rationale is sound, without seeing the platform's
   confidence/priority score first (blind review, to catch anchoring).

## Backtesting plan (post Phase 2, once execution data exists)

For every recommendation with `approval_status = EXECUTED`:
```
realized_pnl_error = realized_pnl - estimated_pnl_impact_usd
```
Track distribution of `realized_pnl_error` by `source_engine`,
`action`, and `confidence` bucket. Expected pattern: error magnitude
should shrink as `confidence` rises (a 95%-confidence recommendation
should be a noticeably better P&L predictor than a 20%-confidence one).
If it doesn't, the confidence model itself
(`explainability/explain.py`) needs review — confidence should be
calibrated, not just monotonic-feeling.

## False-positive / false-negative review cadence

Quarterly review (minimum) of:
- Alerts that were acknowledged but led to no action (potential
  false-positive — review threshold)
- Buy-in risk scores ≥85 that did NOT escalate to an actual buy-in
  (precision check)
- Settlement fails that DID escalate to a buy-in without first crossing
  the ≥70 alert threshold (recall check — a miss here is more serious
  than a false positive and should trigger an immediate threshold review,
  not wait for the quarterly cycle)

## Rollback strategy for a bad model change

Per `docs/implementation_plan.md` "Rollback strategy" — redeploy the
previous container tag. Because every threshold lives in config
(`configs/base.yaml`) rather than hardcoded in logic, many "bad tuning"
incidents can be resolved with a config-only rollback (faster, lower
blast radius) rather than a full code rollback — confirm this is the
case for the specific change before choosing the remediation path.

## Model owner

Securities Finance Quant Engineering (placeholder — assign the actual
owning team/individual before production deployment; `configs/prod.yaml`
`governance.model_owner` and `governance.last_validation_date` must be
populated at each release, per that file's inline comment).
