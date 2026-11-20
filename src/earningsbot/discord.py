"""Build Discord embeds for earnings filings and post them to webhooks.

📣 Posting is synchronous, because the app has no asyncio loop.
"""

import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, NotRequired, TypedDict

import httpx2
from edgar.reference.tickers import find_ticker

from earningsbot.limits import time_limit
from earningsbot.metrics import Metrics, fetch_metrics, format_eps, format_revenue

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from earningsbot.edgar import Filing

logger = logging.getLogger(__name__)

_TITLE_LIMIT = 256
_WEBHOOK_TIMEOUT_SECONDS = 10.0
# ⏱️ A full-submission download can take minutes on a slow day. Past this, the alert posts without metrics.
METRICS_TIMEOUT_SECONDS = 180.0
_EARNINGS_DESCRIPTION = "📄 Earnings release filed"
_INVALID_WEBHOOK = "build webhook request: invalid webhook URL"
# 🔁 Besides 5xx, these 4xx codes mean "try again later", so they are retried.
_RETRYABLE_STATUSES = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS},
)


class EmbedImage(TypedDict):
    """An image URL used by a Discord embed."""

    url: str


class Embed(TypedDict):
    """A Discord embed with optional title, media, description, and URL."""

    title: NotRequired[str]
    image: NotRequired[EmbedImage]
    thumbnail: NotRequired[EmbedImage]
    description: NotRequired[str]
    url: NotRequired[str]


class WebhookPayload(TypedDict):
    """JSON body accepted by a Discord webhook."""

    embeds: list[Embed]


@dataclass(frozen=True)
class _PostError:
    """🧾 One failed post: a safe message, and whether a retry can never help."""

    message: str
    permanent: bool


def earnings_payload(
    filing: Filing,
    ticker: str,
    *,
    stock_logo: str,
    transparent_png: str,
    metrics: Metrics | None = None,
) -> WebhookPayload:
    """Build one embed for a filing, with the ticker and metrics when known."""
    if ticker:
        title = f"{ticker}: {filing.company}"
        # 🖼️ The logo template takes the lowercase ticker, as in the stocknews project.
        thumbnail = stock_logo.replace("%s", ticker.lower())
    else:
        title = filing.company
        thumbnail = transparent_png
    embed: Embed = {
        "title": _limit_embed_text(title, _TITLE_LIMIT),
        "url": filing.homepage_url,
        "description": _description(metrics or Metrics()),
        "image": {"url": transparent_png},
        "thumbnail": {"url": thumbnail},
    }
    return {"embeds": [embed]}


def send_payload(  # noqa: PLR0913 - the log fields are keywords, like the other injected values
    client: httpx2.Client,
    webhooks: Sequence[str],
    payload: WebhookPayload,
    *,
    accession_no: str,
    ticker: str,
    timeout: float = _WEBHOOK_TIMEOUT_SECONDS,
) -> None:
    """Post the payload to each webhook, then raise once if a retryable post failed.

    A permanent failure, such as a deleted webhook, logs a warning and skips only
    that webhook, so it cannot repost the filing to the others forever. A
    retryable failure raises, so the filing is retried next cycle. Every webhook
    is tried, even after one fails. Logs and error text carry the webhook number,
    never its URL, because the URL carries a secret token. 🔒
    """
    # 🧾 Compact JSON keeps each body small, and raw UTF-8 keeps names readable.
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    retryable: list[str] = []
    for number, webhook in enumerate(webhooks, start=1):
        failure = _post(client, webhook, body, timeout)
        if failure is None:
            logger.info(
                "discord post sent",
                extra={"webhook": number, "accession_no": accession_no, "ticker": ticker},
            )
        elif failure.permanent:
            logger.warning(
                "webhook skipped",
                extra={"webhook": number, "accession_no": accession_no, "reason": failure.message},
            )
        else:
            retryable.append(f"webhook {number}: {failure.message}")
    if retryable:
        raise RuntimeError("; ".join(retryable))


def notify_discord(  # noqa: PLR0913 - the spec fixes these injected dependencies as keywords
    filing: Filing,
    *,
    client: httpx2.Client,
    webhooks: Sequence[str],
    stock_logo: str,
    transparent_png: str,
    lookup_ticker: Callable[[int | str], object] = find_ticker,
    get_metrics: Callable[[Filing], Metrics] = fetch_metrics,
) -> None:
    """Post an embed to each webhook. Permanent failures log a warning, retryable ones raise."""
    ticker = _ticker_or_empty(filing.cik, lookup_ticker)
    metrics = _metrics_or_empty(filing, get_metrics)
    payload = earnings_payload(
        filing,
        ticker,
        stock_logo=stock_logo,
        transparent_png=transparent_png,
        metrics=metrics,
    )
    send_payload(
        client,
        webhooks,
        payload,
        accession_no=filing.accession_no,
        ticker=ticker,
    )


def _description(metrics: Metrics) -> str:
    """Return the release line, then one line per known EPS and revenue value."""
    lines = [_EARNINGS_DESCRIPTION]
    # 📊 EPS first, then revenue. Each line shows only when its value is known.
    if metrics.eps is not None:
        lines.append(f"📊 EPS: {format_eps(metrics.eps)} ({metrics.eps_basis})")
    if metrics.revenue is not None:
        lines.append(f"📊 Revenue: {format_revenue(metrics.revenue)}")
    return "\n".join(lines)


def _metrics_or_empty(
    filing: Filing,
    get_metrics: Callable[[Filing], Metrics],
) -> Metrics:
    """Return the filing's metrics, or empty Metrics when the lookup fails or runs past METRICS_TIMEOUT_SECONDS."""
    # 🛟 Metrics are a bonus, so an extraction failure or a slow download must never block the alert.
    try:
        with time_limit(METRICS_TIMEOUT_SECONDS):
            return get_metrics(filing)
    except Exception as err:  # noqa: BLE001 - any failure falls back to the basic alert
        logger.warning(
            "metrics lookup failed", extra={"accession_no": filing.accession_no, "error": type(err).__name__}
        )
        return Metrics()


def _ticker_or_empty(cik: int, lookup_ticker: Callable[[int | str], object]) -> str:
    """Return the CIK's ticker, or an empty string when the lookup fails."""
    # 🛟 The ticker is optional, so a lookup failure must never block the alert.
    try:
        ticker = lookup_ticker(cik)
    except Exception as err:  # noqa: BLE001 - any lookup failure falls back to the company name
        logger.warning("ticker lookup failed", extra={"cik": cik, "error": type(err).__name__})
        return ""
    return ticker if isinstance(ticker, str) else ""


def _post(
    client: httpx2.Client,
    webhook: str,
    body: str,
    timeout: float,
) -> _PostError | None:
    """Post one body to one webhook, returning a classified error or None on success.

    Any unexpected exception becomes a retryable error that names only its type,
    so it never carries the URL and never stops the other webhooks. 🛟
    """
    try:
        return _post_request(client, webhook, body, timeout)
    except Exception as err:  # noqa: BLE001 - never skip webhooks or leak URLs
        # 🛟 Only the type name is kept, because the error text may echo the URL.
        return _PostError(f"post webhook: {type(err).__name__}", permanent=False)


def _post_request(
    client: httpx2.Client,
    webhook: str,
    body: str,
    timeout: float,
) -> _PostError | None:
    """Build and send one request, classifying the failures it knows about."""
    try:
        request = client.build_request(
            "POST",
            webhook,
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx2.InvalidURL:
        # 🧱 A malformed URL must not stop the loop, so it becomes one permanent error.
        request = None
    # 🧱 A URL without a scheme or host builds fine, but cannot be posted to.
    if request is None or not request.url.is_absolute_url or not request.url.host:
        return _PostError(_INVALID_WEBHOOK, permanent=True)
    try:
        response = client.send(request)
    except httpx2.TimeoutException:
        return _PostError("post webhook: deadline exceeded", permanent=False)
    except httpx2.RequestError:
        return _PostError("post webhook: request failed", permanent=False)
    if response.is_success:
        return None
    status = response.status_code
    # 🧭 Only 408, 429, and 5xx may pass later. Every other status is permanent.
    retryable = status in _RETRYABLE_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    return _PostError(
        f"post webhook: unexpected status {status}",
        permanent=not retryable,
    )


def _limit_embed_text(value: str, limit: int) -> str:
    """Keep embed text within its Discord character limit, marking truncation."""
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"
