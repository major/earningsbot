"""Tests for the wall-clock limit helper and its use on a real stalled read."""

import signal
import socket
import threading
import time
from typing import TYPE_CHECKING

import pytest
from edgar.httprequests import download_text

from earningsbot import limits

if TYPE_CHECKING:
    from collections.abc import Iterator

# ⏱️ Far above the 0.3 s limit, far below the 30 s edgartools read timeout and the retry budget.
STALL_BOUND_SECONDS = 2.0


@pytest.fixture
def silent_server() -> Iterator[str]:
    """🧪 Serve a 200 header on 127.0.0.1, then stay silent until the test ends."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    stop = threading.Event()

    def serve() -> None:
        """🧪 Accept each connection, send the headers, and never send the body."""
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                stop.wait()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/index-headers.html"
    stop.set()
    thread.join()
    listener.close()


def test_time_limit_raises_only_for_slow_blocks_and_restores_handler() -> None:
    """A block past its limit raises TimeoutError, a fast block passes, and the old SIGALRM handler returns."""
    # Arrange
    previous = signal.getsignal(signal.SIGALRM)

    # Act and assert: a block that outlives the limit
    with pytest.raises(TimeoutError), limits.time_limit(0.05):
        time.sleep(1)
    assert signal.getsignal(signal.SIGALRM) is previous

    # Act and assert: a block that finishes in time
    with limits.time_limit(1.0):
        pass
    assert signal.getsignal(signal.SIGALRM) is previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_time_limit_stops_stalled_edgar_read(silent_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled EDGAR read is cut off near the limit, not after edgartools' retries and 30 s read timeouts."""
    # Arrange
    monkeypatch.setenv("EDGAR_IDENTITY", "earningsbot-tests tests@example.invalid")
    started = time.perf_counter()

    # Act
    with pytest.raises(TimeoutError, match="still running"), limits.time_limit(0.3):
        download_text(silent_server)

    # Assert
    assert time.perf_counter() - started < STALL_BOUND_SECONDS
