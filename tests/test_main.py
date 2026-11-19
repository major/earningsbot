"""Tests for the earningsbot command line entry point."""

from types import SimpleNamespace

import pytest

import earningsbot
from earningsbot import main

IDENTITY_ENV = "EDGAR_IDENTITY"
USAGE_EXIT_CODE = 2
FAILED_CYCLE_EXIT_CODE = 1


@pytest.fixture
def identities(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """🧪 Set a valid identity, record set_identity calls, and forbid real pauses."""
    recorded: list[str] = []

    def no_sleep(seconds: float) -> None:
        """🧪 Fail if --once ever pauses, so a bug cannot hang the test run."""
        msg = f"--once must not sleep, got {seconds}"
        raise AssertionError(msg)

    monkeypatch.setenv(IDENTITY_ENV, "Test User test@example.com")
    monkeypatch.setattr(earningsbot, "set_identity", recorded.append)
    monkeypatch.setattr(earningsbot.time, "sleep", no_sleep)
    return recorded


@pytest.mark.parametrize("identity", [None, "", "   "])
def test_main_exits_2_without_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    identity: str | None,
) -> None:
    """A missing or blank EDGAR_IDENTITY stops with a stderr message and exit code 2."""
    if identity is None:
        monkeypatch.delenv(IDENTITY_ENV, raising=False)
    else:
        monkeypatch.setenv(IDENTITY_ENV, identity)

    with pytest.raises(SystemExit) as exc_info:
        main([])

    assert exc_info.value.code == USAGE_EXIT_CODE
    assert IDENTITY_ENV in capsys.readouterr().err


def test_main_once_sets_identity_and_polls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    identities: list[str],
) -> None:
    """With --once, main sets the identity, prints one line, and returns normally."""
    feed_calls: list[dict[str, object]] = []
    filing = SimpleNamespace(
        accession_no="0001-26-000001",
        company="Acme Corp",
        cik=320193,
        filing_date="2026-11-18",
        homepage_url="https://www.sec.gov/index.html",
        obj=lambda: SimpleNamespace(items=["Item 2.02"]),
    )

    def fake_get_current_filings(**kwargs: object) -> list[SimpleNamespace]:
        """🧪 Record the feed request and return one earnings filing."""
        feed_calls.append(kwargs)
        return [filing]

    monkeypatch.setattr(earningsbot, "get_current_filings", fake_get_current_filings)

    main(["--once"])

    assert identities == ["Test User test@example.com"]
    assert feed_calls == [{"form": "8-K", "page_size": 100}]
    assert capsys.readouterr().out == (
        "2026-11-18  Acme Corp  CIK 320193  0001-26-000001  "
        "https://www.sec.gov/index.html\n"
    )


@pytest.mark.usefixtures("identities")
def test_main_once_exits_1_when_cycle_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --once, a failed cycle warns on stderr and exits with code 1."""

    def failing_feed(**_kwargs: object) -> list[SimpleNamespace]:
        """🧪 Simulate an unreachable SEC feed."""
        msg = "feed down"
        raise OSError(msg)

    monkeypatch.setattr(earningsbot, "get_current_filings", failing_feed)

    with pytest.raises(SystemExit) as exc_info:
        main(["--once"])

    assert exc_info.value.code == FAILED_CYCLE_EXIT_CODE
    assert "feed down" in capsys.readouterr().err
