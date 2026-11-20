"""Earningsbot package entry point."""

import argparse
import logging
import os
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
from edgar import get_current_filings, set_identity
from edgar.reference.tickers import find_ticker

from earningsbot.discord import notify_discord
from earningsbot.edgar import format_filing, poll
from earningsbot.metrics import fetch_metrics
from earningsbot.state import SeenStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from earningsbot.edgar import Filing

IDENTITY_ENV = "EDGAR_IDENTITY"
STATE_ENV = "EARNINGSBOT_STATE_PATH"
WEBHOOKS_ENV = "DISCORD_EARNINGS_WEBHOOKS"
STOCK_LOGO_ENV = "STOCK_LOGO"
TRANSPARENT_PNG_ENV = "TRANSPARENT_PNG"
DEFAULT_STATE_PATH = ".earningsbot/seen.sqlite3"
DEFAULT_STOCK_LOGO = "https://static.stocktitan.net/company-logo/%s.webp"
DEFAULT_TRANSPARENT_PNG = "https://major.io/transparent.png"
USAGE_EXIT_CODE = 2


def _emit(text: str) -> None:
    """Write text to stdout and flush, so piped readers see each line at once."""
    # 🚰 Flushing matters when the output goes into a pipe.
    sys.stdout.write(text)
    sys.stdout.flush()


def _warn(message: str) -> None:
    """Write one warning line to stderr."""
    sys.stderr.write(message + "\n")


def _notify(filing: Filing) -> None:
    """Print one earnings filing as a line on stdout."""
    _emit(format_filing(filing) + "\n")


def _state_path() -> Path:
    """Return the seen-store path from the environment, or the default when blank."""
    # 🗂️ A blank value counts as unset, so an empty variable never picks the cwd.
    return Path(os.environ.get(STATE_ENV, "").strip() or DEFAULT_STATE_PATH)


def _setting(name: str, default: str) -> str:
    """Return a setting from the environment, or the default when unset or blank."""
    return os.environ.get(name, "").strip() or default


def _webhook_urls(raw: str) -> list[str]:
    """Split a comma-separated webhook list, dropping blank entries."""
    stripped = (item.strip() for item in raw.split(","))
    return [url for url in stripped if url]


def _is_http_url(value: str) -> bool:
    """Return True when the value is an http or https URL with a host and no spaces."""
    # 🚧 httpx2 accepts spaces and turns them into %20 in the host, so reject them here.
    if any(char.isspace() for char in value):
        return False
    try:
        url = httpx2.URL(value)
    except httpx2.InvalidURL:
        return False
    return url.scheme in ("http", "https") and bool(url.host)


def _require_http_urls(webhooks: list[str]) -> None:
    """Exit with a usage error when a webhook is not an http(s) URL with a host.

    The URL is never printed, because it carries a secret token. 🔒
    """
    # 🧭 Checked before polling, so a typo fails at once instead of on every cycle.
    for number, webhook in enumerate(webhooks, start=1):
        if not _is_http_url(webhook):
            sys.stderr.write(f"error: {WEBHOOKS_ENV} entry {number} is not a valid http(s) URL\n")
            raise SystemExit(USAGE_EXIT_CODE)


def _discord_notifier(
    client: httpx2.Client,
    webhooks: list[str],
    warn: Callable[[str], object],
) -> Callable[[Filing], None]:
    """Return a notifier that prints each filing, then posts it to Discord."""
    stock_logo = _setting(STOCK_LOGO_ENV, DEFAULT_STOCK_LOGO)
    transparent_png = _setting(TRANSPARENT_PNG_ENV, DEFAULT_TRANSPARENT_PNG)

    def notify(filing: Filing) -> None:
        """Print the filing line, then post it to every Discord webhook."""
        _notify(filing)
        # 🔎 Looked up at call time, so tests can patch find_ticker in this module.
        notify_discord(
            filing,
            client=client,
            webhooks=webhooks,
            stock_logo=stock_logo,
            transparent_png=transparent_png,
            warn=warn,
            lookup_ticker=find_ticker,
            # 📊 Passed in like find_ticker, so tests can patch it in this module.
            get_metrics=fetch_metrics,
        )

    return notify


def main(argv: list[str] | None = None) -> None:
    """Poll SEC EDGAR and print new 8-K filings that report earnings (Item 2.02)."""
    parser = argparse.ArgumentParser(
        description="Print new 8-K filings with Item 2.02 (earnings) from SEC EDGAR.",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="seconds between polls (default: 60)",
    )
    args = parser.parse_args(argv)

    # 🪪 SEC needs a name and contact in the User-Agent, so refuse to run without one.
    identity = os.environ.get(IDENTITY_ENV, "").strip()
    if not identity:
        sys.stderr.write(f"error: set {IDENTITY_ENV} to 'Your Name your@email' so SEC can identify this client\n")
        raise SystemExit(USAGE_EXIT_CODE)

    set_identity(identity)
    # 🔇 httpx2 logs full request URLs at INFO, and those URLs carry the webhook tokens.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    webhooks = _webhook_urls(os.environ.get(WEBHOOKS_ENV, ""))
    _require_http_urls(webhooks)
    with ExitStack() as stack:
        seen = stack.enter_context(closing(SeenStore(_state_path())))
        notify: Callable[[Filing], None] = _notify
        if webhooks:
            # 🌐 One client serves the whole run, and the stack closes it on any exit.
            client = stack.enter_context(closing(httpx2.Client()))
            notify = _discord_notifier(client, webhooks, _warn)
        succeeded = poll(
            fetch=lambda: get_current_filings(form="8-K", page_size=100),
            notify=notify,
            warn=_warn,
            sleep=time.sleep,
            seen=seen,
            interval=args.interval,
            once=args.once,
        )
    # 🚦 A scripted run needs a non-zero exit when its one cycle failed.
    if args.once and not succeeded:
        raise SystemExit(1)
