"""Tests for the earningsbot entry point."""

from typing import TYPE_CHECKING

from earningsbot import main

if TYPE_CHECKING:
    import pytest


def test_main_prints_greeting(capsys: pytest.CaptureFixture[str]) -> None:
    """Calling main writes the greeting to stdout."""
    main()

    assert capsys.readouterr().out == "Hello from earningsbot!\n"
