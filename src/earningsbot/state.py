"""Remember which filings were already handled, across restarts, in SQLite.

💾 Each mark is committed right away, so a crash cannot forget a filing that
was already sent.
"""

import sqlite3
from pathlib import Path


class SeenStore:
    """🗄️ A set of accession numbers kept in a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        """Open or create the store at ``path``, making parent folders as needed."""
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(file_path)
        self._conn.execute("CREATE TABLE IF NOT EXISTS seen_filings (accession_no TEXT PRIMARY KEY)")
        self._conn.commit()

    def __contains__(self, accession_no: object) -> bool:
        """Return True when ``accession_no`` was added before."""
        # 🔢 Only text can be an accession number, so any other key is never seen.
        if not isinstance(accession_no, str):
            return False
        row = self._conn.execute("SELECT 1 FROM seen_filings WHERE accession_no = ?", (accession_no,)).fetchone()
        return row is not None

    def add(self, accession_no: str) -> None:
        """Mark ``accession_no`` as seen and commit now. Adding it twice is harmless."""
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_filings (accession_no) VALUES (?)",
            (accession_no,),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
