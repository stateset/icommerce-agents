"""JSON-lines logging for the host, with the request id on every record.

``configure_logging`` installs one stderr handler on the root logger. The correlation
middleware sets :data:`request_id_var` for the life of a request, so any record emitted
while serving it -- from the host, the agents, or the engine adapter -- carries the same
``request_id`` the client saw in ``X-Request-Id``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "icommerce_request_id", default=None
)

_HANDLER_NAME = "icommerce-json"


class JsonFormatter(logging.Formatter):
    """One JSON object per line; never a multi-line record, so log shippers stay simple."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON handler once; repeated calls only adjust the level."""
    root = logging.getLogger()
    resolved = (level if level is not None else os.getenv("ICOMMERCE_LOG_LEVEL", "INFO")).upper()
    root.setLevel(resolved)
    for handler in root.handlers:
        if handler.get_name() == _HANDLER_NAME:
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
