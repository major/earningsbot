"""Shared pytest fixtures."""

import logging
from typing import TYPE_CHECKING

import pytest

from earningsbot.log_config import HANDLER_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """🧪 Remove the JSON handler and restore the root level after each test.

    A handler left behind would keep writing to a capture stream that pytest
    has already closed, so later tests would print errors. 🧹
    """
    root = logging.getLogger()
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler.name == HANDLER_NAME:
            root.removeHandler(handler)
    root.setLevel(level)
