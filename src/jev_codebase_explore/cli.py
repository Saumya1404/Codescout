"""Command-line interface for the foundational repository index."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from .database import connect
from .context import build_context_pack
from .git_evidence import GitEvidence, refresh_git_index
from .jev import GatewayJevRanker, HeuristicRanker, candidates_from_search
from .repository import discover_files, sync_files
from .relationships import index_relationships
from .search import rebuild_search_index, search
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

    search_parser = subparsers.add_parser("search", help="search indexed files and symbols")
    search_parser.add_argument("query")
    search_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    search_parser.add_argument("--db", type=Path)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--exact", action="store_true")
    search_parser.add_argument("--json", action="store_true", dest="as_json")

    context_parser = subparsers.add_parser("context-pack", help="build a deterministic context pack")
    context_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    context_parser.add_argument("--issue", required=True, type=Path)
    context_parser.add_argument("--symbol")
    context_parser.add_argument("--max-tokens", type=int, default=5000)
    context_parser.add_argument("--db", type=Path)
    context_parser.add_argument("--json", action="store_true", dest="as_json")

    investigate_parser = subparsers.add_parser("investigate", help="rank issue candidates")
    investigate_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    investigate_parser.add_argument("--issue", required=True, type=Path)
    investigate_parser.add_argument("--ranker", choices=("heuristic", "jev"), default="heuristic")
    investigate_parser.add_argument("--limit", type=int, default=20)
    investigate_parser.add_argument("--db", type=Path)
    investigate_parser.add_argument("--json", action="store_true", dest="as_json")

    compare_parser = subparsers.add_parser("compare", help="compare heuristic and Jev ranking efficiency")
    compare_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    compare_parser.add_argument("--issue", required=True, type=Path)
    compare_parser.add_argument("--limit", type=int, default=20)
    compare_parser.add_argument("--db", type=Path)
    compare_parser.add_argument("--json", action="store_true", dest="as_json")

    for git_command in ("git-history", "blame", "cochanges"):
        git_parser = subparsers.add_parser(git_command, help=f"show Git {git_command} evidence")
        git_parser.add_argument("path")
        git_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
        git_parser.add_argument("--limit", type=int, default=10)
        git_parser.add_argument("--start", type=int, default=1)
        git_parser.add_argument("--end", type=int)
        git_parser.add_argument("--json", action="store_true", dest="as_json")

    symbols_parser = subparsers.add_parser("symbols", help="list indexed symbols")
    symbols_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    symbols_parser.add_argument("--db", type=Path)
    symbols_parser.add_argument("--name")
    symbols_parser.add_argument("--limit", type=int, default=50)
    symbols_parser.add_argument("--json", action="store_true", dest="as_json")

    for relationship_name in ("references", "callers", "callees", "impact"):
        relationship_parser = subparsers.add_parser(
            relationship_name,
            help=f"list {relationship_name} for a symbol",
        )
        relationship_parser.add_argument("symbol")
        relationship_parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
        relationship_parser.add_argument("--db", type=Path)
        relationship_parser.add_argument("--json", action="store_true", dest="as_json")

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
    if args.command == "search":
        return _search(db_path, args.query, args.limit, args.exact, args.as_json)
    if args.command == "context-pack":
        return _context_pack(root, db_path, args.issue, args.symbol, args.max_tokens, args.as_json)
    if args.command == "investigate":
        return _investigate(root, db_path, args.issue, args.ranker, args.limit, args.as_json)
    if args.command == "compare":
        return _compare(root, db_path, args.issue, args.limit, args.as_json)
    if args.command in {"git-history", "blame", "cochanges"}:
        return _git_query(root, args.command, args.path, args.limit, args.start, args.end, args.as_json)
    if args.command == "symbols":
        return _symbols(db_path, args.name, args.limit, args.as_json)
    if args.command in {"references", "callers", "callees", "impact"}:
        return _relationship_query(db_path, args.command, args.symbol, args.as_json)
    return 2


def _init(root: Path, db_path: Path) -> int:
    files = discover_files(root)
    with connect(db_path) as connection:
        file_count = sync_files(connection, files)
        symbol_count = index_symbols(connection, files)
        edge_count = index_relationships(connection, files)
        search_count = rebuild_search_index(connection, files)
        git_count = refresh_git_index(connection, root)
        payload = _status_payload(root, db_path, connection)
    payload["discovered_files"] = file_count
    payload["indexed_symbols"] = symbol_count
    payload["indexed_edges"] = edge_count
    payload["search_documents"] = search_count
    payload["git_changes"] = git_count
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
        print(f"indexed_edges: {payload['indexed_edges']}")
        print(f"search_documents: {payload['search_documents']}")
        print(f"git_changes: {payload['git_changes']}")
    return 0


def _search(db_path: Path, query: str, limit: int, exact: bool, as_json: bool) -> int:
    with connect(db_path) as connection:
        results = search(connection, query, limit=limit, exact=exact)
    payload = [
        {
            "entity_type": result.entity_type,
            "entity_id": result.entity_id,
            "path": result.path,
            "language": result.language,
            "name": result.name,
            "kind": result.kind,
            "start_line": result.start_line,
            "end_line": result.end_line,
            "rank": result.rank,
            "score": result.score,
            "snippet": result.snippet,
            "matched_fields": list(result.matched_fields),
        }
        for result in results
    ]
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        for result in payload:
            fields = ",".join(result["matched_fields"]) or "content"
            print(
                f"{result['path']}:{result['start_line']}-{result['end_line']} "
                f"{result['kind']} {result['name']} "
                f"score={result['score']:.6g} fields={fields}\n  {result['snippet']}"
            )
    return 0


def _context_pack(root: Path, db_path: Path, issue_path: Path, symbol: str | None, max_tokens: int, as_json: bool) -> int:
    try:
        issue = issue_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"could not read issue file: {issue_path}: {error}") from error
    with connect(db_path) as connection:
        pack = build_context_pack(connection, root, issue, symbol=symbol, max_tokens=max_tokens)
    print(json.dumps(pack.to_dict(), indent=2) if as_json else pack.render_markdown())
    return 0


def _investigate(root: Path, db_path: Path, issue_path: Path, ranker_name: str, limit: int, as_json: bool) -> int:
    issue = _read_issue(issue_path)
    with connect(db_path) as connection:
        candidates = candidates_from_search(_issue_search(connection, issue, limit))
    ranker = HeuristicRanker() if ranker_name == "heuristic" else GatewayJevRanker()
    result = ranker.rank_candidates(issue, candidates)
    payload = _ranking_payload(issue, candidates, result)
    _print_payload(payload, as_json)
    return 0


def _compare(root: Path, db_path: Path, issue_path: Path, limit: int, as_json: bool) -> int:
    issue = _read_issue(issue_path)
    with connect(db_path) as connection:
        candidates = candidates_from_search(_issue_search(connection, issue, limit))
    heuristic = HeuristicRanker().rank_candidates(issue, candidates)
    try:
        jev = GatewayJevRanker().rank_candidates(issue, candidates)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    payload = {
        "issue": issue,
        "candidate_count": len(candidates),
        "heuristic": _ranking_payload(issue, candidates, heuristic),
        "jev": _ranking_payload(issue, candidates, jev),
        "comparison": {
            "latency_ratio_jev_over_heuristic": jev.latency_ms / max(heuristic.latency_ms, 0.001),
            "jev_input_tokens": jev.input_tokens,
            "quality_note": "Ranking quality requires labeled issue cases; this command compares runtime and provider usage only.",
        },
    }
    _print_payload(payload, as_json)
    return 0


def _read_issue(issue_path: Path) -> str:
    try:
        return issue_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"could not read issue file: {issue_path}: {error}") from error


def _issue_search(connection: sqlite3.Connection, issue: str, limit: int):
    """Search the full issue first, then merge term-level candidates if needed."""
    results = search(connection, issue, limit=limit)
    if results:
        return results
    terms = [term for term in re.findall(r"[\w$]+", issue) if len(term) >= 4]
    by_key = {}
    for term in terms[:12]:
        for result in search(connection, term, limit=limit):
            by_key[(result.entity_type, result.entity_id)] = result
    return sorted(by_key.values(), key=lambda result: (-result.score, result.path, result.start_line))[:limit]


def _ranking_payload(issue: str, candidates, result) -> dict[str, object]:
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    return {
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "request_id": result.request_id,
        "estimated_cost_usd": result.estimated_cost_usd,
        "selected_candidate": result.selected_candidate,
        "any_candidate_probability": result.any_candidate_probability,
        "ranked": [
            {
                "candidate_id": candidate_id,
                "probability": probability,
                "path": candidate_by_id[candidate_id].path if candidate_id in candidate_by_id else None,
                "name": candidate_by_id[candidate_id].name if candidate_id in candidate_by_id else None,
            }
            for candidate_id, probability in result.ranked
        ],
    }


def _print_payload(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    print(f"provider: {payload.get('provider', 'comparison')}")
    if "candidate_count" in payload:
        print(f"candidate_count: {payload['candidate_count']}")
    for name in ("heuristic", "jev"):
        item = payload.get(name)
        if isinstance(item, dict):
            print(f"{name}: latency={item['latency_ms']:.2f}ms selected={item['selected_candidate']}")
    if "ranked" in payload:
        print(f"latency: {payload['latency_ms']:.2f}ms")
        print(f"selected: {payload['selected_candidate']}")
        for item in payload["ranked"][:10]:
            print(f"  {item['probability']:.4f} {item['candidate_id']} {item['path']}:{item['name']}")


def _git_query(root: Path, command: str, path: str, limit: int, start: int, end: int | None, as_json: bool) -> int:
    git = GitEvidence(root)
    if command == "git-history":
        result = [item.__dict__ for item in git.history_for(path, limit=limit)]
    elif command == "blame":
        result = [item.__dict__ for item in git.blame(path, start, end)]
    else:
        result = [{"path": item_path, "count": count} for item_path, count in git.cochanged(path, limit=limit)]
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        for item in result:
            print(json.dumps(item) if isinstance(item, dict) else item)
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


def _relationship_query(db_path: Path, relationship: str, name: str, as_json: bool) -> int:
    with connect(db_path) as connection:
        symbol_ids = [
            row[0]
            for row in connection.execute(
                """
                SELECT id FROM symbols
                WHERE simple_name = ? OR qualified_name = ?
                """,
                (name, name),
            )
        ]
        if not symbol_ids:
            raise SystemExit(f"symbol not found: {name}")

        placeholders = ", ".join("?" for _ in symbol_ids)
        if relationship in {"references", "callers"}:
            predicate = f"edges.target_symbol_id IN ({placeholders})"
        elif relationship == "callees":
            predicate = f"edges.source_symbol_id IN ({placeholders})"
        else:
            predicate = f"edges.source_symbol_id IN ({placeholders})"

        query = f"""
            SELECT edges.edge_type,
                   source_file.path AS source_path,
                   source_symbol.qualified_name AS source_symbol,
                   target_file.path AS target_path,
                   target_symbol.qualified_name AS target_symbol,
                   edges.confidence
            FROM edges
            JOIN symbols AS source_symbol ON source_symbol.id = edges.source_symbol_id
            JOIN files AS source_file ON source_file.id = source_symbol.file_id
            JOIN symbols AS target_symbol ON target_symbol.id = edges.target_symbol_id
            JOIN files AS target_file ON target_file.id = target_symbol.file_id
            WHERE {predicate}
            ORDER BY source_path, source_symbol, target_path, target_symbol
        """
        rows = [dict(row) for row in connection.execute(query, symbol_ids)]

    if relationship == "impact":
        rows = [row for row in rows if row["edge_type"] in {"CALLS", "IMPORTS"}]

    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(
                f"{row['edge_type']} {row['source_path']}:{row['source_symbol']} -> "
                f"{row['target_path']}:{row['target_symbol']}"
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
        "indexed_edges": connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "indexed_tests": connection.execute("SELECT COUNT(*) FROM tests").fetchone()[0],
        "git_changes": connection.execute("SELECT COUNT(*) FROM git_changes").fetchone()[0],
        "search_documents": connection.execute("SELECT COUNT(*) FROM search_index").fetchone()[0],
    }


if __name__ == "__main__":
    raise SystemExit(main())
