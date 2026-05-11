"""
OmniGraph Global Symbol Table (GST)

SQLite-backed registry for cross-file symbol resolution.
Supports concurrent reads via WAL mode and serialized writes
through the orchestrator's writer queue.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default GST database path
DEFAULT_GST_PATH = Path("data/cache/global_symbols.db")


class GlobalSymbolTable:
    """
    SQLite-backed Global Symbol Table for cross-file symbol resolution.

    Used by:
    - C++ parser: registers USR-based symbols
    - Java parser Pass 1: registers FQN-based symbols
    - Java solver Pass 2: resolves MethodInvocation targets
    """

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS symbols (
        fqn         TEXT PRIMARY KEY,
        usr         TEXT UNIQUE,
        kind        TEXT NOT NULL,
        file        TEXT NOT NULL,
        line        INTEGER NOT NULL,
        language    TEXT NOT NULL,
        signature   TEXT,
        parent_fqn  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
    CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
    CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent_fqn);
    CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(fqn);
    CREATE INDEX IF NOT EXISTS idx_symbols_usr ON symbols(usr);
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_GST_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database with WAL mode and schema."""
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self._conn.executescript(self.SCHEMA_SQL)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_db()
        return self._conn

    def register(
        self,
        fqn: str,
        usr: str,
        kind: str,
        file: str,
        line: int,
        language: str,
        signature: Optional[str] = None,
        parent_fqn: Optional[str] = None,
    ) -> None:
        """Register a single symbol in the GST."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO symbols
                (fqn, usr, kind, file, line, language, signature, parent_fqn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fqn, usr, kind, file, line, language, signature, parent_fqn),
        )
        self.conn.commit()

    def bulk_register(self, records: list[dict]) -> int:
        """
        Batch insert symbols for high throughput.

        Args:
            records: List of dicts with keys matching the symbols table columns.

        Returns:
            Number of records inserted.
        """
        if not records:
            return 0

        self.conn.executemany(
            """
            INSERT OR REPLACE INTO symbols
                (fqn, usr, kind, file, line, language, signature, parent_fqn)
            VALUES
                (:fqn, :usr, :kind, :file, :line, :language, :signature, :parent_fqn)
            """,
            records,
        )
        self.conn.commit()
        return len(records)

    def resolve(
        self,
        name: str,
        context_fqn: Optional[str] = None,
        imports: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """
        Resolve a simple name to a fully qualified symbol.

        Resolution order:
        1. Check if `name` is already an FQN (exact match)
        2. Check within the context class (parent_fqn match)
        3. Check against import list (prefix match)
        4. Fuzzy match by suffix

        Args:
            name: Simple method/class name to resolve.
            context_fqn: FQN of the enclosing class (for `this.method()` calls).
            imports: List of import FQNs from the source file.

        Returns:
            Symbol dict if found, None otherwise.
        """
        # 1. Exact FQN match
        row = self.conn.execute(
            "SELECT * FROM symbols WHERE fqn = ?", (name,)
        ).fetchone()
        if row:
            return dict(row)

        # 2. Context-scoped resolution (same class)
        if context_fqn:
            candidate_fqn = f"{context_fqn}.{name}"
            row = self.conn.execute(
                "SELECT * FROM symbols WHERE fqn LIKE ?", (f"%{candidate_fqn}%",)
            ).fetchone()
            if row:
                return dict(row)

            # Also check parent class methods
            row = self.conn.execute(
                "SELECT * FROM symbols WHERE parent_fqn = ? AND fqn LIKE ?",
                (context_fqn, f"%.{name}%"),
            ).fetchone()
            if row:
                return dict(row)

        # 3. Import-based resolution
        if imports:
            for imp in imports:
                if imp.endswith(f".{name}") or imp.endswith(".*"):
                    package = imp.rsplit(".", 1)[0] if imp.endswith(".*") else imp.rsplit(".", 1)[0]
                    candidate = f"{package}.{name}"
                    row = self.conn.execute(
                        "SELECT * FROM symbols WHERE fqn LIKE ?", (f"{candidate}%",)
                    ).fetchone()
                    if row:
                        return dict(row)

        # 4. Suffix match (last resort)
        row = self.conn.execute(
            "SELECT * FROM symbols WHERE fqn LIKE ?", (f"%.{name}",)
        ).fetchone()
        if row:
            return dict(row)

        return None

    def resolve_method(
        self,
        class_fqn: str,
        method_name: str,
        arg_count: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Resolve a method call on a known class.

        Args:
            class_fqn: Fully qualified name of the class.
            method_name: Name of the method being called.
            arg_count: Number of arguments (for overload disambiguation).

        Returns:
            Symbol dict if found, None otherwise.
        """
        if arg_count is not None:
            # Try exact match with signature arg count
            rows = self.conn.execute(
                """
                SELECT * FROM symbols
                WHERE parent_fqn = ? AND fqn LIKE ?
                ORDER BY fqn
                """,
                (class_fqn, f"%.{method_name}%"),
            ).fetchall()
            for row in rows:
                row_dict = dict(row)
                sig = row_dict.get("signature", "")
                if sig:
                    # Count commas + 1 for param count (rough heuristic)
                    param_count = sig.count(",") + 1 if sig.strip() else 0
                    if param_count == arg_count:
                        return row_dict
            # Fallback: return first match
            if rows:
                return dict(rows[0])
        else:
            row = self.conn.execute(
                """
                SELECT * FROM symbols
                WHERE parent_fqn = ? AND fqn LIKE ?
                LIMIT 1
                """,
                (class_fqn, f"%.{method_name}%"),
            ).fetchone()
            if row:
                return dict(row)

        return None

    def get_class_methods(self, class_fqn: str) -> list[dict]:
        """Get all methods belonging to a class."""
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE parent_fqn = ? AND kind IN ('method', 'function', 'constructor')",
            (class_fqn,),
        ).fetchall()
        return [dict(r) for r in rows]

    def lookup_fqn(self, fqn: str) -> Optional[dict]:
        """Direct FQN lookup."""
        row = self.conn.execute(
            "SELECT * FROM symbols WHERE fqn = ?", (fqn,)
        ).fetchone()
        return dict(row) if row else None

    def lookup_usr(self, usr: str) -> Optional[dict]:
        """Direct USR lookup."""
        row = self.conn.execute(
            "SELECT * FROM symbols WHERE usr = ?", (usr,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_classes(self, language: Optional[str] = None) -> list[dict]:
        """Get all class symbols, optionally filtered by language."""
        if language:
            rows = self.conn.execute(
                "SELECT * FROM symbols WHERE kind IN ('class', 'interface') AND language = ?",
                (language,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM symbols WHERE kind IN ('class', 'interface')"
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """Total number of symbols registered."""
        row = self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
        return row[0] if row else 0

    def clear(self) -> None:
        """Drop all symbols. Used for full re-index."""
        self.conn.execute("DELETE FROM symbols")
        self.conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> GlobalSymbolTable:
        return self

    def __exit__(self, *args) -> None:
        self.close()
