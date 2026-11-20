"""Poll SEC EDGAR for new 8-K filings that report earnings results (Item 2.02).

🛰️ The live feed does not carry item numbers, so each new filing needs one more
request to read its items.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from earningsbot.limits import time_limit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

EARNINGS_ITEM = "2.02"
MAX_FAILURES = 3  # 🛑 A filing whose items cannot be read is skipped once it has this many strikes.
MIN_FAILURE_SPAN_SECONDS = 1800.0  # ⏳ Strikes must also span this long, so a briefly missing page is spared.
OUTAGE_STREAK = 2  # 🌩️ This many misses in a row in one cycle look like an outage, so that cycle counts no strikes.
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


@dataclass
class _Strikes:
    """🧾 Failed items lookups for one filing, and the clock time of its first strike."""

    count: int
    first_strike: float


def _now() -> float:
    """⏳ Return the monotonic clock in seconds. Tests patch this to move time without sleeping."""
    return time.monotonic()


def _record_strikes(
    misses: list[tuple[Filing, Exception]],
    failures: dict[str, _Strikes],
    seen: SeenFilings,
) -> None:
    """Add one strike for each failed filing, and skip a filing that has reached the limit.

    A filing is skipped only when it has MAX_FAILURES strikes and they span at least
    MIN_FAILURE_SPAN_SECONDS. A skipped filing joins ``seen``, its count is cleared,
    and an ERROR is logged.
    """
    now = _now()
    for filing, err in misses:
        accession_no = filing.accession_no
        strikes = failures.setdefault(accession_no, _Strikes(count=0, first_strike=now))
        strikes.count += 1
        if strikes.count < MAX_FAILURES or now - strikes.first_strike < MIN_FAILURE_SPAN_SECONDS:
            continue
        failures.pop(accession_no)
        logger.error(
            "filing skipped after repeated failures",
            extra={
                "accession_no": accession_no,
                "company": filing.company,
                "attempts": strikes.count,
                "error": repr(err),
            },
        )
        seen.add(accession_no)


def new_earnings_filings(
    filings: Iterable[Filing],
    seen: SeenFilings,
    get_items: Callable[[Filing], list[str]] = index_header_items,
    failures: dict[str, _Strikes] | None = None,
) -> Iterator[Filing]:
    """Yield unseen filings that report Item 2.02, in feed order.

    Every unseen filing gets one items lookup, limited to ITEMS_TIMEOUT_SECONDS. A
    failed lookup does not end the cycle, so later filings are still checked. Two
    failed lookups in a row end the cycle with the error, and that cycle counts no
    strikes, since SEC looks down. Otherwise, the cycle ends with the last error when
    any lookup failed, and each failed filing gets a strike only if another lookup
    succeeded in the same cycle. That tells a stuck filing apart from an outage. A
    filing with MAX_FAILURES strikes spanning MIN_FAILURE_SPAN_SECONDS joins ``seen``
    and gets an ERROR. A filing that is the only unseen one never gets strikes.
    Filings yielded before an error stay yielded.

    ``failures`` keeps the strikes across cycles, so the caller passes in one dict for
    the run. A successful lookup clears that accession's strikes. A cycle cut short by
    a notify failure applies no strikes, because the generator is never resumed.

    A non-earnings filing joins ``seen`` right away. An earnings filing joins ``seen``
    only after the caller asks for the next item, so a failed notify is retried on
    the next poll. A notify failure is never counted here and never skips a filing.
    """
    failures = {} if failures is None else failures
    misses: list[tuple[Filing, Exception]] = []
    streak = 0
    found_any = False
    for filing in filings:
        if filing.accession_no in seen:
            continue
        try:
            with time_limit(ITEMS_TIMEOUT_SECONDS):
                items = get_items(filing)
        except Exception as err:
            misses.append((filing, err))
            streak += 1
            if streak >= OUTAGE_STREAK:
                # 🌩️ Two misses in a row look like an outage, so the strikes for this cycle are dropped.
                raise
            continue
        streak = 0
        found_any = True
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
    if misses and found_any:
        # 🧾 Strikes come last, so a cycle that an outage cut short never reaches this line.
        _record_strikes(misses, failures, seen)
    if misses:
        raise misses[-1][1]


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

    Only items-lookup failures are counted, as strikes. A filing with MAX_FAILURES
    strikes spanning MIN_FAILURE_SPAN_SECONDS is skipped, marked seen, and logged at
    ERROR, so a filing whose items cannot be read cannot block the feed for long. A
    cycle with two failed lookups in a row is an outage and counts no strikes. A
    failed notify, such as a Discord outage, ends the cycle and is retried every
    cycle, and the filing is never marked seen. Strikes live in memory, so a restart
    starts them at zero. Returns True when the single cycle in ``once`` mode
    succeeded, and False when it failed. The loop never returns otherwise.
    Dependencies are injected so tests need no network and no real sleeping.
    """
    failures: dict[str, _Strikes] = {}
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
