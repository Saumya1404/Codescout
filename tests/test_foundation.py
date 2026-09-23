from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jev_codebase_explore.cli import main
from jev_codebase_explore.database import connect
from jev_codebase_explore.repository import discover_files, sync_files


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
                    "1",
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
