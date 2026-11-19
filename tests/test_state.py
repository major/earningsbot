"""Tests for the SQLite store of filings already handled."""

from typing import TYPE_CHECKING

from earningsbot.state import SeenStore

if TYPE_CHECKING:
    from pathlib import Path

ACCESSION = "0001-26-000001"
OTHER_ACCESSION = "0001-26-000002"
NON_TEXT_KEY = 42


def test_seen_store_persists_across_reopen(tmp_path: Path) -> None:
    """An accession added before close is still seen after the file is reopened."""
    path = tmp_path / "seen.sqlite3"
    store = SeenStore(path)
    store.add(ACCESSION)
    store.close()

    reopened = SeenStore(path)

    assert ACCESSION in reopened
    assert OTHER_ACCESSION not in reopened
    # 🔢 Non-text keys are never seen, which matches how a set behaves.
    assert NON_TEXT_KEY not in reopened
    reopened.close()


def test_seen_store_creates_parent_dirs_and_ignores_duplicates(tmp_path: Path) -> None:
    """Missing parent folders get created, and a duplicate add is harmless."""
    path = tmp_path / "nested" / "state" / "seen.sqlite3"
    store = SeenStore(path)

    store.add(ACCESSION)
    store.add(ACCESSION)

    assert path.is_file()
    assert ACCESSION in store
    store.close()
