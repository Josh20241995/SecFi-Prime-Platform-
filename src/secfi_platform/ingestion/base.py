"""
Connector interface contract.

Every external data source (EquiLend, DataLend, internal trade capture,
custodian feeds, market data, corporate actions feeds, firm balance-sheet
feeds) implements this interface. This is the seam at which a real
deployment swaps the mock/file-based implementations in
`ingestion/connectors.py` for actual SFTP pulls, REST/FIX/swift
messages, or internal message-bus subscriptions, WITHOUT touching any
downstream analytics code — every engine in this platform consumes
canonical objects from `common/types.py`, never raw vendor payloads.

Fallback contract: every connector must implement `health_check()` and
must raise `DataSourceUnavailableError` (not a bare exception) when it
cannot fetch fresh data, so the orchestration layer can apply the
documented fallback policy (use last-known-good snapshot, flag
DataQualityFlag.STALE, and raise an Alert) rather than crash the entire
batch/intraday cycle. See orchestration/scheduler.py `_run_with_fallback`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Generic, TypeVar

T = TypeVar("T")


class DataSourceUnavailableError(Exception):
    def __init__(self, source_name: str, reason: str):
        self.source_name = source_name
        self.reason = reason
        super().__init__(f"Data source '{source_name}' unavailable: {reason}")


class DataQualityError(Exception):
    """Raised by normalization-layer validators when a raw record fails required checks."""
    def __init__(self, field_errors: list[str]):
        self.field_errors = field_errors
        super().__init__("; ".join(field_errors))


class SourceConnector(ABC, Generic[T]):
    source_name: str

    @abstractmethod
    def fetch(self, as_of: datetime) -> list[T]:
        """Fetch raw records as of the given timestamp. Raises DataSourceUnavailableError on failure."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def last_successful_fetch(self) -> datetime | None:
        raise NotImplementedError
