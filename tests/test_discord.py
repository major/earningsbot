"""Tests for Discord embed payloads and webhook delivery."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from earningsbot import discord
from earningsbot.discord import earnings_payload, notify_discord, send_payload
from earningsbot.log_config import JsonFormatter
from earningsbot.metrics import Metrics

if TYPE_CHECKING:
    from collections.abc import Callable

    from earningsbot.discord import WebhookPayload

STOCK_LOGO = "https://logos.example/%s.webp"
ACME_LOGO = "https://logos.example/acme.webp"
TRANSPARENT_PNG = "https://images.example/transparent.png"
WEBHOOK_A = "https://discord.example/api/webhooks/1/token-a"
WEBHOOK_B = "https://discord.example/api/webhooks/2/token-b"
ACCESSION_NO = "0001-26-000001"
TICKER = "ACME"
LONG_COMPANY = "A" * 300
LONG_TITLE = "A" * 255 + "…"
EARNINGS_DESCRIPTION = "📄 Earnings release filed"


@dataclass
class FakeIndexHeaders:
    """🧪 Stand-in for the index-headers page, which the Discord code never reads."""

    items: list[str]


@dataclass
class FakeFiling:
    """🧪 Stand-in for an edgartools Filing with the fields Discord code reads."""

    accession_no: str = "0001-26-000001"
    company: str = "Acme Corp"
    cik: int = 320193
    filing_date: str = "2026-11-18"
    homepage_url: str = "https://www.sec.gov/index.html"
    items: list[str] = field(default_factory=lambda: ["Item 2.02"])

    @property
    def index_headers(self) -> FakeIndexHeaders:
        """🧪 Return the items without the "Item " prefix, as the index-headers page lists them."""
        return FakeIndexHeaders(items=[item.removeprefix("Item ") for item in self.items])


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.Client:
    """🧪 Build a client whose requests go to handler instead of the network."""
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _warnings(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """🧪 Return each WARNING record as the JSON object it prints as."""
    records = [json.loads(JsonFormatter().format(record)) for record in caplog.records]
    return [record for record in records if record["level"] == "warning"]


@pytest.mark.parametrize(
    ("metrics", "description"),
    [
        pytest.param(
            Metrics(revenue=19_589_000.0, eps=-1.06, eps_basis="diluted"),
            "📄 Earnings release filed\n📊 EPS: -$1.06 (diluted)\n📊 Revenue: $19.6M",
            id="full",
        ),
        pytest.param(
            Metrics(revenue=44_848_000.0),
            "📄 Earnings release filed\n📊 Revenue: $44.8M",
            id="revenue-only",
        ),
        pytest.param(
            Metrics(eps=-0.11, eps_basis="basic"),
            "📄 Earnings release filed\n📊 EPS: -$0.11 (basic)",
            id="eps-only",
        ),
        pytest.param(None, EARNINGS_DESCRIPTION, id="no-metrics"),
    ],
)
def test_earnings_payload_with_metrics(
    metrics: Metrics | None,
    description: str,
) -> None:
    """The first line stays fixed, and known EPS and revenue lines follow it."""
    # Arrange
    filing = FakeFiling()

    # Act
    payload = earnings_payload(
        filing,
        "ACME",
        stock_logo=STOCK_LOGO,
        transparent_png=TRANSPARENT_PNG,
        metrics=metrics,
    )

    # Assert
    assert payload["embeds"][0]["description"] == description


@pytest.mark.parametrize(
    ("ticker", "company", "title", "thumbnail"),
    [
        ("ACME", "Acme Corp", "ACME: Acme Corp", ACME_LOGO),
        ("", "Acme Corp", "Acme Corp", TRANSPARENT_PNG),
        ("", LONG_COMPANY, LONG_TITLE, TRANSPARENT_PNG),
    ],
)
def test_earnings_payload(
    ticker: str,
    company: str,
    title: str,
    thumbnail: str,
) -> None:
    """One embed with the title, link, logos, and fixed description for the filing."""
    # Arrange
    filing = FakeFiling(company=company)

    # Act
    payload = earnings_payload(
        filing,
        ticker,
        stock_logo=STOCK_LOGO,
        transparent_png=TRANSPARENT_PNG,
    )

    # Assert
    assert payload == {
        "embeds": [
            {
                "title": title,
                "url": filing.homepage_url,
                "description": EARNINGS_DESCRIPTION,
                "image": {"url": TRANSPARENT_PNG},
                "thumbnail": {"url": thumbnail},
            },
        ],
    }


def test_send_payload_posts_json_to_each_webhook() -> None:
    """Each webhook gets the same compact UTF-8 JSON body with a JSON content type."""
    # Arrange
    posts: list[httpx2.Request] = []
    payload: WebhookPayload = {"embeds": [{"title": "Café"}]}

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and accept it."""
        posts.append(request)
        return httpx2.Response(204)

    # Act
    send_payload(
        _client(handler),
        [WEBHOOK_A, WEBHOOK_B],
        payload,
        accession_no=ACCESSION_NO,
        ticker=TICKER,
    )

    # Assert
    assert [str(post.url) for post in posts] == [WEBHOOK_A, WEBHOOK_B]
    assert all(post.headers["Content-Type"] == "application/json" for post in posts)
    assert posts[0].content == '{"embeds":[{"title":"Café"}]}'.encode()


def test_send_payload_collects_errors_without_leaking_urls(caplog: pytest.LogCaptureFixture) -> None:
    """Every webhook is tried, retryable errors raise in order, and no URL leaks."""
    # Arrange
    posts: list[httpx2.Request] = []
    webhooks = [
        "https://server-error.example/hook/status-token",
        "https://down.example/hook/connect-token",
        "https://slow.example/hook/timeout-token",
        "https://[::1/malformed-token",
        "https://good.example/hook/good-token",
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Fail each webhook in a different way, and accept the good one."""
        if request.url.host == "server-error.example":
            return httpx2.Response(500)
        if request.url.host == "down.example":
            msg = "connection refused"
            raise httpx2.ConnectError(msg, request=request)
        if request.url.host == "slow.example":
            msg = "too slow"
            raise httpx2.ReadTimeout(msg, request=request)
        posts.append(request)
        return httpx2.Response(204)

    # Act
    with pytest.raises(RuntimeError) as exc_info:
        send_payload(
            _client(handler),
            webhooks,
            {"embeds": []},
            accession_no=ACCESSION_NO,
            ticker=TICKER,
        )

    # Assert
    message = str(exc_info.value)
    assert message == (
        "webhook 1: post webhook: unexpected status 500; "
        "webhook 2: post webhook: request failed; "
        "webhook 3: post webhook: deadline exceeded"
    )
    warnings = _warnings(caplog)
    assert [(record["msg"], record["webhook"], record["reason"]) for record in warnings] == [
        ("webhook skipped", 4, "build webhook request: invalid webhook URL"),
    ]
    assert [post.url.host for post in posts] == ["good.example"]
    text = f"{message} {caplog.text}"
    for secret in ("status-token", "connect-token", "timeout-token", "malformed-token"):
        assert secret not in text
    assert "good-token" not in text


@pytest.mark.parametrize("webhook", ["not a url", "/relative", "https:///no-host"])
def test_send_payload_skips_urls_without_a_host(webhook: str, caplog: pytest.LogCaptureFixture) -> None:
    """A URL with no scheme or host is skipped with a warning, and the next posts."""
    # Arrange
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and accept it."""
        posts.append(request)
        return httpx2.Response(204)

    # Act
    send_payload(
        _client(handler),
        [webhook, WEBHOOK_A],
        {"embeds": []},
        accession_no=ACCESSION_NO,
        ticker=TICKER,
    )

    # Assert
    assert [(record["webhook"], record["reason"]) for record in _warnings(caplog)] == [
        (1, "build webhook request: invalid webhook URL"),
    ]
    assert [str(post.url) for post in posts] == [WEBHOOK_A]


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404])
def test_send_payload_skips_permanent_failures_with_a_warning(status: int, caplog: pytest.LogCaptureFixture) -> None:
    """A permanent 4xx warns once and skips only that webhook. Later ones still post."""
    # Arrange
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Reject the bad webhook with the given status, and accept the rest."""
        if request.url.host == "bad.example":
            return httpx2.Response(status)
        posts.append(request)
        return httpx2.Response(204)

    # Act
    send_payload(
        _client(handler),
        [WEBHOOK_A, "https://bad.example/hook/bad-token", WEBHOOK_B],
        {"embeds": []},
        accession_no=ACCESSION_NO,
        ticker=TICKER,
    )

    # Assert
    assert [(record["msg"], record["webhook"], record["reason"]) for record in _warnings(caplog)] == [
        ("webhook skipped", 2, f"post webhook: unexpected status {status}"),
    ]
    assert [str(post.url) for post in posts] == [WEBHOOK_A, WEBHOOK_B]
    assert "bad-token" not in caplog.text


@pytest.mark.parametrize("status", [408, 429, 500])
def test_send_payload_raises_for_retryable_failures(status: int, caplog: pytest.LogCaptureFixture) -> None:
    """A timeout, rate limit, or server error raises for a retry, after all posts."""
    # Arrange
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Fail the first webhook with the given status, and accept the second."""
        if request.url.host == "flaky.example":
            return httpx2.Response(status)
        posts.append(request)
        return httpx2.Response(204)

    # Act
    with pytest.raises(RuntimeError) as exc_info:
        send_payload(
            _client(handler),
            ["https://flaky.example/hook/flaky-token", WEBHOOK_B],
            {"embeds": []},
            accession_no=ACCESSION_NO,
            ticker=TICKER,
        )

    # Assert
    assert str(exc_info.value) == f"webhook 1: post webhook: unexpected status {status}"
    assert "flaky-token" not in str(exc_info.value)
    assert _warnings(caplog) == []
    assert [str(post.url) for post in posts] == [WEBHOOK_B]


@pytest.mark.parametrize("stage", ["build", "send"])
def test_send_payload_names_unexpected_errors_by_type_only(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """An unexpected build or send error is retryable and shows only its type."""
    # Arrange
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Fail the odd webhook when sending, with a message that echoes its URL."""
        if request.url.host == "odd.example":
            msg = f"failed at {request.url}"
            raise ValueError(msg)
        posts.append(request)
        return httpx2.Response(204)

    client = _client(handler)
    if stage == "build":
        build_request = client.build_request

        def failing_build(
            method: str,
            url: str,
            *,
            content: str,
            headers: dict[str, str],
            timeout: float,
        ) -> httpx2.Request:
            """🧪 Fail the odd webhook while building its request, echoing its URL."""
            if "odd.example" in url:
                msg = f"failed at {url}"
                raise ValueError(msg)
            return build_request(
                method,
                url,
                content=content,
                headers=headers,
                timeout=timeout,
            )

        monkeypatch.setattr(client, "build_request", failing_build)

    # Act
    with pytest.raises(RuntimeError) as exc_info:
        send_payload(
            client,
            ["https://odd.example/hook/odd-token", WEBHOOK_B],
            {"embeds": []},
            accession_no=ACCESSION_NO,
            ticker=TICKER,
        )

    # Assert
    assert str(exc_info.value) == "webhook 1: post webhook: ValueError"
    assert "odd-token" not in str(exc_info.value)
    assert [str(post.url) for post in posts] == [WEBHOOK_B]


def _raising_lookup(_cik: int | str) -> str:
    """🧪 Simulate a ticker service outage."""
    msg = "ticker service down"
    raise OSError(msg)


@pytest.mark.parametrize(
    ("lookup", "warned"),
    [
        pytest.param(_raising_lookup, True, id="raises"),
        pytest.param(lambda _cik: "", False, id="empty"),
        pytest.param(lambda _cik: None, False, id="not-a-string"),
    ],
)
def test_notify_discord_uses_company_when_ticker_lookup_fails(
    lookup: Callable[[int | str], object],
    *,
    warned: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing or unusable ticker lookup still sends the alert, titled by company. Only a raised error warns."""
    # Arrange
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and accept it."""
        posts.append(request)
        return httpx2.Response(204)

    # Act
    notify_discord(
        FakeFiling(),
        client=_client(handler),
        webhooks=[WEBHOOK_A],
        stock_logo=STOCK_LOGO,
        transparent_png=TRANSPARENT_PNG,
        lookup_ticker=lookup,
        get_metrics=lambda _filing: Metrics(),
    )

    # Assert
    embed = json.loads(posts[0].content)["embeds"][0]
    assert embed["title"] == "Acme Corp"
    assert embed["thumbnail"] == {"url": TRANSPARENT_PNG}
    expected = [("ticker lookup failed", "OSError")] if warned else []
    assert [(record["msg"], record["error"]) for record in _warnings(caplog)] == expected


def test_notify_discord_sends_basic_alert_when_metrics_fail(caplog: pytest.LogCaptureFixture) -> None:
    """A failed metrics lookup still posts the alert, with the basic description and a warning."""
    # Arrange
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and accept it."""
        posts.append(request)
        return httpx2.Response(204)

    def failing_metrics(_filing: object) -> Metrics:
        """🧪 Simulate an extraction failure inside edgartools."""
        msg = "release parse failed"
        raise RuntimeError(msg)

    # Act
    notify_discord(
        FakeFiling(),
        client=_client(handler),
        webhooks=[WEBHOOK_A],
        stock_logo=STOCK_LOGO,
        transparent_png=TRANSPARENT_PNG,
        lookup_ticker=lambda _cik: "ACME",
        get_metrics=failing_metrics,
    )

    # Assert
    embed = json.loads(posts[0].content)["embeds"][0]
    assert embed["description"] == EARNINGS_DESCRIPTION
    assert embed["title"] == "ACME: Acme Corp"
    assert [(record["msg"], record["accession_no"], record["error"]) for record in _warnings(caplog)] == [
        ("metrics lookup failed", ACCESSION_NO, "RuntimeError"),
    ]


def test_notify_discord_logs_metrics_duration_after_a_successful_lookup(caplog: pytest.LogCaptureFixture) -> None:
    """A successful metrics lookup logs one INFO line with its accession number and duration in milliseconds."""
    # Arrange
    caplog.set_level(logging.INFO)
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and accept it."""
        posts.append(request)
        return httpx2.Response(204)

    def metrics(_filing: object) -> Metrics:
        """🧪 Return numbers at once, so the duration is short and known to be an int."""
        return Metrics(eps=1.5, eps_basis="GAAP")

    # Act
    notify_discord(
        FakeFiling(),
        client=_client(handler),
        webhooks=[WEBHOOK_A],
        stock_logo=STOCK_LOGO,
        transparent_png=TRANSPARENT_PNG,
        lookup_ticker=lambda _cik: "ACME",
        get_metrics=metrics,
    )

    # Assert
    records = [json.loads(JsonFormatter().format(record)) for record in caplog.records]
    finished = [record for record in records if record["msg"] == "metrics lookup finished"]
    assert len(finished) == 1
    assert finished[0]["accession_no"] == ACCESSION_NO
    assert isinstance(finished[0]["duration_ms"], int)


def test_notify_discord_posts_basic_alert_when_metrics_time_out(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A metrics lookup past its limit still posts the basic alert, and the warning names TimeoutError."""
    # Arrange
    monkeypatch.setattr(discord, "METRICS_TIMEOUT_SECONDS", 0.05)
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """🧪 Record each post and accept it."""
        posts.append(request)
        return httpx2.Response(204)

    def slow_metrics(_filing: object) -> Metrics:
        """🧪 Run past the limit, as a stalled release download would, then return real numbers."""
        time.sleep(1)
        return Metrics(eps=1.5, eps_basis="GAAP")

    # Act
    notify_discord(
        FakeFiling(),
        client=_client(handler),
        webhooks=[WEBHOOK_A],
        stock_logo=STOCK_LOGO,
        transparent_png=TRANSPARENT_PNG,
        lookup_ticker=lambda _cik: "ACME",
        get_metrics=slow_metrics,
    )

    # Assert
    embed = json.loads(posts[0].content)["embeds"][0]
    assert embed["description"] == EARNINGS_DESCRIPTION
    assert [(record["msg"], record["accession_no"], record["error"]) for record in _warnings(caplog)] == [
        ("metrics lookup failed", ACCESSION_NO, "TimeoutError"),
    ]
