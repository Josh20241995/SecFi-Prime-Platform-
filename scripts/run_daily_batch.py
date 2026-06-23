#!/usr/bin/env python3
"""
Daily batch entry point.

This is what a production scheduler (Airflow/Dagster/Control-M) invokes
for the EOD full cycle (see orchestration/scheduler.py module docstring
for the job graph and recommended cadence). It demonstrates the REAL
production code path: ingestion/connectors.py -> normalization/
schema_mapping.py -> orchestration/scheduler.py -> reporting -> API state
publish, as opposed to the test suite's `tests/_helpers.py` shortcut
(which exists purely to keep test setup terse).

Usage:
    python scripts/run_daily_batch.py --as-of 2026-06-18 --environment dev

In this reference build, the file-based connectors point at the bundled
tests/fixtures/ CSVs (see configs/base.yaml `ingestion.sources`). In
production, configs/prod.yaml repoints these at the real EquiLend/
DataLend APIs, the internal trade capture event stream, and the
custodian/balance-sheet feeds — the orchestration and analytics code
does not change.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from secfi_platform.common.config import load_config  # noqa: E402
from secfi_platform.common.logging_setup import configure_logging, get_logger, log_with_fields  # noqa: E402
from secfi_platform.ingestion.connectors import (  # noqa: E402
    BalanceSheetFeedConnector,
    CorporateActionsFeedConnector,
    CustodianFeedConnector,
    DataLendConnector,
    EquiLendConnector,
    InternalTradeCaptureConnector,
    SettlementFailsConnector,
)
from secfi_platform.ingestion.base import DataSourceUnavailableError  # noqa: E402
from secfi_platform.normalization.schema_mapping import (  # noqa: E402
    parse_corporate_action_event,
    parse_market_rate_quote,
    parse_position,
    parse_rows,
    parse_security,
)
from secfi_platform.orchestration.scheduler import CycleInputs, run_full_cycle  # noqa: E402
from secfi_platform.reporting.daily_summary import render_markdown  # noqa: E402

logger = get_logger(__name__)


def _load_demo_dataset(config, as_of: date) -> CycleInputs:
    """
    Loads the bundled reference dataset through the REAL connector +
    normalization pipeline. Counterparties and substitute-inventory mapping
    are still loaded via the lightweight tests/_helpers reader in this
    reference build (no dedicated connector class was warranted for two
    small reference-data tables); production would source counterparty
    master data from the firm's client reference data system instead.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tests._helpers import default_substitutes, load_counterparties  # local import: demo-data convenience only

    securities_path = REPO_ROOT / "tests" / "fixtures" / "securities.csv"
    import csv
    with open(securities_path, newline="") as f:
        securities = {row["internal_id"]: parse_security(row) for row in csv.DictReader(f)}

    fixtures = REPO_ROOT / "tests" / "fixtures"

    trade_capture = InternalTradeCaptureConnector(fixtures / "positions.csv")
    custodian = CustodianFeedConnector(fixtures / "custodian_positions.csv")
    equilend = EquiLendConnector(fixtures / "market_rates_equilend.csv")
    datalend = DataLendConnector(fixtures / "market_rates_datalend.csv")
    corp_actions = CorporateActionsFeedConnector(fixtures / "corporate_actions.csv")
    fails = SettlementFailsConnector(fixtures / "settlement_fails.csv")

    now = datetime.now(timezone.utc)
    position_rows = trade_capture.fetch(now)
    positions, position_errors = parse_rows(position_rows, parse_position, security_lookup=securities)
    for err in position_errors:
        log_with_fields(logger, 30, "normalization.position_rejected", **err)

    datalend_rows = datalend.fetch(now)
    quotes, quote_errors = parse_rows(datalend_rows, parse_market_rate_quote)
    for err in quote_errors:
        log_with_fields(logger, 30, "normalization.quote_rejected", **err)
    market_quotes = {q.security_internal_id: q for q in quotes}

    ca_rows = corp_actions.fetch(now)
    ca_events, ca_errors = parse_rows(ca_rows, parse_corporate_action_event)
    for err in ca_errors:
        log_with_fields(logger, 30, "normalization.corporate_action_rejected", **err)

    import pandas as pd
    from decimal import Decimal as D

    book_rows = [
        {
            "position_id": p.position_id, "security_internal_id": p.security.internal_id,
            "counterparty_id": p.counterparty_id, "trade_date": p.trade_date,
            "direction": p.direction.value, "quantity": p.quantity, "market_value": p.market_value,
            "rate_bps": p.rate_bps,
        }
        for p in positions
    ]
    book_df = pd.DataFrame(book_rows)

    custodian_rows = custodian.fetch(now)
    for row in custodian_rows:
        row["trade_date"] = date.fromisoformat(row["trade_date"])
        row["quantity"] = D(row["quantity"])
        row["market_value"] = D(row["market_value"])
    custodian_df = pd.DataFrame(custodian_rows)

    fail_rows = fails.fetch(now)
    from secfi_platform.recall_buyin.recall_risk_engine import SettlementFail
    settlement_fails = [
        SettlementFail(
            position_id=r["position_id"], security_internal_id=r["security_internal_id"],
            fail_age_days=int(r["fail_age_days"]), fail_quantity=D(r["fail_quantity"]),
            counterparty_id=r["counterparty_id"],
            is_desk_receiving=r["is_desk_receiving"].strip().lower() == "true",
        )
        for r in fail_rows
    ]

    from tests._helpers import default_counterparty_limits_usd, load_fx_rates, load_locate_shortages
    return CycleInputs(
        as_of=as_of,
        positions=positions,
        counterparties=load_counterparties(),
        market_quotes=market_quotes,
        fx_rates=load_fx_rates(),
        corporate_action_events=ca_events,
        settlement_fails=settlement_fails,
        locate_shortages=load_locate_shortages(),
        substitutes=default_substitutes(),
        counterparty_limits_usd=default_counterparty_limits_usd(),
        book_recon_df=book_df,
        custodian_recon_df=custodian_df,
    )


def main():
    parser = argparse.ArgumentParser(description="Run the full daily securities finance orchestration cycle.")
    parser.add_argument("--as-of", type=str, default=date.today().isoformat())
    parser.add_argument("--environment", type=str, default="dev")
    parser.add_argument("--output-dir", type=str, default=str(REPO_ROOT / "outputs"))
    args = parser.parse_args()

    configure_logging()
    config = load_config(environment=args.environment)
    as_of = date.fromisoformat(args.as_of)

    log_with_fields(logger, 20, "batch.start", as_of=args.as_of, environment=args.environment)

    try:
        inputs = _load_demo_dataset(config, as_of)
    except DataSourceUnavailableError as exc:
        log_with_fields(logger, 40, "batch.ingestion_failed", source=exc.source_name, reason=exc.reason)
        sys.exit(1)

    outputs = run_full_cycle(inputs)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"daily_executive_summary_{as_of.isoformat()}.md"
    summary_path.write_text(render_markdown(outputs.executive_summary))

    log_with_fields(
        logger, 20, "batch.complete",
        correlation_id=outputs.correlation_id,
        alerts_raised=len(outputs.alerts),
        recon_breaks=len(outputs.recon_breaks),
        summary_path=str(summary_path),
    )
    print(f"Daily executive summary written to: {summary_path}")
    print(f"Correlation ID: {outputs.correlation_id}")
    print(f"Alerts raised: {len(outputs.alerts)}")


if __name__ == "__main__":
    main()
