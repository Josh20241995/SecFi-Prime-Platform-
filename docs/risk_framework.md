# Risk Framework & Controls

## Three lines of defense, mapped to this platform

| Line | Who | What this platform gives them |
|---|---|---|
| 1st — the desk | Trader, desk head | Recommendations, alerts, exposure dashboards, P&L opportunity queue |
| 2nd — risk management | Counterparty credit risk, market risk | Stress exposure, WWR flags, limit breach alerts, capital/RWA approximation (clearly labeled, see below) |
| 3rd — audit/model risk | Internal audit, model risk management | Immutable audit log (`secfi.recommendation_approval_log`, `secfi.cycle_run_log`), full reproducibility via correlation_id + config snapshot hash, `docs/model_risk.md` governance checklist |

## What this platform IS and IS NOT authorized to do

**IS authorized to:**
- Compute risk/capital/pricing analytics from ingested data
- Propose recommendations with full rationale and confidence scoring
- Raise alerts to humans
- Record human approve/reject decisions immutably

**IS NOT authorized to:**
- Execute trades, amend rates, or change limits
- Auto-approve its own recommendations under any configuration
- Be the system of record for official regulatory capital, RWA, or
  leverage ratio (see "Capital approximation" below)
- Bypass any existing risk control, limit, or approval workflow

This boundary is enforced architecturally, not just by policy: the API
layer (`api/routers/desk.py`) is read-mostly, and the one mutating
endpoint (`POST /v1/recommendations/approval-decision`) only transitions
an approval status and writes an audit record — see the router's
docstring.

## Capital approximation — explicit governance boundary

`risk/capital_rwa.py` computes a **desk-decision-support approximation**
of EAD/RWA/leverage exposure, modeled on the *shape* of Basel SA-CCR /
comprehensive-approach methodology for SFTs. It is explicitly:
- **NOT** the firm's books-and-records regulatory capital engine
- **NOT** to be used for regulatory filings or external capital attestation
- **A directional tool** so the desk can see the marginal capital cost of
  a trade BEFORE booking it, using risk weights that must be supplied or
  confirmed by Treasury/Capital Management (`configs/base.yaml`
  `capital.risk_weights_pct` — currently populated with the reference
  table in `risk/capital_rwa.py` `DEFAULT_RISK_WEIGHTS_PCT`, which is
  illustrative, not authoritative)

Any output of this module appearing in an official capital report is a
governance violation and should be escalated per the escalation path
below.

## Limit framework

| Limit type | Source of truth | Fallback when source absent |
|---|---|---|
| Counterparty exposure limit | `secfi.counterparty_limit` (credit risk system feed) | `configs/risk_limits.yaml` `limit_templates_usd` by tier — flagged, never silent |
| Issuer concentration cap | `configs/base.yaml` `risk_limits.issuer_concentration_cap_pct` (desk-level policy) | N/A — has a hard-coded default (15%) |
| Desk RWA budget | Optional, set per `OptimizationConstraints.desk_rwa_budget_usd` | If unset, optimizer does not constrain on RWA at all — must be set explicitly to activate |

Limit breaches generate `CRITICAL` alerts at ≥100% utilization and `HIGH`
alerts at ≥85% (configurable, `configs/base.yaml`
`alerting.thresholds.limit_utilization_*`).

## Stress testing

Six standard scenarios run on every cycle for every counterparty (see
`docs/algorithms.md` section 3): BASE, EQUITY_DOWN_10, EQUITY_DOWN_25_CRISIS,
RATES_UP_100BP, FX_USD_UP_10, COMBINED_STRESS. Scenario definitions live in
`risk/counterparty_risk.py` `STANDARD_SCENARIOS` and are mirrored in
`configs/risk_limits.yaml` `stress_scenarios` for desk-level visibility
without a code change (note: the code constants are currently the
authoritative source; syncing the YAML to actually drive scenario
selection is listed in the roadmap, see `README.md` "Future Roadmap").

## Wrong-way risk

Flagged (not probabilistically modeled) when a `BANK_DEALER`
counterparty's exposure concentrates ≥35% (configurable,
`risk_limits.wwr_concentration_threshold_pct`) in a single GICS sector,
or when the counterparty is on the firmwide watch list with any active
exposure. A genuine WWR model requires joint default/exposure correlation
data from the firm's credit risk system, which is explicitly out of scope
for this reference build — see `docs/assumptions_and_limitations.md` R-3.

## Data quality as a risk control

Every canonical object carries a `DataQualityFlag`. Confidence scoring
(`explainability/explain.py`) multiplicatively discounts (never zeroes
out) recommendations built on stale or fallback data — the floor at 0.10
is deliberate: a stale-but-present signal (e.g., delayed custodian feed
on a buy-in-risk name) should still surface, just with appropriately
lowered confidence, rather than disappear entirely when the desk may need
it most.

## Operational risk — reconciliation as a control

`reconciliation/recon_engine.py` flags every break with
`buyin_risk_relevant` and `capital_misstatement_relevant` booleans so
downstream consumers (alerting, growth engine's "unresolved CRITICAL
breaks force REDUCE" rule) can react to the SPECIFIC operational risk a
break represents, not just its generic severity label.

## Escalation paths

| Situation | Escalate to | Channel |
|---|---|---|
| Limit breach (CRITICAL alert) | Desk head + counterparty credit risk | `desk-risk-channel`, `counterparty-risk-team` (configs/base.yaml `alerting.routing`) |
| Buy-in risk score ≥85 | Trading desk + ops settlements | `desk-trading-channel`, `ops-settlements-team` |
| CRITICAL reconciliation break | Ops recon team | `ops-recon-team`; auto-escalates from HIGH after 3 days open if buy-in-relevant (`sql/procedures/sp_age_open_items.sql`) |
| Suspected capital/RWA output misuse (treated as official) | Model risk management + Treasury/Capital Management | Immediate, out-of-band — this is a governance violation, not a routine alert |
| Data source unavailable beyond staleness threshold | Platform engineering | `platform-engineering-channel` |
| Model behavior change needed (threshold, weight, formula) | Model owner (Securities Finance Quant Engineering) | Follow `docs/model_risk.md` change process — never a hotfix without the checklist |

## Escalation paths

| Trigger | Immediate action | Owner | SLA |
|---|---|---|---|
| Counterparty limit BREACH | Alert `CRITICAL`, ring-fence new trades | Desk head + Counterparty Risk | 15 min |
| Buy-in risk ≥85/100 | Ops escalation, locate alternative inventory | Trader + Settlements | Same day |
| CRITICAL reconciliation break (buyin_risk_relevant=True) | Manual custodian check, hold further settlements on affected name | Ops Recon | Same session |
| Capital output being used as official capital | Escalate to Model Risk + Legal immediately | Model Risk owner | Immediate |
| Platform cycle failure (cycle_run_log.status = FAILED) | Restore from last-known-good; check data source availability | Platform Engineering | 30 min |
| Data source down >4h during trading hours | Manual fall-back to prior EOD snapshot, flag all outputs as STALE | Platform Engineering | 30 min |
| Wrong-way risk flag on a watch-list counterparty | Reduce exposure, notify Counterparty Risk | Desk head | Same day |

## Risk measurement completeness matrix

| Risk type | Measured? | Module | Note |
|---|---|---|---|
| Counterparty credit exposure | ✓ | `risk/counterparty_risk.py` | Gross, net, collateralized, stress |
| Wrong-way risk | ✓ (flagged) | `risk/counterparty_risk.py` | Sector-concentration heuristic; see R-3 in assumptions doc |
| Capital / RWA | ✓ (approx) | `risk/capital_rwa.py` | Desk decision support only — see C-1 in assumptions doc |
| Concentration risk (issuer, sector) | ✓ | `risk/counterparty_risk.py` (HHI) + `optimization/book_optimizer.py` (constraint) | |
| Interest rate risk (DV01) | ✓ | `risk/rates_fx.py` | Cash-collateral positions and repo legs |
| FX exposure | ✓ | `risk/rates_fx.py` | Non-USD positions; fixture book is all-USD |
| Funding gap / term mismatch | ✓ | `risk/rates_fx.py` | Asset vs. funding tenor by bucket |
| Settlement / buy-in risk | ✓ | `recall_buyin/recall_risk_engine.py` | Scored 0-100 |
| Collateral risk | ✓ | `risk/collateral_optimizer.py` | CTD scoring, eligibility, substitution |
| Corporate action risk | ✓ | `corporate_actions/ca_impact_engine.py` | 60-day forward |
| Operational / break risk | ✓ | `reconciliation/recon_engine.py` | Break classification + buy-in risk flag |
| Credit migration risk | Partial | `common/types.py Counterparty.tier` + stress via `risk/counterparty_risk.py` | Full credit migration model requires firm credit risk system integration |
| Tail risk / VaR | Not implemented | — | See Phase 4 roadmap |
| Liquidity risk | Partial | `risk/rates_fx.py` funding gap + `risk/inventory_manager.py` lendable pool | Full LCR/NSFR treatment requires Treasury integration |
