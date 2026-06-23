# Core Algorithms & Optimization Logic

## 1. Book optimization (`optimization/book_optimizer.py`)

### Problem formulation

Decision variable `x_i` = USD market value of position `i`'s balance
allocated to a particular (destination counterparty, rate) combination.
Two variable kinds per position:

- **REPRICE_CURRENT**: keep the position with its current counterparty,
  re-rate to the market composite rate.
- **REROUTE**: move balance to one of up to 3 alternative counterparties
  (fan-out capped for tractability — see "Scaling" below) at a rate equal
  to market minus a configurable competitive concession
  (`configs/base.yaml` `optimization.destination_rate_haircut_bps`).

Objective coefficient for each variable is the **negative** net economics
per dollar (annualized revenue minus capital cost, from
`risk/capital_rwa.py`), because `scipy.optimize.linprog` minimizes by
convention — the module negates internally so the public objective is
"maximize net risk-adjusted revenue."

```
maximize   Σ_i  netEconomicsPerDollar_i · x_i
subject to
  Σ_{variables of position p}  x_i  ≤  marketValue(p)              (can't reallocate more than you have)
  Σ_{variables routed to counterparty c}  x_i  ≤  limit(c)         (counterparty exposure limit)
  Σ_{variables on issuer j}  x_i  ≤  concentrationCap · bookNMV     (issuer concentration cap)
  Σ_i  rwaPerDollar_i · x_i  ≤  rwaBudget                            (optional desk RWA budget)
  x_i ≥ 0
```

### Why LP relaxation, not MILP, in this reference build

Real securities-finance optimization has genuinely integer aspects
(minimum lot sizes, all-or-nothing counterparty moves). This build solves
the **linear relaxation** with `scipy.optimize.linprog` (HiGHS backend —
the same dual-simplex/interior-point solver family used in serious
commercial LP work, fully open source, zero license dependency) and
applies a documented heuristic rounding pass (`_round_and_refine`) that
zeroes out economically-meaningless "dust" allocations below 1% of a
variable's own upper bound.

**Upgrade path to true MILP** (when lot-size/discreteness materially
changes the answer — e.g., very small books, or counterparties with large
minimum-ticket conventions): the constraint-builder code in
`optimize_book` emits plain numpy arrays (`A_ub`, `b_ub`, `bounds`, `c`).
Swap the `linprog(...)` call for:
- `PuLP` + CBC (open source, easy swap, same array shapes work with minor
  adaptation), or
- OR-Tools CP-SAT (better for highly combinatorial discreteness), or
- a commercial solver (Gurobi/CPLEX) if the firm has an existing license.

No other code in the module needs to change — the constraint-builder
functions are solver-agnostic by design.

### Scaling

Fan-out per position is capped at 3 alternative destination counterparties
(`orchestration/scheduler.py` `run_full_cycle`) to keep variable count
tractable: N positions × (1 reprice + 3 reroute) = 4N variables. At
10,000 positions that's 40,000 variables, comfortably within HiGHS's
practical range on commodity hardware. If the desk's universe of
candidate counterparties per position needs to be larger, increase the
slice but watch wall-clock time — HiGHS scales roughly linearly to
low-degree-polynomial in practice for problems this sparse and structured.

### Noise suppression

`OptimizationConstraints.min_economic_pickup_bps` (default 2.0bps)
suppresses REPRICE_CURRENT recommendations below a noise threshold — this
prevents recommendation fatigue from sub-basis-point "optimal" reallocations
that aren't worth the operational friction of actioning.

## 2. Pricing intelligence (`pricing/pricing_intelligence.py`)

### Specialness classification

A deterministic decision tree (not a classifier) over fee level and
utilization, with thresholds in `configs/base.yaml`
`pricing.specialness_thresholds`. The highest-value tier to detect early
is **SPECIALS_IN_WAITING**: utilization has crossed a tightening threshold
(default 85%+) but the fee hasn't repriced yet (still below the special
floor, default 100bps) — this is the window where the desk can capture
the spread before the rest of the market catches up.

### Mispricing scoring

Gap = desk rate − market weighted-average rate, sign-adjusted by economic
side (see below). Z-scored **within specialness tier**
(`_tier_dispersion_stats`) so a 5bp gap on a GC name (large relative miss)
is scored very differently from a 5bp gap on a 2000bp deep special
(noise). This avoids the common mistake of using a single global z-score
across wildly different rate regimes.

### Economic-side convention (important, previously a bug, now fixed and
regression-tested — see `tests/unit/test_pricing_intelligence.py`
`test_reverse_repo_not_misclassified_as_paying_side`)

- **"Earns the rate" side** (wants the rate HIGH): `LEND` (lends
  securities, earns fee) and `REVERSE_REPO` (lends cash, earns repo rate).
- **"Pays the rate" side** (wants the rate LOW): `BORROW` and `REPO`.

This mirrors the DV01 sign convention in `risk/rates_fx.py`
`_rate_exposure_sign` for the same underlying economic reason, and the two
should always be kept consistent if either is modified.

## 3. Counterparty risk & stress testing (`risk/counterparty_risk.py`)

Per-position signed exposure:
```
LEND / REVERSE_REPO:  exposure = shockedMarketValue(security/cash leg) − haircutAdjustedCollateralValue
BORROW / REPO:        exposure = haircutAdjustedCollateralValue − shockedMarketValue(security/cash leg)
```
Positive exposure = desk has credit exposure TO the counterparty (the
quantity counterparty limits are measured against). Six standard shock
scenarios (`STANDARD_SCENARIOS`) apply equity price shocks, rate shocks
(reserved for future curve-based repricing — current build applies them
via the rates/FX module, not by reshocking SFT exposure rates directly),
FX shocks, and collateral spread-widening (which increases effective
haircuts, i.e., reduces effective collateral value under stress).

Concentration is measured via Herfindahl-Hirschman Index (HHI) on issuer
exposure share (0–10,000 scale, matching the standard antitrust/risk
convention). Wrong-way risk is **flagged**, not probabilistically modeled
— a real joint-default-correlation WWR model requires data this platform
does not assume access to (see `docs/assumptions_and_limitations.md`
R-3).

## 4. Capital/RWA approximation (`risk/capital_rwa.py`)

```
EAD  = max(0, |marketValue − haircutAdjustedCollateralValue|) · (1 + sftAddonPct)
RWA  = EAD · riskWeight(counterpartyType, tier)          # config-driven table, NOT invented Basel constants
LeverageExposure = EAD,  reduced 40% if counterparty.is_netting_eligible (simplified MNA netting proxy)
CapitalRequired = RWA · targetCET1Ratio
CapitalCost = CapitalRequired · costOfCapitalPct
RAROC = (revenue − PD·LGD·EAD) / CapitalRequired           # only computed when PD/LGD present
```

**This is explicitly a desk-decision-support approximation, not official
regulatory capital** — see the governance note at the top of
`risk/capital_rwa.py` and `docs/model_risk.md`.

## 5. Recall/buy-in urgency scoring (`recall_buyin/recall_risk_engine.py`)

```
urgency = min(100,
    failAgeComponent(failAgeDays)                  # min(failAgeDays·12, 60)
  + 15 if locate shortage exists
  + 15 if HTB/DEEP_SPECIAL, else 8 if SPECIALS_IN_WAITING
  + 20 if security has an imminent CA-driven return obligation
  + 5 if no substitute inventory identified
)

buyInRisk = min(100,
    baseFromFailAgeAndDirection(fail)               # fail-to-deliver scores much higher than fail-to-receive
  · specialnessMultiplier(tier)                       # 0.8 (GC) .. 2.2 (DEEP_SPECIAL)
  · (1.25 if no substitute else 1.0)
)
```
Both scores saturate at 100 — verified by regression test
(`test_substitute_availability_reduces_buyin_score`, which deliberately
uses a moderate-severity synthetic fail to demonstrate the multiplier's
effect below the saturation point, since the real GME fixture saturates
both with and without a substitute).

## 6. Corporate action impact (`corporate_actions/ca_impact_engine.py`)

Five independent 0–100 sub-scores (supply, recall, rate-dislocation,
settlement-fail, balance-sheet-impact) from a per-event-type base-weight
table (`EVENT_IMPACT_WEIGHTS`), scaled by a proximity multiplier
(1.5× within 2 days down to 0.5× at 31–60 days), then combined into a
composite score with fixed weights (25/25/20/15/15%). Urgency
(`ActionUrgency`) is derived from composite score AND days-to-event
jointly — a low-impact event tomorrow is not more urgent than a
high-impact event in 5 days.

## 7. Growth/contraction recommendation (`growth/counterparty_growth.py`)

A deterministic decision tree, deliberately NOT a black-box classifier
(every number here must be defensible to a risk committee — see
`docs/model_risk.md`):

```
watch_list OR unresolved CRITICAL recon break  -> REDUCE
utilization > reduceThreshold (90%)             -> REDUCE
RoC < minAcceptableRoC (5%)                       -> REDUCE
minAcceptableRoC ≤ RoC < targetRoC (15%)            -> REPRICE
RoC ≥ targetRoC AND headroom AND no WWR flags        -> GROW
else                                                    -> HOLD
```

## 8. Shared explainability/confidence model (`explainability/explain.py`)

```
confidence = dataCompletenessPct · modelCertainty · stalenessPenalty(ageMinutes)
stalenessPenalty = max(0.5^(age/halfLife), 0.10)      # floored, never fully zeroes a recommendation out

priorityScore = (0.35·pnlScore + 0.30·riskScore + 0.25·urgencyScore + 0.10·confidence·100)
                 · (0.5 + 0.5·confidence)               # confidence damps the WHOLE score, not just its own term
```
Deliberately simple and auditable — every weight is a named constant a
risk committee can review, not a learned parameter. See
`docs/model_risk.md` for why this platform avoids black-box ML for
anything that drives a desk action.
