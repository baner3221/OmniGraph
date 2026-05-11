"""
OmniGraph Incremental Build Detection

SHA-256 file hashing with SQLite storage for skipping unchanged files.
Uses a 64KB read buffer for memory-efficient hashing of large files.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 64KB read buffer — keeps memory usage constant regardless of file size
HASH_BUFFER_SIZE = 65536

DEFAULT_HASH_DB_PATH = Path("data/cache/file_hashes.db")


class FileHasher:
    """
    Incremental build detection via SHA-256 file hashing.

    Maintains a SQLite database of file paths → SHA-256 digests.
    On each run, only files whose content has changed are re-parsed.
    """

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS file_hashes (
        filepath    TEXT PRIMARY KEY,
        sha256      TEXT NOT NULL,
        last_parsed TEXT NOT NULL
    );
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_HASH_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the hash cache database."""
        self._conn = sqlite3.connect(str(self.db_path), timeout=15)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self.SCHEMA_SQL)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_db()
        return self._conn

    @staticmethod
    def compute_hash(filepath: str) -> str:
        """
        Compute SHA-256 hash of a file using a streaming 64KB buffer.

        Memory usage is O(1) regardless of file size — critical for
        processing 20M-line codebases without memory exhaustion.
        """
        sha256 = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(HASH_BUFFER_SIZE)
                    if not chunk:
                        break
                    sha256.update(chunk)
        except (OSError, IOError) as e:
            logger.warning("Failed to hash file %s: %s", filepath, e)
            return ""
        return sha256.hexdigest()

    def has_changed(self, filepath: str) -> bool:
        """
        Check if a file has changed since the last successful parse.

        Args:
            filepath: Absolute path to the source file.

        Returns:
            True if the file is new or has changed, False if unchanged.
        """
        current_hash = self.compute_hash(filepath)
        if not current_hash:
            return True  # If we can't hash it, assume changed

        row = self.conn.execute(
            "SELECT sha256 FROM file_hashes WHERE filepath = ?",
            (filepath,),
        ).fetchone()

        if row is None:
            return True  # New file
        return row[0] != current_hash

    def update_hash(self, filepath: str) -> None:
        """
        Store or update the hash for a file after successful parsing.

        Args:
            filepath: Absolute path to the source file.
        """
        current_hash = self.compute_hash(filepath)
        if not current_hash:
            return

        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO file_hashes (filepath, sha256, last_parsed)
            VALUES (?, ?, ?)
            """,
            (filepath, current_hash, now),
        )
        self.conn.commit()

    def get_changed_files(self, file_list: list[str]) -> list[str]:
        """
        Filter a list of files, returning only those that need re-parsing.

        This is the primary API for incremental builds.

        Args:
            file_list: List of absolute file paths to check.

        Returns:
            List of file paths that are new or have changed.
        """
        changed = []
        for filepath in file_list:
            if self.has_changed(filepath):
                changed.append(filepath)
        logger.info(
            "Incremental filter: %d/%d files need re-parsing",
            len(changed),
            len(file_list),
        )
        return changed

    def bulk_update(self, filepaths: list[str]) -> None:
        """
        Batch update hashes for multiple files after successful parsing.

        Args:
            filepaths: List of absolute file paths to update.
        """
        now = datetime.now(timezone.utc).isoformat()
        records = []
        for filepath in filepaths:
            h = self.compute_hash(filepath)
            if h:
                records.append((filepath, h, now))

        if records:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO file_hashes (filepath, sha256, last_parsed)
                VALUES (?, ?, ?)
                """,
                records,
            )
            self.conn.commit()
            logger.info("Updated hashes for %d files", len(records))

    def clear(self) -> None:
        """Clear all stored hashes. Used for full re-index."""
        self.conn.execute("DELETE FROM file_hashes")
        self.conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> FileHasher:
        return self

    def __exit__(self, *args) -> None:
        self.close()
