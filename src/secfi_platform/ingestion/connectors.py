"""
Reference connector implementations.

These implementations read from local CSV/JSON snapshots (see
tests/fixtures/ for the schema) to keep this repository runnable without
live vendor credentials. Each class documents EXACTLY what a production
implementation must replace:

  EquiLendConnector            -> EquiLend Data & Analytics API (REST) or
                                   SFTP end-of-day file drop; auth via
                                   OAuth2 client credentials, rotated
                                   secret stored in firm secret manager,
                                   referenced via ${EQUILEND_API_KEY} in
                                   configs/prod.yaml.
  DataLendConnector            -> DataLend API; same credential pattern.
  InternalTradeCaptureConnector -> Firm's trade capture system (e.g., a
                                   Kafka topic of trade lifecycle events,
                                   or a direct read-replica DB connection).
                                   This is the single most important feed
                                   and should be event-streamed intraday,
                                   not batch-polled, in production.
  CustodianFeedConnector       -> Custodian SWIFT MT5xx messages or
                                   custodian API (e.g., position/holding
                                   statements); typically T+1 EOD plus
                                   intraday where the custodian supports it.
  MarketDataConnector          -> Firm market data platform (prices, FX,
                                   curves) — typically an internal pricing
                                   service, not a direct exchange feed.
  CorporateActionsFeedConnector -> Firm reference data / corporate actions
                                   system (often sourced from a vendor like
                                   ICE, Bloomberg DRSE, or SIX, normalized
                                   by the firm's reference data team).
  BalanceSheetFeedConnector    -> Firm Treasury/Finance balance-sheet
                                   system extract (often a nightly batch
                                   from the regulatory reporting platform).

All `fetch()` methods below raise DataSourceUnavailableError if the
backing file is missing/unreadable, exercising the same failure path a
live integration would hit on a network/auth failure.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from secfi_platform.ingestion.base import DataSourceUnavailableError, SourceConnector


class _CsvFileConnector(SourceConnector):
    """Shared implementation for file-backed connectors used in this reference build/tests."""

    def __init__(self, source_name: str, file_path: str | Path):
        self.source_name = source_name
        self.file_path = Path(file_path)
        self._last_success: datetime | None = None

    def fetch(self, as_of: datetime) -> list[dict]:
        if not self.file_path.exists():
            raise DataSourceUnavailableError(self.source_name, f"snapshot file not found: {self.file_path}")
        try:
            with open(self.file_path, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as exc:  # noqa: BLE001 — re-raised in canonical connector error type
            raise DataSourceUnavailableError(self.source_name, str(exc)) from exc
        self._last_success = datetime.now(timezone.utc)
        return rows

    def health_check(self) -> bool:
        return self.file_path.exists()

    def last_successful_fetch(self) -> datetime | None:
        return self._last_success


class EquiLendConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("EQUILEND", file_path)


class DataLendConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("DATALEND", file_path)


class InternalTradeCaptureConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("INTERNAL_TRADE_CAPTURE", file_path)


class CustodianFeedConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path, custodian_name: str = "GENERIC_CUSTODIAN"):
        super().__init__(f"CUSTODIAN::{custodian_name}", file_path)


class MarketDataConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("INTERNAL_MARKET_DATA", file_path)


class CorporateActionsFeedConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("CORP_ACTIONS_FEED", file_path)


class BalanceSheetFeedConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("FIRM_BALANCE_SHEET", file_path)


class SettlementFailsConnector(_CsvFileConnector):
    def __init__(self, file_path: str | Path):
        super().__init__("SETTLEMENT_SYSTEM", file_path)
