"""Wall-clock limits for slow network calls.

⏱️ Shared by edgar.py and discord.py, so neither module imports the other for it.
"""

import signal
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import FrameType


class _Alarm(BaseException):
    """🚨 Raised by SIGALRM inside the block.

    A BaseException, not an Exception: edgartools, httpx2, and httpcore2 turn an
    OSError from a socket read (a TimeoutError is one) into a retryable error, and
    `except Exception` clauses there would swallow the alarm and retry with no limit.
    """


@contextmanager
def time_limit(seconds: float) -> Generator[None]:
    """Raise TimeoutError when the block runs longer than ``seconds`` of wall-clock time.

    Uses SIGALRM, so only the main thread can set it. In any other thread the block
    runs with no limit instead of crashing. On exit the timer is set to zero and the
    previous SIGALRM handler is restored, even when the block raises. Time limits
    never nest, so the previous handler is never a live limit.
    """
    if threading.current_thread() is not threading.main_thread():
        # 🧵 signal.signal raises ValueError outside the main thread, so skip the limit.
        yield
        return

    def on_alarm(_signum: int, _frame: FrameType | None) -> None:
        """Interrupt the block, wherever it is stuck."""
        raise _Alarm

    previous = signal.signal(signal.SIGALRM, on_alarm)
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, seconds)
            yield
        finally:
            # ⏲️ A late alarm raises here, inside the outer try, so it is still converted.
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _Alarm:
        # 🔁 Convert at this boundary only, so callers see a plain TimeoutError.
        msg = f"still running after {seconds} seconds"
        raise TimeoutError(msg) from None
    finally:
        # 🧩 getsignal returns None for a handler set outside Python, and SIG_DFL is the closest restore.
        signal.signal(signal.SIGALRM, signal.SIG_DFL if previous is None else previous)
