"""
Shared fixture loading helpers for tests.

Centralizes the "read CSV -> parse into canonical objects" wiring so
individual test modules stay focused on assertions, not plumbing. This
mirrors the real ingestion -> normalization pipeline (ingestion/connectors.py
+ normalization/schema_mapping.py) at a smaller scale.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from secfi_platform.common.types import Security
from secfi_platform.normalization.schema_mapping import (
    parse_corporate_action_event,
    parse_counterparty,
    parse_fx_rate,
    parse_market_rate_quote,
    parse_position,
    parse_security,
)
from secfi_platform.recall_buyin.recall_risk_engine import LocateShortage, SettlementFail, SubstituteInventory

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _read_csv(name: str) -> list[dict]:
    path = FIXTURES_DIR / name
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_securities() -> dict[str, Security]:
    rows = _read_csv("securities.csv")
    return {row["internal_id"]: parse_security(row) for row in rows}


def load_counterparties() -> dict:
    rows = _read_csv("counterparties.csv")
    return {row["counterparty_id"]: parse_counterparty(row) for row in rows}


def load_positions() -> list:
    securities = load_securities()
    rows = _read_csv("positions.csv")
    return [parse_position(row, securities) for row in rows]


def load_market_quotes(source_file: str = "market_rates_datalend.csv") -> dict:
    rows = _read_csv(source_file)
    quotes = [parse_market_rate_quote(row) for row in rows]
    return {q.security_internal_id: q for q in quotes}


def load_corporate_action_events() -> list:
    rows = _read_csv("corporate_actions.csv")
    return [parse_corporate_action_event(row) for row in rows]


def load_fx_rates() -> dict:
    rows = _read_csv("fx_rates.csv")
    rates = [parse_fx_rate(row) for row in rows]
    return {r.base_ccy: r for r in rates}


def load_settlement_fails() -> list:
    rows = _read_csv("settlement_fails.csv")
    return [
        SettlementFail(
            position_id=row["position_id"],
            security_internal_id=row["security_internal_id"],
            fail_age_days=int(row["fail_age_days"]),
            fail_quantity=Decimal(row["fail_quantity"]),
            counterparty_id=row["counterparty_id"],
            is_desk_receiving=row["is_desk_receiving"].strip().lower() == "true",
        )
        for row in rows
    ]


def load_locate_shortages() -> list:
    rows = _read_csv("locate_shortages.csv")
    return [
        LocateShortage(
            security_internal_id=row["security_internal_id"],
            requested_quantity=Decimal(row["requested_quantity"]),
            available_quantity=Decimal(row["available_quantity"]),
        )
        for row in rows
    ]


def default_substitutes() -> dict:
    """SEC003 (GME) and SEC002 (TSLA) substitute candidates for testing the recall queue's substitute path."""
    return {
        "SEC003": SubstituteInventory(security_internal_id="SEC003", substitute_security_ids=()),
        "SEC002": SubstituteInventory(security_internal_id="SEC002", substitute_security_ids=("SEC002B",)),
    }


def load_book_recon_df() -> pd.DataFrame:
    positions = load_positions()
    rows = [
        {
            "position_id": p.position_id,
            "security_internal_id": p.security.internal_id,
            "counterparty_id": p.counterparty_id,
            "trade_date": p.trade_date,
            "direction": p.direction.value,
            "quantity": p.quantity,
            "market_value": p.market_value,
            "rate_bps": p.rate_bps,
        }
        for p in positions
    ]
    return pd.DataFrame(rows)


def load_custodian_recon_df() -> pd.DataFrame:
    rows = _read_csv("custodian_positions.csv")
    for row in rows:
        row["trade_date"] = date.fromisoformat(row["trade_date"])
        row["quantity"] = Decimal(row["quantity"])
        row["market_value"] = Decimal(row["market_value"])
    return pd.DataFrame(rows)


def default_counterparty_limits_usd() -> dict:
    return {
        "CPTY001": Decimal("60000000"),
        "CPTY002": Decimal("250000000"),
        "CPTY003": Decimal("50000000"),
        "CPTY004": Decimal("5000000"),     # deliberately tight to exercise limit-breach path for watch-list name
        "CPTY005": Decimal("100000000"),
    }


CYCLE_AS_OF = date(2026, 6, 18)
