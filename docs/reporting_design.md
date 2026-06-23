# Output / Reporting Design

## Output inventory (mapped to spec section L)

| Required output | Where it lives | Format |
|---|---|---|
| Daily executive summary | `reporting/daily_summary.py` `build_daily_executive_summary` + `render_markdown` | `DailyExecutiveSummary` dataclass; Markdown render for email/Slack/PDF distribution. Sample: `docs/sample_outputs/daily_executive_summary_2026-06-18.md` |
| Intraday alert feed | `alerting/alert_engine.py` `collect_alerts`, persisted to `secfi.alert` | List[`Alert`], severity-sorted |
| Desk-level actionable recommendations | Every engine's `*_to_recommendations()` / `generate_*_recommendations()` function | List[`Recommendation`], persisted to `secfi.recommendation` |
| Counterparty heatmap | `GET /v1/risk/counterparty` (all counterparties) + `sql/views/v_counterparty_exposure_rollup.sql` | JSON array; frontend renders as heatmap (utilization% × tier × region) |
| Exposure dashboards | `GET /v1/risk/counterparty/{id}` | Full `CounterpartyExposureResponse` incl. stress, concentration, WWR flags |
| Rates dispersion view | `pricing/pricing_intelligence.py` `build_pricing_dispersion` (not yet exposed as a dedicated endpoint — see roadmap) | `PricingDispersionRow` list |
| Buy-in risk queue | `GET /v1/recall-buyin/queue` | `RecallQueueRowResponse` list, sorted by (buy-in risk, urgency) desc |
| Recall/return queue | Same endpoint — `recommended_action` field distinguishes RECALL/RETURN/SUBSTITUTE/HEDGE |
| Corporate action watchlist | `GET /v1/corporate-actions/watchlist` | `CorporateActionWatchlistRowResponse` list, sorted by composite risk desc |
| Reconciliation break dashboard | `GET /v1/reconciliation/breaks?severity=` | `ReconciliationBreakResponse` list, filterable by severity |
| Capital usage dashboard | `secfi.position_capital_profile` (persisted per cycle) — not yet exposed as a dedicated endpoint, see roadmap | Queryable historically for trend |
| Hedge recommendation view | `risk/rates_fx.py` `recommend_hedges()` output, part of `RatesAndFxRiskReport` | Not yet exposed as a dedicated endpoint, see roadmap |
| Drill-down by desk/entity/counterparty/product | `sql/views/v_book_summary.sql` (one row per position with every dimension joined) | SQL view, queryable with arbitrary `WHERE`/`GROUP BY` from any BI tool |

## Persona-to-output mapping (spec requirement: "usable by every persona")

| Persona | Primary outputs they use |
|---|---|
| Trading desk head | Daily executive summary, all recommendation queues, capital usage |
| Trader | Recall/buy-in queue, pricing dispersion, corporate action watchlist |
| Risk manager | Counterparty exposure dashboard, stress results, WWR flags, limit breach alerts |
| Portfolio/funding strategist | Rates/FX risk report, funding gap, growth/contraction recommendations |
| Operations team | Reconciliation break dashboard, settlement fail aging, buy-in risk queue |
| Technology team | `secfi.cycle_run_log`, `secfi.data_quality_exception_log`, `/healthz`, structured logs |
| Senior management | Daily executive summary (designed to be readable standalone, no drill-down required) |

## Design principle: reporting computes nothing

`reporting/daily_summary.py` is intentionally "dumb" — pure aggregation
and ranking (via `explainability/explain.py` `rank_recommendations`) of
upstream engine outputs. This means:
1. The dashboard/report format can evolve (new view, new export, a future
   PDF generator) without touching any risk/pricing/optimization logic.
2. Every number in a report is independently unit-testable at its source
   engine — the report test (`tests/integration/test_full_cycle.py`
   `test_executive_summary_internally_consistent`) only needs to verify
   the AGGREGATION is correct, not re-derive the underlying analytics.

## Drill-down pattern

Every `Recommendation` and queue row carries `target_type` +`target_id`,
so any UI can implement "click a recommendation -> see the full position/
counterparty/security record" generically, without per-engine UI code.
The `supporting_metrics` dict on every `Recommendation` carries engine-
specific detail (e.g., `specialness_tier`, `gap_bps` for pricing;
`urgency_score`, `substitute_candidates` for recall/buy-in) for exactly
this purpose.

## Sample output

A real, generated (not hand-written) sample daily executive summary from
this build's reference dataset is checked in at
`docs/sample_outputs/daily_executive_summary_2026-06-18.md`. It was
produced by running `scripts/run_daily_batch.py` against the bundled
`tests/fixtures/` dataset through the actual ingestion -> normalization ->
orchestration pipeline — not fabricated by hand.

## Roadmap items (not yet built — see README "Future Roadmap")

- Dedicated `/v1/pricing/dispersion`, `/v1/risk/rates-fx`, and
  `/v1/capital/usage` endpoints (the underlying engine output exists;
  only the router wiring is pending)
- A persisted, paginated recommendation/alert history API (currently
  serves only the latest cycle's in-memory output — see `api/state.py`)
- PDF rendering of the executive summary (Markdown render exists; PDF
  is a templating layer on top, not a new analytics requirement)

## Scheduling by output type

| Output | When produced | Triggering module |
|---|---|---|
| Daily executive summary | EOD full cycle + morning pre-market | `orchestration/scheduler.py` → `reporting/daily_summary.py` |
| Intraday alert feed | Every fast-cycle (15 min) | `alerting/alert_engine.py` → `alerting/prioritizer.py` |
| Pricing repricing queue | Every cycle | `pricing/pricing_intelligence.py` |
| Buy-in / recall urgency queue | Every fast-cycle | `recall_buyin/recall_risk_engine.py` |
| Optimization recommendations | Every fast-cycle | `optimization/book_optimizer.py` |
| Corporate action watchlist | Daily pre-market (CA data doesn't change intraday) | `corporate_actions/ca_impact_engine.py` |
| Reconciliation breaks | EOD full cycle (custodian data is T+1) | `reconciliation/recon_engine.py` |
| Capital/RWA profiles | Every full cycle | `risk/capital_rwa.py` |
| Rates/FX report | Every fast-cycle | `risk/rates_fx.py` |
| Growth/contraction opportunities | Every full cycle | `growth/counterparty_growth.py` |

## Audience mapping

| Output | Desk head | Trader | Risk mgr | Ops | Senior mgmt |
|---|---|---|---|---|---|
| Executive summary | ✓ primary | ✓ | ✓ | ✓ | ✓ primary |
| Alert feed | ✓ | ✓ primary | ✓ | ✓ | — |
| Pricing queue | ✓ | ✓ primary | — | — | — |
| Buy-in queue | ✓ | ✓ primary | ✓ | ✓ primary | — |
| CA watchlist | ✓ | ✓ | ✓ | ✓ primary | — |
| Recon breaks | — | — | ✓ | ✓ primary | — |
| Capital dashboard | ✓ | — | ✓ primary | — | ✓ |
| Hedge view | ✓ | ✓ | ✓ primary | — | — |
| Growth/contraction | ✓ primary | ✓ | ✓ | — | ✓ |

## Recommendation output contract (every engine, same shape)

```python
@dataclass
class Recommendation:
    recommendation_id: str          # UUID, stored in secfi.recommendation
    generated_at: datetime          # Timestamp for audit traceability
    source_engine: str              # "optimization.book_optimizer" etc.
    action: RecommendationAction    # GROW | HOLD | REDUCE | REPRICE | REROUTE | ...
    target_type: str                # "POSITION" | "COUNTERPARTY" | "SECURITY"
    target_id: str                  # Identifies what to act on
    quantity: Optional[Decimal]     # USD size of recommended action
    from_value: Optional[Decimal]   # Current rate/balance
    to_value: Optional[Decimal]     # Proposed rate/balance
    estimated_pnl_impact_usd: Optional[Decimal]
    estimated_capital_impact_usd: Optional[Decimal]
    estimated_rwa_impact_usd: Optional[Decimal]
    rationale: list[str]            # Human-readable explanation (list of clauses)
    supporting_metrics: dict        # Engine-specific detail (e.g. bps_pickup, z_score)
    confidence: float               # 0-1, shared formula from explainability/explain.py
    data_completeness_pct: float    # 0-1, fraction of required inputs present
    priority_score: float           # 0-100, shared formula, cross-engine comparable
    approval_status: ApprovalStatus # Always starts PROPOSED
```

This shared shape means a unified recommendation queue can be built
by any API consumer that simply concatenates all engine outputs and
sorts by `priority_score` — no per-engine parsing logic needed.
