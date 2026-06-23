#!/usr/bin/env python3
"""
Intraday "fast cycle" entry point.

Runs the subset of the job graph that doesn't depend on custodian EOD
data: risk, pricing, optimization, recall/buy-in. Skips reconciliation
(custodian feeds are typically T+1 and don't refresh intraday — see
orchestration/scheduler.py module docstring) and corporate actions
(daily refresh is sufficient — CA reference data does not change
intraday). Recommended cadence: every 15 minutes during trading hours,
see docs/runbook.md "Scheduling".

This reuses `orchestration.scheduler.run_full_cycle` with
`book_recon_df`/`custodian_recon_df` left as None, which short-circuits
the reconciliation step inside the cycle (see CycleInputs defaults).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from secfi_platform.common.logging_setup import configure_logging, get_logger, log_with_fields  # noqa: E402
from secfi_platform.orchestration.scheduler import CycleInputs, run_full_cycle  # noqa: E402
from tests._helpers import (  # noqa: E402  (reference dataset loader; see run_daily_batch.py docstring)
    default_counterparty_limits_usd,
    default_substitutes,
    load_corporate_action_events,
    load_counterparties,
    load_fx_rates,
    load_locate_shortages,
    load_market_quotes,
    load_positions,
    load_settlement_fails,
)

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the intraday fast cycle (no reconciliation).")
    parser.add_argument("--as-of", type=str, default=date.today().isoformat())
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)

    configure_logging()
    inputs = CycleInputs(
        as_of=as_of,
        positions=load_positions(),
        counterparties=load_counterparties(),
        market_quotes=load_market_quotes(),
        fx_rates=load_fx_rates(),
        corporate_action_events=load_corporate_action_events(),
        settlement_fails=load_settlement_fails(),
        locate_shortages=load_locate_shortages(),
        substitutes=default_substitutes(),
        counterparty_limits_usd=default_counterparty_limits_usd(),
        book_recon_df=None,
        custodian_recon_df=None,
    )
    outputs = run_full_cycle(inputs)
    log_with_fields(
        logger, 20, "intraday_cycle.complete",
        correlation_id=outputs.correlation_id,
        recon_breaks=len(outputs.recon_breaks),  # always 0 in fast cycle — expected, not a bug
        alerts_raised=len(outputs.alerts),
        top_recall_item=(outputs.recall_queue[0].ticker if outputs.recall_queue else None),
    )
    print(f"Intraday cycle complete. Correlation ID: {outputs.correlation_id}. "
          f"Alerts: {len(outputs.alerts)}. Recall queue size: {len(outputs.recall_queue)}.")


if __name__ == "__main__":
    main()
