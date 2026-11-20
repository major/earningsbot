"""Poll SEC EDGAR for new 8-K filings that report earnings results (Item 2.02).

🛰️ The live feed does not carry item numbers, so each new filing needs one more
request to read its items.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

EARNINGS_ITEM = "2.02"


class SeenFilings(Protocol):
    """🗃️ What this module needs from a seen collection. A set or SeenStore fits."""

    # 🧩 Positional-only, because set.__contains__ takes its key that way.
    def __contains__(self, accession_no: object, /) -> bool:
        """Return True when the accession number was already handled."""

    def add(self, accession_no: str, /) -> None:
        """Record the accession number as handled."""


class EightKReport(Protocol):
    """🧩 The part of an edgartools EightK report this module reads."""

    @property
    def items(self) -> list[str]:
        """Return item labels such as ['Item 2.02', 'Item 9.01']."""


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

    def obj(self) -> EightKReport:
        """Fetch and parse the full filing. This costs one HTTP request."""


def is_earnings(items: list[str]) -> bool:
    """Return True when any item label is Item 2.02 (results of operations)."""
    return any(EARNINGS_ITEM in item for item in items)


def new_earnings_filings(
    filings: Iterable[Filing],
    seen: SeenFilings,
    get_items: Callable[[Filing], list[str]] = lambda f: f.obj().items,
) -> Iterator[Filing]:
    """Yield unseen filings that report Item 2.02, in feed order.

    Every unseen filing gets one items lookup. Lookup errors propagate to the
    caller, and filings yielded before the error stay yielded. A failure ends
    the cycle, so filings after the failing one wait for the next cycle. The SEC
    feed is newest first, so older filings stuck behind a filing that keeps
    failing can scroll off the page and be missed. A non-earnings
    filing joins ``seen`` right away. An earnings filing joins ``seen`` only
    after the caller asks for the next item, so a failed write is retried on
    the next poll.
    """
    for filing in filings:
        if filing.accession_no in seen:
            continue
        items = get_items(filing)
        if not is_earnings(items):
            seen.add(filing.accession_no)
            continue
        # 🔁 Add after the yield: the caller resumes us only once its write worked.
        yield filing
        seen.add(filing.accession_no)


def format_filing(filing: Filing) -> str:
    """Return one line: filing date, company, CIK, accession number, and URL."""
    return (
        f"{filing.filing_date}  {filing.company}  CIK {filing.cik}  "
        f"{filing.accession_no}  {filing.homepage_url}"
    )


def poll(  # noqa: PLR0913 - the spec fixes these injected dependencies as keywords
    fetch: Callable[[], Iterable[Filing]],
    notify: Callable[[Filing], object],
    warn: Callable[[str], object],
    sleep: Callable[[float], object],
    *,
    seen: SeenFilings,
    interval: float,
    once: bool,
) -> bool:
    """Notify about new earnings filings until stopped, or for one cycle when ``once``.

    ``notify`` gets each new earnings filing once. The caller owns ``seen``, so
    a filing already recorded there, even by an earlier run, is not notified
    again. A failed cycle calls ``warn`` and polling continues. A failure ends
    the cycle, so filings after the failing one wait for the next cycle. The SEC
    feed is newest first, so older filings stuck behind a filing that keeps
    failing can scroll off the page and be missed. Returns True
    when the single cycle in ``once`` mode succeeded, and False when it warned.
    The loop never returns otherwise. Dependencies are injected so tests need
    no network and no real sleeping.
    """
    while True:
        succeeded = True
        try:
            for filing in new_earnings_filings(fetch(), seen):
                notify(filing)
        except Exception as err:  # noqa: BLE001 - one bad cycle must not stop the poller
            warn(f"poll cycle failed: {err!r}")
            succeeded = False
        if once:
            return succeeded
        sleep(interval)
