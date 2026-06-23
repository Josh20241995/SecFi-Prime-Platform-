"""
Cycle output store.

*** PRODUCTION NOTE ***
This is an in-process, in-memory store used so the API layer has
something to serve in this reference build and in integration tests. A
real deployment replaces this with reads against the persisted SQL
tables (sql/schemas/) populated by the orchestration cycle, typically
fronted by a short-TTL Redis cache for the hot "latest" view to keep
desk-facing dashboard latency low without hammering the OLTP/analytics
database on every page load. See docs/architecture.md "API Layer".

Using a simple module-level singleton here (not thread-safe, not
multi-process-safe) is an explicit, documented simplification — flagged
per skill governance requirement to never silently imply production
readiness. docs/assumptions_and_limitations.md item ARCH-1.
"""

from __future__ import annotations

from threading import Lock
from typing import Optional

from secfi_platform.orchestration.scheduler import CycleOutputs

_lock = Lock()
_latest: Optional[CycleOutputs] = None


def set_latest_cycle_outputs(outputs: CycleOutputs) -> None:
    global _latest
    with _lock:
        _latest = outputs


def get_latest_cycle_outputs() -> Optional[CycleOutputs]:
    with _lock:
        return _latest
