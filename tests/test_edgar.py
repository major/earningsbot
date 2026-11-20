"""Tests for the EDGAR earnings poller."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

import pytest

from earningsbot import edgar
from earningsbot.edgar import format_filing, is_earnings, new_earnings_filings, poll
from earningsbot.log_config import JsonFormatter

if TYPE_CHECKING:
    from earningsbot.edgar import Filing

CYCLES_BEFORE_STOP = 3
RETRY_CYCLES = 2
FAILURE_LIMIT = 3
SKIP_CYCLES = 4
LONE_CYCLES = 5
WARNED_CYCLES = 4  # 🧾 Cycles 1, 2, 3 (a rejected notify), and 4 (a skip) each end with a warning
SCRIPT_CYCLES = 7
NOTIFY_ATTEMPTS = 5
EXPECTED_LINE = "2026-11-18  Acme Corp  CIK 320193  0001-26-000001  https://www.sec.gov/index.html"


class _StopPolling(BaseException):
    """🛑 Test-only signal that ends the loop. Not an Exception, so poll keeps it."""


def _records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """🧪 Return each captured log record as the JSON object it prints as."""
    return [json.loads(JsonFormatter().format(record)) for record in caplog.records]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """🧪 Return the captured records at WARNING level."""
    return [record for record in _records(caplog) if record["level"] == "warning"]


def _errors(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """🧪 Return the captured records at ERROR level."""
    return [record for record in _records(caplog) if record["level"] == "error"]


@dataclass
class FakeIndexHeaders:
    """🧪 Stand-in for the index-headers page, which lists bare item numbers such as 2.02."""

    items: list[str]


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

    @property
    def index_headers(self) -> FakeIndexHeaders:
        """🧪 Return the items without the "Item " prefix, as the index-headers page lists them."""
        return FakeIndexHeaders(items=[item.removeprefix("Item ") for item in self.items])


class _NoBodyFiling(FakeFiling):
    """🧪 Filing whose full submission must never be read, as in the stuck production case."""

    def obj(self) -> Self:
        """🧪 Fail the test if the lookup falls back to the full submission."""
        msg = "full submission must not be downloaded"
        raise AssertionError(msg)


class _TimingOutFiling(FakeFiling):
    """🧪 Filing whose index-headers page fails with a timeout, as a stalled download does."""

    @property
    def index_headers(self) -> FakeIndexHeaders:
        """🧪 Raise a timeout at once."""
        msg = "SEC never answered"
        raise TimeoutError(msg)


class _HangingFiling(FakeFiling):
    """🧪 Filing whose index-headers page takes longer than the limits the tests set."""

    @property
    def index_headers(self) -> FakeIndexHeaders:
        """🧪 Block for a second before answering, so only the time limit can end the call early."""
        time.sleep(1)
        return super().index_headers


@dataclass
class _ScriptedFiling(FakeFiling):
    """🧪 Filing whose successive lookups follow a script of "ok" and "fail" answers."""

    lookups: list[str] = field(default_factory=list)

    @property
    def index_headers(self) -> FakeIndexHeaders:
        """🧪 Answer the next scripted lookup, or time out when the script says so."""
        if self.lookups.pop(0) == "fail":
            msg = "SEC timed out"
            raise TimeoutError(msg)
        return super().index_headers


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

    result = list(new_earnings_filings([already_seen, not_earnings, earnings], seen, get_items))

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


def test_failed_lookup_does_not_end_the_cycle() -> None:
    """A failed lookup gets a strike, but the next filing is still yielded, and the cycle still ends with the error."""
    # Arrange
    pill = FakeFiling("0001-26-000001")
    good = FakeFiling("0001-26-000002", items=["Item 2.02"])
    seen: set[str] = set()
    failures: dict[str, Any] = {}

    def get_items(filing: Filing) -> list[str]:
        """🧪 Time out on the pill and return Item 2.02 for the good filing."""
        if filing is pill:
            msg = "SEC timed out"
            raise TimeoutError(msg)
        return ["Item 2.02"]

    results = new_earnings_filings([pill, good], seen, get_items, failures=failures)

    # Act and assert: the good filing comes out, then the cycle ends with the pill's error
    assert next(results) == good
    with pytest.raises(TimeoutError, match="SEC timed out"):
        next(results)
    assert seen == {good.accession_no}
    assert failures[pill.accession_no].count == 1


def test_two_failed_lookups_in_a_row_end_the_cycle_without_strikes() -> None:
    """Two misses in a row look like an outage: the cycle stops at the second, and it counts no strikes."""
    # Arrange
    first = FakeFiling("0001-26-000001")
    second = FakeFiling("0001-26-000002")
    later = FakeFiling("0001-26-000003", items=["Item 2.02"])
    seen: set[str] = set()
    failures: dict[str, Any] = {}
    looked_up: list[str] = []

    def get_items(filing: Filing) -> list[str]:
        """🧪 Record each lookup, and time out on every one."""
        looked_up.append(filing.accession_no)
        msg = "SEC timed out"
        raise TimeoutError(msg)

    # Act
    with pytest.raises(TimeoutError, match="SEC timed out"):
        list(new_earnings_filings([first, second, later], seen, get_items, failures=failures))

    # Assert
    assert looked_up == [first.accession_no, second.accession_no]
    assert failures == {}
    assert seen == set()


def test_format_filing() -> None:
    """A filing prints as one line with two spaces between fields."""
    filing = FakeFiling("0001-26-000001")

    assert format_filing(filing) == EXPECTED_LINE


def test_poll_once_notifies_new_earnings(caplog: pytest.LogCaptureFixture) -> None:
    """One cycle notifies only earnings filings, never sleeps, and logs the count."""
    caplog.set_level(logging.INFO)
    filings = [
        FakeFiling("0001-26-000001", items=["Item 2.02"]),
        FakeFiling("0001-26-000002", items=["Item 9.01"]),
    ]
    notified: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()

    succeeded = poll(
        fetch=lambda: filings,
        notify=notified.append,
        sleep=sleeps.append,
        seen=seen,
        interval=60.0,
        once=True,
    )

    assert succeeded is True
    assert notified == [filings[0]]
    assert _warnings(caplog) == []
    assert sleeps == []
    # 🧾 The injected seen set records every filing this cycle handled.
    assert seen == {"0001-26-000001", "0001-26-000002"}
    finished = [record for record in _records(caplog) if record["msg"] == "poll cycle finished"]
    assert [record["earnings"] for record in finished] == [1]
    assert isinstance(finished[0]["duration_ms"], int)


def test_poll_warns_on_error_and_keeps_polling(caplog: pytest.LogCaptureFixture) -> None:
    """A failed cycle warns, polling continues, and repeats are not notified again."""
    filing = FakeFiling("0001-26-000001", items=["Item 2.02"])
    responses: list[list[FakeFiling] | Exception] = [
        RuntimeError("network down"),
        [filing],
        [filing],
    ]
    notified: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()

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
            notify=notified.append,
            sleep=sleep,
            seen=seen,
            interval=5.0,
            once=False,
        )

    warnings = _warnings(caplog)
    assert [record["msg"] for record in warnings] == ["poll cycle failed"]
    assert "network down" in warnings[0]["error"]
    assert notified == [filing]


def test_poll_retries_filing_when_notify_fails(caplog: pytest.LogCaptureFixture) -> None:
    """A filing whose notify failed is notified again next cycle, with one warning."""
    filing = FakeFiling("0001-26-000001", items=["Item 2.02"])
    attempts: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()

    def notify(item: Filing) -> None:
        """🧪 Fail the first notify, then succeed."""
        attempts.append(item)
        if len(attempts) == 1:
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
            notify=notify,
            sleep=sleep,
            seen=seen,
            interval=5.0,
            once=False,
        )

    assert attempts == [filing] * RETRY_CYCLES
    assert len(_warnings(caplog)) == 1


def test_default_lookup_reads_index_headers_not_full_submission() -> None:
    """The default lookup reads items from the small index-headers page and never calls obj()."""
    earnings = _NoBodyFiling("0001-26-000001", items=["Item 2.02", "Item 9.01"])

    result = list(new_earnings_filings([earnings], set()))

    assert result == [earnings]


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (["Item 2.02", "Item 9.01"], True),
        (["Item 1.01", "Item 9.01"], False),
        ([], False),
    ],
)
def test_index_header_items_work_with_is_earnings(items: list[str], *, expected: bool) -> None:
    """Index-headers items pass through is_earnings the same way full-submission items did."""
    filing = FakeFiling("0001-26-000001", items=items)

    assert is_earnings(edgar.index_header_items(filing)) is expected


def test_hung_items_lookup_warns_and_retries_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung items lookup ends the cycle with a warning, and the filing stays unseen for the next cycle."""
    # Arrange
    monkeypatch.setattr(edgar, "ITEMS_TIMEOUT_SECONDS", 0.05)
    filing = _HangingFiling("0001-26-000001", items=["Item 2.02"])
    notified: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()

    def sleep(seconds: float) -> None:
        """🧪 Record the pause and stop after the second cycle."""
        sleeps.append(seconds)
        if len(sleeps) == RETRY_CYCLES:
            raise _StopPolling

    # Act
    with pytest.raises(_StopPolling):
        poll(fetch=lambda: [filing], notify=notified.append, sleep=sleep, seen=seen, interval=5.0, once=False)

    # Assert
    warnings = _warnings(caplog)
    assert [record["msg"] for record in warnings] == ["poll cycle failed"] * RETRY_CYCLES
    assert all("TimeoutError" in record["error"] for record in warnings)
    assert seen == set()
    assert notified == []


def test_poll_skips_filing_only_after_strikes_span_the_limit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three strikes inside the span limit keep the filing unseen. A strike past the span skips it with an ERROR."""
    # Arrange: the clock moves 600 s per cycle, so strikes land at 0, 600, 1200, and 1800 seconds
    clock = [0.0]
    monkeypatch.setattr(edgar, "_now", lambda: clock[0])
    poison = _TimingOutFiling("0001-26-000001", items=["Item 2.02"])
    notified: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()
    skipped_after_cycle: list[bool] = []

    def fetch() -> list[FakeFiling]:
        """🧪 Return the poison filing and one new non-earnings filing, so each cycle has a success."""
        return [poison, FakeFiling(f"0002-26-{len(sleeps):06d}", items=["Item 9.01"])]

    def sleep(seconds: float) -> None:
        """🧪 Move the clock, record whether the poison filing is skipped yet, and stop after four cycles."""
        clock[0] += 600.0
        sleeps.append(seconds)
        skipped_after_cycle.append(poison.accession_no in seen)
        if len(sleeps) == SKIP_CYCLES:
            raise _StopPolling

    # Act
    with pytest.raises(_StopPolling):
        poll(fetch=fetch, notify=notified.append, sleep=sleep, seen=seen, interval=5.0, once=False)

    # Assert
    assert skipped_after_cycle == [False, False, False, True]
    assert notified == []
    assert len(_warnings(caplog)) == SKIP_CYCLES
    errors = _errors(caplog)
    # 🧾 The fourth strike is the one that lands past the span, so attempts is 4
    assert [(record["msg"], record["accession_no"], record["attempts"]) for record in errors] == [
        ("filing skipped after repeated failures", poison.accession_no, 4),
    ]
    assert "TimeoutError" in errors[0]["error"]


def test_poll_resets_count_on_success_and_counts_each_filing_apart(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A success clears that filing's strikes, and one filing's failures never count toward another's."""
    # Arrange: "fail, fail, ok (notify rejected), fail, ok" for the first filing, and always fail for the second.
    # Each cycle also has a fresh non-earnings filing that succeeds, so the strikes are not dropped as an outage.
    clock = [0.0]
    monkeypatch.setattr(edgar, "_now", lambda: clock[0])
    filing = _ScriptedFiling("0001-26-000001", items=["Item 2.02"], lookups=["fail", "fail", "ok", "fail", "ok"])
    other = _TimingOutFiling("0001-26-000002", items=["Item 2.02"])
    notify_rejects = [True, False]
    notified: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()

    def fetch() -> list[Filing]:
        """🧪 Return the scripted filing, a fresh success, then the always-failing filing."""
        return [filing, FakeFiling(f"0002-26-{len(sleeps):06d}", items=["Item 9.01"]), other]

    def notify(item: Filing) -> None:
        """🧪 Reject the first notify, which follows the lookup that succeeds in cycle 3, then accept."""
        if notify_rejects.pop(0):
            msg = "webhook rejected"
            raise OSError(msg)
        notified.append(item)

    def sleep(seconds: float) -> None:
        """🧪 Move the clock 600 s per cycle, and stop after the scripted cycles."""
        clock[0] += 600.0
        sleeps.append(seconds)
        if len(sleeps) == SCRIPT_CYCLES:
            raise _StopPolling

    # Act
    with pytest.raises(_StopPolling):
        poll(fetch=fetch, notify=notify, sleep=sleep, seen=seen, interval=5.0, once=False)

    # Assert
    assert notified == [filing]
    assert {filing.accession_no, other.accession_no} <= seen
    errors = _errors(caplog)
    assert [(record["accession_no"], record["attempts"]) for record in errors] == [(other.accession_no, FAILURE_LIMIT)]
    assert len(_warnings(caplog)) == WARNED_CYCLES


def test_lone_failing_filing_never_collects_strikes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A filing that is the only unseen one never collects strikes, so it is never skipped, however long it fails."""
    # Arrange: 900 s per cycle, so the span limit would pass if strikes were counted
    clock = [0.0]
    monkeypatch.setattr(edgar, "_now", lambda: clock[0])
    lone = _TimingOutFiling("0001-26-000001", items=["Item 2.02"])
    notified: list[Filing] = []
    sleeps: list[float] = []
    seen: set[str] = set()

    def sleep(seconds: float) -> None:
        """🧪 Move the clock, and stop after the fixed number of cycles."""
        clock[0] += 900.0
        sleeps.append(seconds)
        if len(sleeps) == LONE_CYCLES:
            raise _StopPolling

    # Act
    with pytest.raises(_StopPolling):
        poll(fetch=lambda: [lone], notify=notified.append, sleep=sleep, seen=seen, interval=5.0, once=False)

    # Assert
    assert seen == set()
    assert notified == []
    assert _errors(caplog) == []
    assert len(_warnings(caplog)) == LONE_CYCLES


def test_failed_notify_never_marks_filing_seen(caplog: pytest.LogCaptureFixture) -> None:
    """A filing whose notify keeps failing stays unseen with no skip and no ERROR, and once=True still returns False."""
    # Arrange
    filing = FakeFiling("0001-26-000001", items=["Item 2.02"])
    sleeps: list[float] = []
    seen: set[str] = set()

    def notify(_filing: Filing) -> None:
        """🧪 Reject every notify."""
        msg = "webhook rejected"
        raise OSError(msg)

    def sleep(seconds: float) -> None:
        """🧪 Record the pause and stop after the fifth failed cycle."""
        sleeps.append(seconds)
        if len(sleeps) == NOTIFY_ATTEMPTS:
            raise _StopPolling

    # Act
    with pytest.raises(_StopPolling):
        poll(fetch=lambda: [filing], notify=notify, sleep=sleep, seen=seen, interval=5.0, once=False)
    succeeded = poll(fetch=lambda: [filing], notify=notify, sleep=sleeps.append, seen=seen, interval=5.0, once=True)

    # Assert
    assert succeeded is False
    assert seen == set()
    assert len(_warnings(caplog)) == NOTIFY_ATTEMPTS + 1
    assert _errors(caplog) == []
