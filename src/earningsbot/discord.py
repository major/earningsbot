"""Build Discord embeds for earnings filings and post them to webhooks.

📣 Posting is synchronous, because the app has no asyncio loop.
"""

import json
from typing import TYPE_CHECKING, NotRequired, TypedDict

import httpx2
from edgar.reference.tickers import find_ticker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from earningsbot.edgar import Filing

_TITLE_LIMIT = 256
_WEBHOOK_TIMEOUT_SECONDS = 10.0
_EARNINGS_DESCRIPTION = "📄 Earnings release filed"
_INVALID_WEBHOOK = "build webhook request: invalid webhook URL"


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


def earnings_payload(
    filing: Filing,
    ticker: str,
    *,
    stock_logo: str,
    transparent_png: str,
) -> WebhookPayload:
    """Build one embed for an earnings filing, with the ticker when one is known."""
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
        "description": _EARNINGS_DESCRIPTION,
        "image": {"url": transparent_png},
        "thumbnail": {"url": thumbnail},
    }
    return {"embeds": [embed]}


def send_payload(
    client: httpx2.Client,
    webhooks: Sequence[str],
    payload: WebhookPayload,
    *,
    timeout: float = _WEBHOOK_TIMEOUT_SECONDS,
) -> None:
    """Post the payload to each webhook in order, then raise once if any post failed.

    Every webhook is tried, even after one fails. Error text never includes a
    webhook URL, because the URL carries a secret token. 🔒
    """
    # 🧾 Compact JSON keeps each body small, and raw UTF-8 keeps names readable.
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    errors: list[str] = []
    for webhook in webhooks:
        error = _post(client, webhook, body, timeout)
        if error:
            errors.append(error)
    if errors:
        raise RuntimeError("; ".join(errors))


def notify_discord(  # noqa: PLR0913 - the spec fixes these injected dependencies as keywords
    filing: Filing,
    *,
    client: httpx2.Client,
    webhooks: Sequence[str],
    stock_logo: str,
    transparent_png: str,
    lookup_ticker: Callable[[int | str], object] = find_ticker,
) -> None:
    """Post an embed for the filing to each webhook. Failures raise for a retry."""
    ticker = _ticker_or_empty(filing.cik, lookup_ticker)
    payload = earnings_payload(
        filing,
        ticker,
        stock_logo=stock_logo,
        transparent_png=transparent_png,
    )
    send_payload(client, webhooks, payload)


def _ticker_or_empty(cik: int, lookup_ticker: Callable[[int | str], object]) -> str:
    """Return the CIK's ticker, or an empty string when the lookup fails."""
    # 🛟 The ticker is optional, so a lookup failure must never block the alert.
    try:
        ticker = lookup_ticker(cik)
    except Exception:  # noqa: BLE001 - any lookup failure falls back to the company name
        return ""
    return ticker if isinstance(ticker, str) else ""


def _post(
    client: httpx2.Client,
    webhook: str,
    body: str,
    timeout: float,
) -> str | None:
    """Post one body to one webhook, returning a sanitized error or None on success."""
    try:
        request = client.build_request(
            "POST",
            webhook,
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx2.InvalidURL:
        # 🧱 A malformed URL must not stop the loop, so it becomes one error entry.
        return _INVALID_WEBHOOK
    # 🧱 A URL without a scheme or host builds fine, but cannot be posted to.
    if not request.url.is_absolute_url or not request.url.host:
        return _INVALID_WEBHOOK
    try:
        response = client.send(request)
    except httpx2.TimeoutException:
        return "post webhook: deadline exceeded"
    except httpx2.RequestError:
        return "post webhook: request failed"
    if not response.is_success:
        return f"post webhook: unexpected status {response.status_code}"
    return None


def _limit_embed_text(value: str, limit: int) -> str:
    """Keep embed text within its Discord character limit, marking truncation."""
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"
