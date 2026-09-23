"""Command-line interface for the foundational repository index."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .database import connect
from .repository import discover_files, sync_files
from .symbols import index_symbols


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codescout")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create or refresh a repository index")
    init_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    init_parser.add_argument("--db", type=Path)

    status_parser = subparsers.add_parser("status", help="show repository index status")
    status_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    status_parser.add_argument("--db", type=Path)
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    symbols_parser = subparsers.add_parser("symbols", help="list indexed symbols")
    symbols_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    symbols_parser.add_argument("--db", type=Path)
    symbols_parser.add_argument("--name")
    symbols_parser.add_argument("--limit", type=int, default=50)
    symbols_parser.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    db_path = (args.db or root / ".codebase" / "index.db").resolve()

    if not root.is_dir():
        raise SystemExit(f"repository root does not exist: {root}")

    if args.command == "init":
        return _init(root, db_path)
    if args.command == "status":
        return _status(root, db_path, args.as_json)
    if args.command == "symbols":
        return _symbols(db_path, args.name, args.limit, args.as_json)
    return 2


def _init(root: Path, db_path: Path) -> int:
    files = discover_files(root)
    with connect(db_path) as connection:
        file_count = sync_files(connection, files)
        symbol_count = index_symbols(connection, files)
        payload = _status_payload(root, db_path, connection)
    payload["discovered_files"] = file_count
    payload["indexed_symbols"] = symbol_count
    print(json.dumps(payload, indent=2))
    return 0


def _status(root: Path, db_path: Path, as_json: bool) -> int:
    with connect(db_path) as connection:
        payload = _status_payload(root, db_path, connection)
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"root: {payload['root']}")
        print(f"database: {payload['database']}")
        print(f"schema_version: {payload['schema_version']}")
        print(f"indexed_files: {payload['indexed_files']}")
        print(f"indexed_symbols: {payload['indexed_symbols']}")
    return 0


def _symbols(db_path: Path, name: str | None, limit: int, as_json: bool) -> int:
    if limit < 1:
        raise SystemExit("--limit must be positive")

    query = """
        SELECT symbols.qualified_name, symbols.kind, files.path,
               symbols.start_line, symbols.end_line, symbols.signature
        FROM symbols
        JOIN files ON files.id = symbols.file_id
    """
    parameters: list[object] = []
    if name:
        query += " WHERE symbols.simple_name LIKE ? OR symbols.qualified_name LIKE ?"
        pattern = f"%{name}%"
        parameters.extend([pattern, pattern])
    query += " ORDER BY files.path, symbols.start_line LIMIT ?"
    parameters.append(limit)

    with connect(db_path) as connection:
        rows = [dict(row) for row in connection.execute(query, parameters)]

    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(
                f"{row['path']}:{row['start_line']}-{row['end_line']} "
                f"{row['kind']} {row['qualified_name']}"
            )
    return 0




def _status_payload(root: Path, db_path: Path, connection: sqlite3.Connection) -> dict[str, object]:
    schema_version = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    indexed_files = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return {
        "root": str(root),
        "database": str(db_path),
        "schema_version": schema_version,
        "indexed_files": indexed_files,
        "indexed_symbols": connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
    }
