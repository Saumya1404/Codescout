from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from jev_codebase_explore.cli import main
from jev_codebase_explore.context import build_context_pack
from jev_codebase_explore.database import connect
from jev_codebase_explore.git_evidence import refresh_git_index
from jev_codebase_explore.jev import Candidate, GatewayJevClient, GatewayJevRanker
from jev_codebase_explore.repository import discover_files, sync_files
from jev_codebase_explore.relationships import index_relationships
from jev_codebase_explore.search import rebuild_search_index, search
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
                    "4",
                )

    def test_symbol_index_persists_python_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                sync_files(connection, files)
                symbol_count = index_symbols(connection, files)
                rebuild_search_index(connection, files)
                symbols = connection.execute(
                    """
                    SELECT qualified_name, kind, start_line
                    FROM symbols
                    WHERE kind != 'module'
                    ORDER BY qualified_name
                    """
                ).fetchall()

            self.assertEqual(symbol_count, 5)
            self.assertEqual(
                [(row[0], row[1]) for row in symbols],
                [
                    ("test_partial_refund_is_valid", "function"),
                    ("validate_refund", "function"),
                ],
            )

    def test_relationship_index_resolves_calls_and_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                sync_files(connection, files)
                index_symbols(connection, files)
                edge_count = index_relationships(connection, files)
                rows = connection.execute(
                    """
                    SELECT edge_type, source_symbol.qualified_name,
                           target_symbol.qualified_name
                    FROM edges
                    JOIN symbols AS source_symbol ON source_symbol.id = edges.source_symbol_id
                    JOIN symbols AS target_symbol ON target_symbol.id = edges.target_symbol_id
                    WHERE source_symbol.simple_name = 'test_partial_refund_is_valid'
                    ORDER BY edge_type
                    """
                ).fetchall()
                test_count = connection.execute("SELECT COUNT(*) FROM tests").fetchone()[0]

            self.assertGreaterEqual(edge_count, 2)
            self.assertIn(
                ("CALLS", "test_partial_refund_is_valid", "validate_refund"),
                [tuple(row) for row in rows],
            )
            self.assertIn(
                ("TESTS", "test_partial_refund_is_valid", "validate_refund"),
                [tuple(row) for row in rows],
            )
            self.assertEqual(test_count, 1)

    def test_fts_search_returns_ranked_symbol_and_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                sync_files(connection, files)
                index_symbols(connection, files)
                rebuild_search_index(connection, files)
                refresh_git_index(connection, FIXTURE_ROOT)
                results = search(connection, "validate refund", limit=10)

            self.assertTrue(results)
            self.assertEqual(results[0].name, "validate_refund")
            self.assertEqual(results[0].path, "src/shop/refunds.py")
            self.assertGreater(results[0].score, 0)
            self.assertTrue(results[0].matched_fields)

    def test_fts_exact_mode_does_not_use_prefix_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)

            with connect(db_path) as connection:
                sync_files(connection, files)
                index_symbols(connection, files)
                rebuild_search_index(connection, files)
                prefix_results = search(connection, "validate_ref", exact=False)
                exact_results = search(connection, "validate_ref", exact=True)

            self.assertTrue(prefix_results)
            self.assertFalse(exact_results)

    def test_context_pack_preserves_primary_source_and_budget_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "index.db"
            files = discover_files(FIXTURE_ROOT)
            issue = "Partial refunds are accepted when the paid amount is sufficient."

            with connect(db_path) as connection:
                sync_files(connection, files)
                index_symbols(connection, files)
                index_relationships(connection, files)
                rebuild_search_index(connection, files)
                pack = build_context_pack(
                    connection,
                    FIXTURE_ROOT,
                    issue,
                    symbol="validate_refund",
                    max_tokens=500,
                )

            self.assertEqual(pack.primary["qualified_name"], "validate_refund")
            self.assertIn("validate_refund", pack.render_markdown())
            self.assertGreater(pack.estimated_tokens, 0)
            self.assertEqual(pack.token_limit, 500)

    def test_gateway_jev_ranker_parses_typed_answers_and_usage(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "request_id": "req_test",
                    "answers": {
                        "relevant_candidate": {
                            "type": "choice",
                            "choice": "symbol:3",
                            "probabilities": {"symbol:3": 0.9, "file:6": 0.1},
                        },
                        "any_candidate_fits": {"type": "boolean", "probability": 0.96},
                    },
                    "usage": {"input_tokens": 321, "output_tokens": 0},
                },
                request=request,
            )

        client = GatewayJevClient(
            api_key="test-key",
            transport=httpx.MockTransport(handler),
        )
        result = GatewayJevRanker(client).rank_candidates(
            "partial refund validation",
            [
                Candidate("symbol:3", "src/refunds.py", "validate_refund", "function", 1, 2, "return amount <= paid", 1.0),
                Candidate("file:6", "tests/test_refunds.py", "test_refund", "file", 1, 5, "test refund", 0.5),
            ],
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-key")
        self.assertEqual(result.selected_candidate, "symbol:3")
        self.assertEqual(result.input_tokens, 321)
        self.assertEqual(result.any_candidate_probability, 0.96)

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
