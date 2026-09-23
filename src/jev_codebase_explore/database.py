"""SQLite storage for the codebase index."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = "1"


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    language TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    modified_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    qualified_name TEXT NOT NULL,
    simple_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    signature TEXT,
    docstring TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_byte INTEGER NOT NULL,
    end_byte INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    source_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    target_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,
    resolution_method TEXT NOT NULL,
    confidence REAL,
    PRIMARY KEY (source_symbol_id, target_symbol_id, edge_type)
);

CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    test_name TEXT NOT NULL,
    framework TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS git_changes (
    id INTEGER PRIMARY KEY,
    commit_hash TEXT NOT NULL,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    change_type TEXT NOT NULL,
    lines_added INTEGER NOT NULL DEFAULT 0,
    lines_deleted INTEGER NOT NULL DEFAULT 0,
    timestamp REAL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS symbols_file_id_idx ON symbols(file_id);
CREATE INDEX IF NOT EXISTS symbols_simple_name_idx ON symbols(simple_name);
CREATE INDEX IF NOT EXISTS edges_source_idx ON edges(source_symbol_id);
CREATE INDEX IF NOT EXISTS edges_target_idx ON edges(target_symbol_id);
CREATE INDEX IF NOT EXISTS tests_file_id_idx ON tests(file_id);
"""


class DatabaseConnection(sqlite3.Connection):
    """SQLite connection that also closes when used as a context manager."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: Path) -> DatabaseConnection:
    """Open an index database and initialize its foundational schema."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, factory=DatabaseConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """Create the schema without deleting existing index data."""

    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    connection.commit()
