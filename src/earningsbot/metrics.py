"""Read revenue and EPS from an earnings release, and format them for Discord.

📊 Extraction is strict: a value that looks wrong is left out, never shown.
"""

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from edgar.earnings import EarningsRelease

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from earningsbot.edgar import Filing

MIN_REVENUE = 1_000_000
MAX_EPS = 1000
_QUARTER_MARK = "three months"
_MILLION = 1_000_000
_BILLION = 1_000_000_000
_TRILLION = 1_000_000_000_000


@dataclass(frozen=True)
class Metrics:
    """Quarterly revenue in dollars and EPS per share, or None and "" when unknown."""

    revenue: float | None = None
    eps: float | None = None
    eps_basis: str = ""


class _Release(Protocol):
    """🧩 The part of an edgartools EarningsRelease this module reads."""

    def get_key_metrics(self) -> Mapping[str, object]:
        """Return the key metrics that edgartools found in the release."""


def extract_metrics(key_metrics: Mapping[str, object]) -> Metrics:
    """Pick revenue and EPS from edgartools key metrics, or return empty Metrics."""
    # 🧭 Only a quarterly income statement is safe to show, so other tables are skipped.
    period = key_metrics.get("period")
    # 🧹 Line breaks inside table cells would break the quarter check, so collapse them.
    text = " ".join(period.split()) if isinstance(period, str) else ""
    if _QUARTER_MARK not in text.lower():
        return Metrics()
    eps, basis = _eps(key_metrics)
    return Metrics(
        revenue=_revenue(key_metrics.get("revenue")),
        eps=eps,
        eps_basis=basis,
    )


def fetch_metrics(
    filing: Filing,
    from_filing: Callable[[Any], _Release | None] = EarningsRelease.from_filing,
) -> Metrics:
    """Read the metrics from the filing's earnings exhibit, or empty Metrics when none.

    Exceptions from edgartools propagate, so the caller decides how to handle them.
    """
    release = from_filing(filing)
    if release is None:
        return Metrics()
    return extract_metrics(release.get_key_metrics())


def format_revenue(value: float) -> str:
    """Format dollars with the largest unit that fits, such as $19.6M or $4.20B."""
    if value < _BILLION:
        return f"${value / _MILLION:.1f}M"
    if value < _TRILLION:
        return f"${value / _BILLION:.2f}B"
    return f"${value / _TRILLION:.2f}T"


def format_eps(value: float) -> str:
    """Format EPS with two decimals and the minus sign before the dollar sign."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f}"


def _revenue(value: object) -> float | None:
    """Return revenue in dollars when it is plausible for one quarter, else None."""
    number = _finite_number(value)
    # 🚫 Post Holdings reported 4.2 (unscaled billions), so tiny values are omitted.
    if number is None or number < MIN_REVENUE:
        return None
    return number


def _eps(key_metrics: Mapping[str, object]) -> tuple[float | None, str]:
    """Return (value, basis) from diluted EPS, else basic EPS, else (None, "")."""
    for key, basis in (("eps_diluted", "diluted"), ("eps_basic", "basic")):
        number = _finite_number(key_metrics.get(key))
        # 🛡️ An EPS this large is almost always a units or table mistake.
        if number is not None and abs(number) < MAX_EPS:
            return number, basis
    return None, ""


def _finite_number(value: object) -> float | None:
    """Return the value as a float when it is a real, finite number, else None."""
    # 🔢 bool is a subclass of int, so True would pass as 1 without this check.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return float(value)
