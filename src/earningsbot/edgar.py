"""Poll SEC EDGAR for new 8-K filings that report earnings results (Item 2.02).

🛰️ The live feed does not carry item numbers, so each new filing needs one more
request to read its items.
"""

import logging
import time
from typing import TYPE_CHECKING, Protocol

from earningsbot.limits import time_limit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

EARNINGS_ITEM = "2.02"
MAX_FAILURES = 3  # 🛑 A filing whose items cannot be read is skipped after this many failed lookups in a row.
ITEMS_TIMEOUT_SECONDS = 45.0  # ⏱️ Wall-clock budget for one filing's items lookup.


class SeenFilings(Protocol):
    """🗃️ What this module needs from a seen collection. A set or SeenStore fits."""

    # 🧩 Positional-only, because set.__contains__ takes its key that way.
    def __contains__(self, accession_no: object, /) -> bool:
        """Return True when the accession number was already handled."""

    def add(self, accession_no: str, /) -> None:
        """Record the accession number as handled."""


class IndexHeaders(Protocol):
    """🧩 The part of an edgartools IndexHeaders this module reads."""

    @property
    def items(self) -> list[str]:
        """Return bare item numbers such as ['2.02', '9.01']."""


class Filing(Protocol):
    """🧩 The parts of an edgartools Filing this module reads."""

    @property
    def accession_no(self) -> str:
        """Return the SEC accession number, which is unique per filing."""

    @property
    def company(self) -> str:
        """Return the filer's company name."""

    @property
    def cik(self) -> int:
        """Return the filer's Central Index Key."""

    @property
    def filing_date(self) -> str:
        """Return the filing date as text."""

    @property
    def homepage_url(self) -> str:
        """Return the URL of the filing's index page."""

    @property
    def index_headers(self) -> IndexHeaders:
        """Fetch and parse the index-headers page. This is one small HTTP request."""


def is_earnings(items: list[str]) -> bool:
    """Return True when any item label is Item 2.02 (results of operations)."""
    return any(EARNINGS_ITEM in item for item in items)


def index_header_items(filing: Filing) -> list[str]:
    """Return item labels such as ['Item 2.02'] from the filing's index-headers page.

    🪶 The index-headers page is a few KB. The full submission can be megabytes and
    can stall, so this lookup never reads it. The page lists bare numbers, so the
    "Item " prefix is added to match the labels that is_earnings reads.
    """
    return [f"Item {number}" for number in filing.index_headers.items]


def _record_failure(filing: Filing, err: Exception, failures: dict[str, int], seen: SeenFilings) -> bool:
    """Count one failed items lookup for ``filing``. Return True when the filing is skipped.

    Below MAX_FAILURES the caller re-raises, which ends the cycle, and the lookup is
    tried again next cycle. At MAX_FAILURES the filing joins ``seen`` and an ERROR is
    logged, so the rest of the feed keeps moving. The count is cleared on skip.
    """
    accession_no = filing.accession_no
    attempts = failures.get(accession_no, 0) + 1
    if attempts < MAX_FAILURES:
        failures[accession_no] = attempts
        return False
    failures.pop(accession_no, None)
    logger.error(
        "filing skipped after repeated failures",
        extra={"accession_no": accession_no, "company": filing.company, "attempts": attempts, "error": repr(err)},
    )
    seen.add(accession_no)
    return True


def new_earnings_filings(
    filings: Iterable[Filing],
    seen: SeenFilings,
    get_items: Callable[[Filing], list[str]] = index_header_items,
    failures: dict[str, int] | None = None,
) -> Iterator[Filing]:
    """Yield unseen filings that report Item 2.02, in feed order.

    Every unseen filing gets one items lookup, limited to ITEMS_TIMEOUT_SECONDS. A
    failed or timed-out lookup is one failed attempt for that accession. Below
    MAX_FAILURES the error propagates, so the cycle ends and the lookup is tried
    again next cycle. Filings yielded before the error stay yielded. At MAX_FAILURES
    the filing joins ``seen``, an ERROR is logged, and the next filing is checked.
    ``failures`` keeps the counts across cycles, so the caller passes in one dict for
    the run. A successful lookup clears that accession's count.

    A non-earnings filing joins ``seen`` right away. An earnings filing joins ``seen``
    only after the caller asks for the next item, so a failed notify is retried on
    the next poll. A notify failure is never counted here and never skips a filing.
    """
    failures = {} if failures is None else failures
    for filing in filings:
        if filing.accession_no in seen:
            continue
        try:
            with time_limit(ITEMS_TIMEOUT_SECONDS):
                items = get_items(filing)
        except Exception as err:
            if not _record_failure(filing, err, failures, seen):
                raise
            continue
        failures.pop(filing.accession_no, None)
        if not is_earnings(items):
            logger.debug("filing skipped", extra={"accession_no": filing.accession_no, "items": items})
            seen.add(filing.accession_no)
            continue
        logger.info(
            "earnings filing found",
            extra={
                "accession_no": filing.accession_no,
                "company": filing.company,
                "cik": filing.cik,
                "filing_date": filing.filing_date,
            },
        )
        # 🔁 Add after the yield: the caller resumes us only once its write worked.
        yield filing
        seen.add(filing.accession_no)


def format_filing(filing: Filing) -> str:
    """Return one line: filing date, company, CIK, accession number, and URL."""
    return f"{filing.filing_date}  {filing.company}  CIK {filing.cik}  {filing.accession_no}  {filing.homepage_url}"


def poll(  # noqa: PLR0913 - the spec fixes these injected dependencies as keywords
    fetch: Callable[[], Iterable[Filing]],
    notify: Callable[[Filing], object],
    sleep: Callable[[float], object],
    *,
    seen: SeenFilings,
    interval: float,
    once: bool,
) -> bool:
    """Notify about new earnings filings until stopped, or for one cycle when ``once``.

    ``notify`` gets each new earnings filing once. The caller owns ``seen``, so
    a filing already recorded there, even by an earlier run, is not notified
    again. A failed cycle logs a warning and polling continues.

    Only items-lookup failures are counted. After MAX_FAILURES in a row for one
    filing, that filing is skipped, marked seen, and logged at ERROR, so a filing
    whose items cannot be read cannot block the feed. A failed notify, such as a
    Discord outage, ends the cycle and is retried every cycle, and the filing is
    never marked seen. Failure counts live in memory, so a restart starts them at
    zero. Returns True when the single cycle in ``once`` mode succeeded, and False
    when it failed. The loop never returns otherwise. Dependencies are injected so
    tests need no network and no real sleeping.
    """
    failures: dict[str, int] = {}
    while True:
        succeeded = True
        started = time.perf_counter()
        try:
            notified = 0
            for filing in new_earnings_filings(fetch(), seen, failures=failures):
                notify(filing)
                notified += 1
            logger.info(
                "poll cycle finished",
                extra={"earnings": notified, "duration_ms": round((time.perf_counter() - started) * 1000)},
            )
        except Exception as err:  # noqa: BLE001 - one bad cycle must not stop the poller
            logger.warning("poll cycle failed", extra={"error": repr(err)})
            succeeded = False
        if once:
            return succeeded
        sleep(interval)
