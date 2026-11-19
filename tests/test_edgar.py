"""Tests for the EDGAR earnings poller."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

import pytest

from earningsbot.edgar import format_filing, is_earnings, new_earnings_filings, poll

if TYPE_CHECKING:
    from earningsbot.edgar import Filing

CYCLES_BEFORE_STOP = 3
RETRY_CYCLES = 2
EXPECTED_LINE = (
    "2026-11-18  Acme Corp  CIK 320193  0001-26-000001  https://www.sec.gov/index.html"
)


class _StopPolling(BaseException):
    """🛑 Test-only signal that ends the loop. Not an Exception, so poll keeps it."""


@dataclass
class FakeFiling:
    """🧪 Stand-in for an edgartools Filing with only the fields this module reads."""

    accession_no: str
    company: str = "Acme Corp"
    cik: int = 320193
    filing_date: str = "2026-11-18"
    homepage_url: str = "https://www.sec.gov/index.html"
    items: list[str] = field(default_factory=list)

    def obj(self) -> Self:
        """🧪 Return self, since the fake's items play the role of the EightK report."""
        return self


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (["Item 2.02", "Item 9.01"], True),
        (["Item 9.01"], False),
        ([], False),
    ],
)
def test_is_earnings(items: list[str], *, expected: bool) -> None:
    """Only a 2.02 item marks a filing as an earnings release."""
    assert is_earnings(items) is expected


def test_new_earnings_filings_returns_only_unseen_earnings() -> None:
    """Seen filings are skipped without a lookup, and new ones are recorded as seen."""
    already_seen = FakeFiling("0001-26-000001")
    not_earnings = FakeFiling("0001-26-000002")
    earnings = FakeFiling("0001-26-000003")
    seen = {"0001-26-000001"}
    items_by_accession = {
        "0001-26-000002": ["Item 9.01"],
        "0001-26-000003": ["Item 2.02", "Item 9.01"],
    }
    looked_up: list[str] = []

    def get_items(filing: Filing) -> list[str]:
        """🧪 Record each lookup and return canned items."""
        looked_up.append(filing.accession_no)
        return items_by_accession[filing.accession_no]

    result = list(
        new_earnings_filings([already_seen, not_earnings, earnings], seen, get_items)
    )

    assert result == [earnings]
    assert seen == {"0001-26-000001", "0001-26-000002", "0001-26-000003"}
    assert looked_up == ["0001-26-000002", "0001-26-000003"]


def test_new_earnings_filings_keeps_earlier_results_when_lookup_fails() -> None:
    """Earlier earnings filings are yielded before a later lookup raises."""
    earnings = FakeFiling("0001-26-000001")
    failing = FakeFiling("0001-26-000002")
    seen: set[str] = set()

    def get_items(filing: Filing) -> list[str]:
        """🧪 Return Item 2.02 for one filing and time out on the other."""
        if filing is failing:
            msg = "SEC timed out"
            raise TimeoutError(msg)
        return ["Item 2.02"]

    results = new_earnings_filings([earnings, failing], seen, get_items)

    assert next(results) == earnings
    with pytest.raises(TimeoutError, match="SEC timed out"):
        next(results)
    assert seen == {"0001-26-000001"}


def test_format_filing() -> None:
    """A filing prints as one line with two spaces between fields."""
    filing = FakeFiling("0001-26-000001")

    assert format_filing(filing) == EXPECTED_LINE


def test_poll_once_writes_new_earnings() -> None:
    """One cycle writes only the earnings filings, never sleeps, and reports success."""
    filings = [
        FakeFiling("0001-26-000001", items=["Item 2.02"]),
        FakeFiling("0001-26-000002", items=["Item 9.01"]),
    ]
    writes: list[str] = []
    warnings: list[str] = []
    sleeps: list[float] = []

    succeeded = poll(
        fetch=lambda: filings,
        write=writes.append,
        warn=warnings.append,
        sleep=sleeps.append,
        interval=60.0,
        once=True,
    )

    assert succeeded is True
    assert writes == [EXPECTED_LINE + "\n"]
    assert warnings == []
    assert sleeps == []


def test_poll_warns_on_error_and_keeps_polling() -> None:
    """A failed cycle warns, polling continues, and repeats are not written again."""
    filing = FakeFiling("0001-26-000001", items=["Item 2.02"])
    responses: list[list[FakeFiling] | Exception] = [
        RuntimeError("network down"),
        [filing],
        [filing],
    ]
    writes: list[str] = []
    warnings: list[str] = []
    sleeps: list[float] = []

    def fetch() -> list[FakeFiling]:
        """🧪 Return the next scripted response, or raise it if it is an error."""
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def sleep(seconds: float) -> None:
        """🧪 Record the pause and stop the loop after the last scripted cycle."""
        sleeps.append(seconds)
        if len(sleeps) == CYCLES_BEFORE_STOP:
            raise _StopPolling

    with pytest.raises(_StopPolling):
        poll(
            fetch=fetch,
            write=writes.append,
            warn=warnings.append,
            sleep=sleep,
            interval=5.0,
            once=False,
        )

    assert len(warnings) == 1
    assert "network down" in warnings[0]
    assert writes == [EXPECTED_LINE + "\n"]


def test_poll_retries_filing_when_write_fails() -> None:
    """A filing whose write failed is written again next cycle, with one warning."""
    filing = FakeFiling("0001-26-000001", items=["Item 2.02"])
    write_attempts: list[str] = []
    warnings: list[str] = []
    sleeps: list[float] = []

    def write(line: str) -> None:
        """🧪 Fail the first write, then succeed."""
        write_attempts.append(line)
        if len(write_attempts) == 1:
            msg = "stdout closed"
            raise OSError(msg)

    def sleep(seconds: float) -> None:
        """🧪 Record the pause and stop the loop after the retry cycle."""
        sleeps.append(seconds)
        if len(sleeps) == RETRY_CYCLES:
            raise _StopPolling

    with pytest.raises(_StopPolling):
        poll(
            fetch=lambda: [filing],
            write=write,
            warn=warnings.append,
            sleep=sleep,
            interval=5.0,
            once=False,
        )

    assert write_attempts == [EXPECTED_LINE + "\n"] * RETRY_CYCLES
    assert len(warnings) == 1
