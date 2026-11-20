"""Central logging setup: one JSON object per line on stderr.

📜 Only this module attaches handlers. Other modules call getLogger(__name__).
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

HANDLER_NAME = "earningsbot-json"
# 🧱 A blank record has every standard attribute. Formatter.format adds message and asctime.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


def _timestamp(created: float) -> str:
    """Return a UTC ISO 8601 timestamp with milliseconds and a Z suffix."""
    moment = datetime.fromtimestamp(created, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """🧾 Format each record as one JSON object with a fixed key order."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as one JSON line: ts, level, logger, msg, extra fields, then exc."""
        entry: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS:
                # 🛡️ setdefault keeps the fixed keys, so an extra named msg cannot replace them.
                entry.setdefault(key, value)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # 🧾 Compact separators keep each line short, as in the Discord payloads.
        # 🧩 default=str keeps an odd value from crashing the log call.
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str | int = logging.INFO) -> None:
    """Send root logs to stderr as JSON Lines at the given level. Calling it again replaces the handler."""
    root = logging.getLogger()
    # 🔁 Remove the handler from an earlier call, so repeat calls never print each line twice.
    for handler in list(root.handlers):
        if handler.name == HANDLER_NAME:
            root.removeHandler(handler)
    # 🧭 sys.stderr is read now, so a stream swapped in before this call receives the output.
    handler = logging.StreamHandler(sys.stderr)
    handler.name = HANDLER_NAME
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # 🔇 httpx2 logs full request URLs at INFO, and those URLs carry the webhook tokens.
    # Keep it at WARNING even when DEBUG is requested.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
