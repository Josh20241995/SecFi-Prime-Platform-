"""
Structured logging setup.

All log lines are JSON with a `correlation_id` so a single batch run or
intraday cycle can be traced end-to-end across ingestion -> normalization
-> risk -> optimization -> pricing -> reporting, and joined against the
audit log in SQL (see sql/schemas/08_audit_log.sql). This is what makes
"replay tooling for reconstructing critical incidents" (skill section 15)
actually possible: every engine run is reconstructable from its
correlation_id, its input data vintage, and its config snapshot hash.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

_correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="UNSET")


def new_correlation_id() -> str:
    cid = f"secfi-{uuid.uuid4().hex[:16]}"
    _correlation_id_ctx.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id_ctx.get()


def set_correlation_id(cid: str) -> None:
    _correlation_id_ctx.set(cid)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with_fields(logger: logging.Logger, level: int, message: str, **fields) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
