"""SQLite FTS5 retrieval over repository files and symbols."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .repository import DiscoveredFile


@dataclass(frozen=True)
class SearchResult:
    """A ranked file or symbol search result."""

    entity_type: str
    entity_id: int
    path: str
    language: str
    name: str
    kind: str
    start_line: int
    end_line: int
    rank: float
    score: float
    snippet: str
    matched_fields: tuple[str, ...]


def rebuild_search_index(connection, files: list[DiscoveredFile]) -> int:
    """Rebuild the FTS document layer from current files and symbols."""

    connection.execute("DELETE FROM search_index")
    file_by_path = {file.relative_path: file for file in files}
    file_rows = connection.execute(
        """
        SELECT id, path, language
        FROM files
        ORDER BY path
        """
    ).fetchall()

    file_documents = []
    for row in file_rows:
        file = file_by_path.get(row["path"])
        if file is None:
            continue
        try:
            content = file.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        file_documents.append(
            (
                "file",
                row["id"],
                row["path"],
                row["language"],
                Path(row["path"]).name,
                "file",
                "",
                "",
                content,
            )
        )

    symbol_rows = connection.execute(
        """
        SELECT symbols.id, files.path, files.language, symbols.simple_name,
               symbols.kind, symbols.signature, symbols.docstring,
               symbols.start_line, symbols.end_line,
               symbols.start_byte, symbols.end_byte
        FROM symbols
        JOIN files ON files.id = symbols.file_id
        ORDER BY files.path, symbols.start_line
        """
    ).fetchall()
    symbol_documents = []
    for row in symbol_rows:
        file = file_by_path.get(row["path"])
        if file is None:
            continue
        try:
            source = file.path.read_bytes()
            content = source[row["start_byte"] : row["end_byte"]].decode(
                "utf-8", errors="replace"
            )
        except OSError:
            content = ""
        symbol_documents.append(
            (
                "symbol",
                row["id"],
                row["path"],
                row["language"],
                row["simple_name"],
                row["kind"],
                row["signature"] or "",
                row["docstring"] or "",
                content,
            )
        )

    connection.executemany(
        """
        INSERT INTO search_index(
            entity_type, entity_id, path, language, name, kind,
            signature, docstring, content
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [*file_documents, *symbol_documents],
    )
    connection.commit()
    return len(file_documents) + len(symbol_documents)


def search(
    connection,
    query: str,
    *,
    limit: int = 20,
    exact: bool = False,
) -> list[SearchResult]:
    """Search indexed files and symbols using FTS5 BM25 ranking."""

    if not query.strip():
        return []
    if limit < 1:
        raise ValueError("limit must be positive")

    match_query = _match_query(query, exact=exact)
    rows = connection.execute(
        """
        SELECT rowid, entity_type, entity_id, path, language, name, kind,
               bm25(search_index) AS rank,
               CASE WHEN entity_type = 'file'
                    THEN length(content) - length(replace(content, char(10), '')) + 1
                    ELSE 1 END AS file_line_count,
               snippet(search_index, 8, '[', ']', '...', 12) AS content_snippet,
               highlight(search_index, 2, '[', ']') AS path_match,
               highlight(search_index, 4, '[', ']') AS name_match,
               highlight(search_index, 6, '[', ']') AS signature_match,
               highlight(search_index, 7, '[', ']') AS docstring_match,
               highlight(search_index, 8, '[', ']') AS content_match
        FROM search_index
        WHERE search_index MATCH ?
        ORDER BY rank, path, entity_type, entity_id
        LIMIT ?
        """,
        (match_query, limit),
    ).fetchall()

    symbol_spans = _symbol_spans(connection, [row["entity_id"] for row in rows if row["entity_type"] == "symbol"])
    results = []
    for row in rows:
        if row["entity_type"] == "symbol":
            start_line, end_line = symbol_spans[row["entity_id"]]
        else:
            start_line, end_line = 1, max(1, int(row["file_line_count"]))
        matched_fields = tuple(
            field
            for field, value in (
                ("path", row["path_match"]),
                ("name", row["name_match"]),
                ("signature", row["signature_match"]),
                ("docstring", row["docstring_match"]),
                ("content", row["content_match"]),
            )
            if "[" in value
        )
        snippet = row["content_snippet"] or row["name"] or row["path"]
        rank = float(row["rank"])
        results.append(
            SearchResult(
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                path=row["path"],
                language=row["language"],
                name=row["name"],
                kind=row["kind"],
                start_line=start_line,
                end_line=end_line,
                rank=rank,
                score=abs(rank),
                snippet=snippet,
                matched_fields=matched_fields,
            )
        )
    return results


def _match_query(query: str, *, exact: bool) -> str:
    terms = re.findall(r"[\w$]+", query, flags=re.UNICODE)
    if not terms:
        return '""'
    if exact:
        return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    return " AND ".join(f'{term}*' for term in terms)


def _symbol_spans(connection, symbol_ids: list[int]) -> dict[int, tuple[int, int]]:
    if not symbol_ids:
        return {}
    placeholders = ", ".join("?" for _ in symbol_ids)
    return {
        row["id"]: (row["start_line"], row["end_line"])
        for row in connection.execute(
            f"SELECT id, start_line, end_line FROM symbols WHERE id IN ({placeholders})",
            symbol_ids,
        )
    }
