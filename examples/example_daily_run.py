#!/usr/bin/env python3
"""
Example: ad-hoc usage of individual engines without running the full cycle.

This is the pattern a quant on the desk would use interactively (e.g., in
a Jupyter notebook) to answer a one-off question — "what does the buy-in
risk queue look like right now for just GME" — without paying the cost of
running every engine. Contrast with scripts/run_daily_batch.py, which
runs the full orchestrated pipeline for the desk-wide daily deliverables.

Run with:  python examples/example_daily_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests._helpers import (  # noqa: E402
    CYCLE_AS_OF,
    default_substitutes,
    load_locate_shortages,
    load_market_quotes,
    load_positions,
    load_settlement_fails,
)
from secfi_platform.pricing.pricing_intelligence import classify_specialness  # noqa: E402
from secfi_platform.recall_buyin.recall_risk_engine import compute_urgency_queue  # noqa: E402
from secfi_platform.risk.counterparty_risk import compute_counterparty_exposure  # noqa: E402
from tests._helpers import load_counterparties  # noqa: E402


def main():
    positions = load_positions()
    quotes = load_market_quotes()
    fails = load_settlement_fails()
    shortages = load_locate_shortages()
    substitutes = default_substitutes()
    counterparties = load_counterparties()

    # --- Question 1: what's the buy-in risk queue look like? -----------------
    specialness = {sec_id: classify_specialness(q) for sec_id, q in quotes.items()}
    queue = compute_urgency_queue(positions, fails, shortages, specialness, substitutes,
                                   ca_driven_return_security_ids=set())
    print("=== Buy-In / Recall Urgency Queue ===")
    for row in queue:
        print(f"  {row.ticker:8s} buy-in={row.buyin_risk_score:5.1f} urgency={row.urgency_score:5.1f} "
              f"action={row.recommended_action.value}")

    # --- Question 2: what's our exposure to a single counterparty? -----------
    target_cpty_id = "CPTY002"
    cpty = counterparties[target_cpty_id]
    cpty_positions = [p for p in positions if p.counterparty_id == target_cpty_id]
    exposure = compute_counterparty_exposure(cpty, cpty_positions, as_of=CYCLE_AS_OF.isoformat())
    print(f"\n=== Exposure Snapshot: {cpty.legal_name} ===")
    print(f"  Gross exposure:            ${exposure.gross_exposure_usd:,.0f}")
    print(f"  Net exposure:              ${exposure.net_exposure_usd:,.0f}")
    print(f"  Uncollateralized exposure: ${exposure.uncollateralized_exposure_usd:,.0f}")
    print(f"  Issuer HHI:                {exposure.herfindahl_issuer:.0f}")
    if exposure.wrong_way_risk_flags:
        print("  Wrong-way risk flags:")
        for flag in exposure.wrong_way_risk_flags:
            print(f"    - {flag}")
    print("  Stress results (COMBINED_STRESS):",
          exposure.stress_results["COMBINED_STRESS"])


if __name__ == "__main__":
    main()
