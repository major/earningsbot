"""Tests for the earningsbot command line entry point."""

import functools
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest

import earningsbot
from earningsbot import main

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

IDENTITY_ENV = "EDGAR_IDENTITY"
STATE_ENV = "EARNINGSBOT_STATE_PATH"
WEBHOOKS_ENV = "DISCORD_EARNINGS_WEBHOOKS"
USAGE_EXIT_CODE = 2
FAILED_CYCLE_EXIT_CODE = 1
RETRY_POST_COUNT = 2
EXPECTED_LINE = (
    "2026-11-18  Acme Corp  CIK 320193  0001-26-000001  "
    "https://www.sec.gov/index.html\n"
)


@pytest.fixture(autouse=True)
def state_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """🧪 Point the seen store at a temp file so no test writes into the project."""
    path = tmp_path / "state" / "seen.sqlite3"
    monkeypatch.setenv(STATE_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def no_discord_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """🧪 Clear the webhook variable so no test can post to Discord."""
    monkeypatch.delenv(WEBHOOKS_ENV, raising=False)


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


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """🧪 Serve one earnings filing from a fake EDGAR feed and record each request."""
    calls: list[dict[str, object]] = []
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
        calls.append(kwargs)
        return [filing]

    monkeypatch.setattr(earningsbot, "get_current_filings", fake_get_current_filings)
    return calls


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
    capsys: pytest.CaptureFixture[str],
    identities: list[str],
    feed: list[dict[str, object]],
) -> None:
    """With --once, main sets the identity, prints one line, and returns normally."""
    main(["--once"])

    assert identities == ["Test User test@example.com"]
    assert feed == [{"form": "8-K", "page_size": 100}]
    assert capsys.readouterr().out == EXPECTED_LINE


@pytest.mark.usefixtures("identities", "feed")
def test_main_second_once_run_skips_seen_filings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A filing printed by one --once run is not printed again by the next run."""
    main(["--once"])
    first_output = capsys.readouterr().out

    main(["--once"])
    second_output = capsys.readouterr().out

    assert first_output == EXPECTED_LINE
    assert second_output == ""


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.usefixtures("identities", "feed")
def test_main_uses_default_state_path_when_blank(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    blank: str,
) -> None:
    """A blank EARNINGSBOT_STATE_PATH uses .earningsbot/ under the cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(STATE_ENV, blank)

    main(["--once"])

    assert (tmp_path / ".earningsbot" / "seen.sqlite3").is_file()


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


def _patch_discord(
    monkeypatch: pytest.MonkeyPatch,
    statuses: Iterator[int],
) -> list[httpx2.Request]:
    """🧪 Send Discord posts to a mock transport that answers with each status."""
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and answer with the next status."""
        posts.append(request)
        return httpx2.Response(next(statuses))

    # 🧪 The partial is built before setattr, so it wraps the real Client class.
    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        httpx2,
        "Client",
        functools.partial(httpx2.Client, transport=transport),
    )
    monkeypatch.setattr(earningsbot, "find_ticker", lambda _cik: "ACME")
    return posts


@pytest.mark.usefixtures("identities", "feed")
def test_main_posts_to_discord_when_webhooks_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each configured webhook gets one post, and the stdout line is still printed."""
    # Arrange
    monkeypatch.setenv(WEBHOOKS_ENV, " https://a/x , ,https://b/y ")
    posts = _patch_discord(monkeypatch, iter([204, 204]))

    # Act
    main(["--once"])

    # Assert
    assert [str(post.url) for post in posts] == ["https://a/x", "https://b/y"]
    assert json.loads(posts[0].content)["embeds"][0]["title"] == "ACME: Acme Corp"
    assert capsys.readouterr().out == EXPECTED_LINE


@pytest.mark.usefixtures("identities", "feed")
def test_main_discord_failure_retries_next_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed post warns and exits 1, and the next run posts the same filing again."""
    # Arrange
    monkeypatch.setenv(WEBHOOKS_ENV, "https://a/x")
    posts = _patch_discord(monkeypatch, iter([500, 204]))

    # Act
    with pytest.raises(SystemExit) as exc_info:
        main(["--once"])
    first = capsys.readouterr()
    main(["--once"])
    second = capsys.readouterr()

    # Assert
    assert exc_info.value.code == FAILED_CYCLE_EXIT_CODE
    assert "unexpected status 500" in first.err
    assert len(posts) == RETRY_POST_COUNT
    assert second.out == EXPECTED_LINE
    assert second.err == ""
