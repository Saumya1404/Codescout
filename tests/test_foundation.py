from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jev_codebase_explore.cli import main
from jev_codebase_explore.database import connect
from jev_codebase_explore.repository import discover_files, sync_files
from jev_codebase_explore.symbols import index_symbols, parse_source


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "tiny_repo"


class FoundationTests(unittest.TestCase):
    def test_discovery_finds_fixture_files_and_skips_ignored_directories(self) -> None:
        files = discover_files(FIXTURE_ROOT)
        relative_paths = {file.relative_path for file in files}

        self.assertIn("src/shop/refunds.py", relative_paths)
        self.assertIn("tests/test_refunds.py", relative_paths)
        self.assertNotIn("ignored.py", relative_paths)
        self.assertEqual(
            next(file for file in files if file.relative_path == "src/shop/refunds.py").language,
            "python",
        )

    def test_database_initializes_schema_and_syncs_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / ".codebase" / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                self.assertEqual(sync_files(connection, files), len(files))
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    len(files),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "2",
                )

    def test_symbol_index_persists_python_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                sync_files(connection, files)
                symbol_count = index_symbols(connection, files)
                symbols = connection.execute(
                    """
                    SELECT qualified_name, kind, start_line
                    FROM symbols
                    ORDER BY qualified_name
                    """
                ).fetchall()

            self.assertEqual(symbol_count, 2)
            self.assertEqual(
                [(row[0], row[1]) for row in symbols],
                [
                    ("test_partial_refund_is_valid", "function"),
                    ("validate_refund", "function"),
                ],
            )

    def test_file_sync_removes_stale_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                sync_files(connection, files)
                connection.execute(
                    "INSERT INTO files(path, language, size_bytes, content_hash, modified_at) "
                    "VALUES ('deleted.py', 'python', 0, 'stale', 0)"
                )
                connection.commit()
                sync_files(connection, files)
                stale_count = connection.execute(
                    "SELECT COUNT(*) FROM files WHERE path = 'deleted.py'"
                ).fetchone()[0]

            self.assertEqual(stale_count, 0)

    def test_symbol_parser_supports_javascript_typescript_and_tsx(self) -> None:
        javascript_symbols = parse_source(
            "demo.js",
            "javascript",
            b"class Box { method(value) { return value; } }\nfunction run() {}\n",
        )
        typescript_symbols = parse_source(
            "demo.ts",
            "typescript",
            b"interface User { name: string }\ntype ID = string;\n",
        )
        tsx_symbols = parse_source(
            "demo.tsx",
            "tsx",
            b"interface Props { title: string }\nfunction View(props: Props) { return <div>{props.title}</div>; }\n",
        )

        self.assertEqual(
            [(symbol.qualified_name, symbol.kind) for symbol in javascript_symbols],
            [("Box", "class"), ("Box.method", "method"), ("run", "function")],
        )
        self.assertEqual(
            [(symbol.qualified_name, symbol.kind) for symbol in typescript_symbols],
            [("User", "interface"), ("ID", "type")],
        )
        self.assertEqual(
            [(symbol.qualified_name, symbol.kind) for symbol in tsx_symbols],
            [("Props", "interface"), ("View", "function")],
        )

    def test_cli_init_creates_index_and_reports_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"

            exit_code = main(
                ["init", str(FIXTURE_ROOT), "--db", str(db_path)]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(db_path.exists())

            with connect(db_path) as connection:
                indexed_files = connection.execute(
                    "SELECT COUNT(*) FROM files"
                ).fetchone()[0]
            self.assertGreater(indexed_files, 0)

    def test_database_foreign_keys_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with connect(Path(temporary_directory) / "index.db") as connection:
                foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(foreign_keys, 1)


if __name__ == "__main__":
    unittest.main()
