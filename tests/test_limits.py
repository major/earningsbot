"""Tests for the wall-clock limit helper and its use on a real stalled read."""

import signal
import socket
import threading
import time
from email.utils import formatdate
from typing import TYPE_CHECKING

import pytest
from edgar import httpclient
from edgar.httprequests import download_text

import earningsbot
from earningsbot import limits

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# ⏱️ Far above the 0.3 s limit, far below the 30 s edgartools read timeout and the retry budget.
STALL_BOUND_SECONDS = 2.0
FULL_BODY = b"<html>the full index-headers page</html>"
STALLED_THEN_RETRIED = 2  # 🔁 One request that stalls, then one retry


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


@pytest.fixture
def stall_once_server() -> Iterator[tuple[str, list[bytes]]]:
    """🧪 Stall the first request after its 200 headers, then answer every later request in full.

    Each connection gets its own thread, so the stalled one never blocks the next request.
    The list holds one entry per request the server received.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    stop = threading.Event()
    received: list[bytes] = []
    workers: list[threading.Thread] = []
    # 📅 SEC always sends a Date header, and the cache needs it to mark an entry as fetched.
    headers = (
        f"HTTP/1.1 200 OK\r\nDate: {formatdate(usegmt=True)}\r\n"
        f"Content-Length: {len(FULL_BODY)}\r\nConnection: close\r\n\r\n"
    ).encode()

    def handle(conn: socket.socket, *, stall: bool) -> None:
        """🧪 Send the headers, then either stall until teardown or send the full body."""
        with conn:
            received.append(conn.recv(4096))
            conn.sendall(headers)
            if stall:
                stop.wait()
            else:
                conn.sendall(FULL_BODY)

    def serve() -> None:
        """🧪 Accept connections, and stall only the first one."""
        stall = True
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            worker = threading.Thread(target=handle, args=(conn,), kwargs={"stall": stall}, daemon=True)
            worker.start()
            workers.append(worker)
            stall = False

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/index-headers.html", received
    stop.set()
    server.join()
    for worker in workers:
        worker.join()
    listener.close()


def test_time_limited_download_is_not_cached_as_a_truncated_body(
    stall_once_server: tuple[str, list[bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download cut off by the time limit is not cached, so the retry gets the full body from the server."""
    # Arrange: a forever-cache rule like the SEC Archives rule, and a temp cache dir so the real cache is untouched.
    # The cache starts on, as edgartools sets it, and then the startup switch under test turns it off.
    url, received = stall_once_server
    monkeypatch.setenv("EDGAR_IDENTITY", "earningsbot-tests tests@example.invalid")
    monkeypatch.setenv("EDGAR_LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(httpclient, "CACHE_RULES", {"127.0.0.1": {"/index-headers.html": True}})
    monkeypatch.setattr(httpclient, "HTTP_MGR", httpclient.get_http_mgr())
    earningsbot.disable_edgar_http_cache()

    # Act
    with pytest.raises(TimeoutError, match="still running"), limits.time_limit(0.3):
        download_text(url)
    retried = download_text(url)

    # Assert
    assert retried == FULL_BODY.decode()
    assert len(received) == STALLED_THEN_RETRIED
