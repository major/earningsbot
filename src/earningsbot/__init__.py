"""Earningsbot package entry point."""

import argparse
import os
import sys
import time

from edgar import get_current_filings, set_identity

from earningsbot.edgar import poll

IDENTITY_ENV = "EDGAR_IDENTITY"
USAGE_EXIT_CODE = 2


def _emit(text: str) -> None:
    """Write text to stdout and flush, so piped readers see each line at once."""
    # 🚰 Flushing matters when the output goes into a pipe.
    sys.stdout.write(text)
    sys.stdout.flush()


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
        sys.stderr.write(
            f"error: set {IDENTITY_ENV} to 'Your Name your@email' "
            "so SEC can identify this client\n"
        )
        raise SystemExit(USAGE_EXIT_CODE)

    set_identity(identity)
    succeeded = poll(
        fetch=lambda: get_current_filings(form="8-K", page_size=100),
        write=_emit,
        warn=lambda message: sys.stderr.write(message + "\n"),
        sleep=time.sleep,
        interval=args.interval,
        once=args.once,
    )
    # 🚦 A scripted run needs a non-zero exit when its one cycle failed.
    if args.once and not succeeded:
        raise SystemExit(1)
