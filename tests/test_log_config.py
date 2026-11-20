"""Tests for the JSON Lines formatter and the central logging setup."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from earningsbot.log_config import HANDLER_NAME, JsonFormatter, configure_logging

# 🕙 A quarter second is exact in binary, so the millisecond part stays exact.
RECORD_TIME = datetime(2026, 11, 19, 10, tzinfo=UTC).timestamp() + 0.25
WEBHOOK_NUMBER = 2


def _record(msg: str, **extra: Any) -> logging.LogRecord:  # noqa: ANN401 - extra fields hold any value
    """🧪 Build an INFO record at a fixed time, with extra fields set like logger.info(extra=...) does."""
    record = logging.LogRecord("earningsbot.test", logging.INFO, __file__, 1, msg, (), None)
    record.created = RECORD_TIME
    vars(record).update(extra)
    return record


def test_json_formatter_writes_one_line_with_fixed_keys_in_order() -> None:
    """Each record is one JSON object, with ts, level, logger, and msg first in that order."""
    # Arrange
    record = _record("hello world")

    # Act
    line = JsonFormatter().format(record)

    # Assert
    assert "\n" not in line
    assert list(json.loads(line)) == ["ts", "level", "logger", "msg"]
    assert json.loads(line) == {
        "ts": "2026-11-19T10:00:00.250Z",
        "level": "info",
        "logger": "earningsbot.test",
        "msg": "hello world",
    }


def test_json_formatter_adds_extra_fields_as_flat_keys() -> None:
    """Extra fields become top-level keys, and values JSON cannot encode are stringified."""
    # Arrange
    record = _record("earnings filing found", accession_no="0001-26-000001", webhook=2, tags={"a"})
    # 🧩 Another handler may format the record first and set record.message, which must not leak out.
    logging.Formatter().format(record)

    # Act
    payload = json.loads(JsonFormatter().format(record))

    # Assert
    assert list(payload) == ["ts", "level", "logger", "msg", "accession_no", "webhook", "tags"]
    assert payload["webhook"] == WEBHOOK_NUMBER
    assert payload["tags"] == "{'a'}"


def _fail_with_boom() -> None:
    """🧪 Raise a ValueError so a test can capture a real traceback."""
    msg = "boom"
    raise ValueError(msg)


def test_json_formatter_adds_exc_last_when_exc_info_is_set() -> None:
    """A record with exc_info gets one exc key at the end, and the line stays on one line."""
    # Arrange
    record = _record("poll cycle failed")
    with pytest.raises(ValueError, match="boom") as excinfo:
        _fail_with_boom()
    record.exc_info = (excinfo.type, excinfo.value, excinfo.tb)

    # Act
    line = JsonFormatter().format(record)

    # Assert
    payload = json.loads(line)
    assert list(payload)[-1] == "exc"
    assert "ValueError: boom" in payload["exc"]
    assert "\n" not in line


@pytest.mark.parametrize(
    ("level", "expected"),
    [("DEBUG", logging.DEBUG), (logging.WARNING, logging.WARNING)],
)
def test_configure_logging_sets_root_level(level: str | int, expected: int) -> None:
    """The level argument becomes the root level, as a name or a number."""
    configure_logging(level)

    assert logging.getLogger().level == expected


def test_configure_logging_replaces_its_own_handler_on_repeat_calls() -> None:
    """Calling configure_logging again swaps the handler, so only one of ours stays attached."""
    configure_logging()
    configure_logging()

    ours = [handler for handler in logging.getLogger().handlers if handler.name == HANDLER_NAME]
    assert len(ours) == 1


def test_configure_logging_keeps_httpx2_at_warning_even_when_debugging(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx2 logs webhook URLs at INFO, so it stays at WARNING even when DEBUG is requested."""
    # Arrange
    httpx_logger = logging.getLogger("httpx2")
    monkeypatch.setattr(httpx_logger, "level", logging.DEBUG)

    # Act
    configure_logging("DEBUG")

    # Assert
    assert httpx_logger.level == logging.WARNING
