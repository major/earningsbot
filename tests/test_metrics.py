"""Tests for reading revenue and EPS from earnings releases and formatting them."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from earningsbot.metrics import (
    Metrics,
    extract_metrics,
    fetch_metrics,
    format_eps,
    format_revenue,
)

if TYPE_CHECKING:
    from earningsbot.edgar import Filing

QUARTER = "Three Months Ended September 30, 2026"
# 🧪 fetch_metrics only hands the filing to from_filing, so a stub is enough.
FILING = cast("Filing", object())


def _key_metrics(**values: object) -> dict[str, object]:
    """🧪 Build key metrics like EarningsRelease.get_key_metrics, None by default."""
    base: dict[str, object] = {
        "revenue": None,
        "net_income": None,
        "eps_basic": None,
        "eps_diluted": None,
        "period": QUARTER,
    }
    return base | values


@dataclass
class FakeRelease:
    """🧪 Stand-in for an edgartools EarningsRelease with fixed key metrics."""

    metrics: dict[str, object]

    def get_key_metrics(self) -> dict[str, object]:
        """🧪 Return the canned key metrics, as edgartools does."""
        return self.metrics


@pytest.mark.parametrize(
    ("key_metrics", "expected"),
    [
        pytest.param(
            _key_metrics(
                revenue=19_589_000.0,
                eps_basic=-1.06,
                eps_diluted=-1.06,
                period="Three Months Ended - September 30, 2026",
            ),
            Metrics(revenue=19_589_000.0, eps=-1.06, eps_basis="diluted"),
            id="geospace-diluted",
        ),
        pytest.param(
            _key_metrics(
                revenue=44_848_000.0,
                eps_basic=-0.11,
                period="Three Months\n\n\nEnded September\n\n\n30, 2026",
            ),
            Metrics(revenue=44_848_000.0, eps=-0.11, eps_basis="basic"),
            id="hyperscale-basic-only",
        ),
        pytest.param(
            _key_metrics(revenue=4.2, period="Three Months Ended September 30, - 2026"),
            Metrics(),
            id="post-unscaled-revenue",
        ),
        pytest.param(
            _key_metrics(net_income=7_300_000.0, period="U.S. CLO Debt"),
            Metrics(),
            id="ellington-wrong-table",
        ),
        pytest.param(
            _key_metrics(
                revenue=80_000_000.0,
                eps_diluted=2.5,
                period="Year Ended September 30, 2026",
            ),
            Metrics(),
            id="annual-table-skipped",
        ),
        pytest.param(
            _key_metrics(revenue=80_000_000.0, eps_diluted=2.5, period=None),
            Metrics(),
            id="missing-period-skipped",
        ),
        pytest.param(_key_metrics(), Metrics(), id="caleres-all-missing"),
        pytest.param(_key_metrics(revenue=True), Metrics(), id="bool-revenue"),
        pytest.param(_key_metrics(eps_diluted=True), Metrics(), id="bool-eps"),
        pytest.param(
            _key_metrics(eps_diluted=float("nan"), eps_basic=-0.5),
            Metrics(eps=-0.5, eps_basis="basic"),
            id="nan-diluted-falls-back-to-basic",
        ),
        pytest.param(_key_metrics(eps_diluted=1500.0), Metrics(), id="eps-too-large"),
    ],
)
def test_extract_metrics(key_metrics: dict[str, object], expected: Metrics) -> None:
    """Only a quarterly table with plausible, finite values yields metrics."""
    # Arrange, Act
    result = extract_metrics(key_metrics)

    # Assert
    assert result == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (19_589_000, "$19.6M"),
        (4_200_000_000, "$4.20B"),
        (1.5e12, "$1.50T"),
    ],
)
def test_format_revenue(value: float, expected: str) -> None:
    """Revenue uses the largest unit that fits, with one or two decimals."""
    # Arrange, Act
    result = format_revenue(value)

    # Assert
    assert result == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.64, "$0.64"),
        (-1.06, "-$1.06"),
    ],
)
def test_format_eps(value: float, expected: str) -> None:
    """EPS has two decimals, with the minus sign before the dollar sign."""
    # Arrange, Act
    result = format_eps(value)

    # Assert
    assert result == expected


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        pytest.param(None, Metrics(), id="no-earnings-exhibit"),
        pytest.param(
            FakeRelease(_key_metrics(revenue=19_589_000.0, eps_diluted=-1.06)),
            Metrics(revenue=19_589_000.0, eps=-1.06, eps_basis="diluted"),
            id="release-with-metrics",
        ),
    ],
)
def test_fetch_metrics(release: FakeRelease | None, expected: Metrics) -> None:
    """No release gives empty metrics; a release gives its extracted values."""

    # Arrange
    def from_filing(_filing: object) -> FakeRelease | None:
        """🧪 Return the canned release, as EarningsRelease.from_filing would."""
        return release

    # Act
    result = fetch_metrics(FILING, from_filing=from_filing)

    # Assert
    assert result == expected
