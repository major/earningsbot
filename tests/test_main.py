"""Tests for the earningsbot command line entry point."""

import functools
import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

import earningsbot
from earningsbot import main
from earningsbot.metrics import Metrics

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

IDENTITY_ENV = "EDGAR_IDENTITY"
STATE_ENV = "EARNINGSBOT_STATE_PATH"
WEBHOOKS_ENV = "DISCORD_EARNINGS_WEBHOOKS"
LOG_LEVEL_ENV = "EARNINGSBOT_LOG_LEVEL"
WEBHOOK_URL = "https://discord.example/api/webhooks/1/secret-token"
USAGE_EXIT_CODE = 2
FAILED_CYCLE_EXIT_CODE = 1
RETRY_POST_COUNT = 2
EXPECTED_LINE = "2026-11-18  Acme Corp  CIK 320193  0001-26-000001  https://www.sec.gov/index.html\n"


def _records(text: str) -> list[dict[str, Any]]:
    """🧪 Parse each stderr line as one JSON log record. Any other line fails the test."""
    return [json.loads(line) for line in text.splitlines()]


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
        index_headers=SimpleNamespace(items=["2.02"]),
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    identities: list[str],
    feed: list[dict[str, object]],
) -> None:
    """With --once, main sets the identity, prints one line, and quiets httpx2."""
    # Arrange: edgartools sets this level on import, so reset it to prove main sets it.
    httpx_logger = logging.getLogger("httpx2")
    monkeypatch.setattr(httpx_logger, "level", logging.NOTSET)

    # Act
    main(["--once"])

    # Assert
    assert identities == ["Test User test@example.com"]
    assert feed == [{"form": "8-K", "page_size": 100}]
    assert capsys.readouterr().out == EXPECTED_LINE
    assert httpx_logger.level == logging.WARNING


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
    # 📊 Stub the metrics lookup so no test reads a real release from EDGAR.
    monkeypatch.setattr(earningsbot, "fetch_metrics", lambda _filing: Metrics())
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


@pytest.mark.parametrize(
    "webhook",
    ["not a url", "ftp://h/x", "https://[::1/x", "https://a b/x"],
)
@pytest.mark.usefixtures("identities")
def test_main_exits_2_on_invalid_webhook_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    feed: list[dict[str, object]],
    webhook: str,
) -> None:
    """A bad webhook exits 2 before polling, and its URL is never printed."""
    # Arrange
    monkeypatch.setenv(WEBHOOKS_ENV, f"https://a/x,{webhook}")
    posts = _patch_discord(monkeypatch, iter([]))

    # Act
    with pytest.raises(SystemExit) as exc_info:
        main(["--once"])

    # Assert
    err = capsys.readouterr().err
    assert exc_info.value.code == USAGE_EXIT_CODE
    assert err == "error: DISCORD_EARNINGS_WEBHOOKS entry 2 is not a valid http(s) URL\n"
    assert feed == []
    assert posts == []


@pytest.mark.usefixtures("identities", "feed")
def test_main_permanent_webhook_failure_does_not_repeat(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deleted webhook is warned about once, and the filing is still marked seen."""
    # Arrange
    monkeypatch.setenv(WEBHOOKS_ENV, "https://a/x,https://b/y")
    posts = _patch_discord(monkeypatch, iter([204, 404]))

    # Act
    main(["--once"])
    first = capsys.readouterr()
    main(["--once"])
    second = capsys.readouterr()

    # Assert
    skipped = [record for record in _records(first.err) if record["msg"] == "webhook skipped"]
    assert first.out == EXPECTED_LINE
    assert [(record["webhook"], record["reason"]) for record in skipped] == [
        (2, "post webhook: unexpected status 404"),
    ]
    assert [str(post.url) for post in posts] == ["https://a/x", "https://b/y"]
    assert second.out == ""
    assert {record["level"] for record in _records(second.err)} == {"info"}


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
    assert {record["level"] for record in _records(second.err)} == {"info"}


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        pytest.param(None, logging.INFO, id="unset"),
        pytest.param("", logging.INFO, id="blank"),
        pytest.param("   ", logging.INFO, id="spaces"),
        pytest.param("debug", logging.DEBUG, id="lowercase"),
        pytest.param(" Error ", logging.ERROR, id="mixed-case-padded"),
    ],
)
@pytest.mark.usefixtures("identities", "feed")
def test_main_reads_log_level_from_env(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    expected: int,
) -> None:
    """EARNINGSBOT_LOG_LEVEL sets the root level. Unset or blank means INFO, and case does not matter."""
    if env_value is None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    else:
        monkeypatch.setenv(LOG_LEVEL_ENV, env_value)

    main(["--once"])

    assert logging.getLogger().level == expected


@pytest.mark.usefixtures("identities")
def test_main_exits_2_on_invalid_log_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    feed: list[dict[str, object]],
) -> None:
    """An unknown log level is a usage error: exit 2 with one plain line, and no polling."""
    monkeypatch.setenv(LOG_LEVEL_ENV, "verbose")

    with pytest.raises(SystemExit) as exc_info:
        main(["--once"])

    assert exc_info.value.code == USAGE_EXIT_CODE
    assert capsys.readouterr().err == (
        "error: EARNINGSBOT_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL\n"
    )
    assert feed == []


@pytest.mark.usefixtures("identities", "feed")
def test_main_once_logs_json_lines_without_webhook_urls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stderr holds only JSON log lines for the poll events, and no webhook URL or token appears."""
    # Arrange
    monkeypatch.setenv(WEBHOOKS_ENV, WEBHOOK_URL)
    _patch_discord(monkeypatch, iter([204]))

    # Act
    main(["--once"])

    # Assert
    err = capsys.readouterr().err
    by_msg = {record["msg"]: record for record in _records(err)}
    assert by_msg["earnings filing found"]["accession_no"] == "0001-26-000001"
    assert by_msg["poll cycle finished"]["earnings"] == 1
    assert by_msg["discord post sent"]["webhook"] == 1
    assert "secret-token" not in err
    assert "discord.example" not in err
