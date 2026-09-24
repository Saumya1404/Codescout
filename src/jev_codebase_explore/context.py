"""Deterministic, budgeted context-pack construction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .git_evidence import GitEvidence


@dataclass(frozen=True)
class ContextSection:
    title: str
    content: str
    priority: int
    estimated_tokens: int


@dataclass
class ContextPack:
    issue: str
    primary: dict[str, object]
    sections: list[ContextSection] = field(default_factory=list)
    token_limit: int = 5000
    estimated_tokens: int = 0
    omitted_sections: list[str] = field(default_factory=list)
    branch: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "primary": self.primary,
            "branch": self.branch,
            "sections": [{"title": item.title, "content": item.content, "priority": item.priority, "estimated_tokens": item.estimated_tokens} for item in self.sections],
            "token_limit": self.token_limit,
            "estimated_tokens": self.estimated_tokens,
            "omitted_sections": self.omitted_sections,
        }

    def render_markdown(self) -> str:
        lines = ["# Investigation Context", "", "## Issue", "", self.issue.strip(), "", "## Primary Candidate", "", f"`{self.primary['path']}:{self.primary['start_line']}-{self.primary['end_line']}`", f"`{self.primary['qualified_name']}` ({self.primary['kind']})", ""]
        if self.branch:
            lines.extend([f"Git branch: `{self.branch}`", ""])
        for section in self.sections:
            lines.extend([f"## {section.title}", "", section.content.rstrip(), ""])
        if self.omitted_sections:
            lines.extend(["## Omitted", "", *[f"- {title}" for title in self.omitted_sections], ""])
        lines.append(f"Context estimate: {self.estimated_tokens} tokens / {self.token_limit} token budget.")
        return "\n".join(lines)


def build_context_pack(connection, root: Path, issue: str, *, symbol: str | None = None, max_tokens: int = 5000) -> ContextPack:
    """Build a source-preserving investigation packet without an LLM."""
    if max_tokens < 100:
        raise ValueError("max_tokens must be at least 100")
    primary = _find_primary(connection, issue, symbol)
    if primary is None:
        raise ValueError("no symbol matched the issue or --symbol")
    pack = ContextPack(issue=issue, primary=primary, token_limit=max_tokens, branch=GitEvidence(root).branch())
    sections = _sections(connection, root, primary, GitEvidence(root))
    selected = _estimate_tokens(issue) + _estimate_tokens(json.dumps(primary))
    for section in sorted(sections, key=lambda item: item.priority):
        if section.priority == 0 or selected + section.estimated_tokens <= max_tokens:
            pack.sections.append(section)
            selected += section.estimated_tokens
        else:
            pack.omitted_sections.append(section.title)
    pack.estimated_tokens = selected
    return pack


def _find_primary(connection, issue: str, symbol: str | None) -> dict[str, object] | None:
    if symbol:
        row = connection.execute("SELECT symbols.id, symbols.qualified_name, symbols.kind, files.path, symbols.start_line, symbols.end_line, symbols.start_byte, symbols.end_byte FROM symbols JOIN files ON files.id = symbols.file_id WHERE symbols.simple_name = ? OR symbols.qualified_name = ? ORDER BY symbols.kind = 'module', files.path LIMIT 1", (symbol, symbol)).fetchone()
    else:
        terms = [term for term in issue.split() if len(term) > 2]
        if not terms:
            return None
        query = " AND ".join(f"{term}*" for term in terms[:8])
        row = connection.execute("SELECT symbols.id, symbols.qualified_name, symbols.kind, files.path, symbols.start_line, symbols.end_line, symbols.start_byte, symbols.end_byte FROM search_index JOIN symbols ON search_index.entity_type = 'symbol' AND search_index.entity_id = symbols.id JOIN files ON files.id = symbols.file_id WHERE search_index MATCH ? ORDER BY bm25(search_index) LIMIT 1", (query,)).fetchone()
    return dict(row) if row else None


def _sections(connection, root: Path, primary: dict[str, object], git: GitEvidence) -> list[ContextSection]:
    source = _read_span(root, primary)
    sections = [ContextSection("Primary Source", _fenced(primary["path"], source), 0, _estimate_tokens(source))]
    neighbors = connection.execute("SELECT edges.edge_type, source_symbol.qualified_name AS source_name, source_file.path AS source_path, target_symbol.qualified_name AS target_name, target_file.path AS target_path FROM edges JOIN symbols AS source_symbol ON source_symbol.id = edges.source_symbol_id JOIN files AS source_file ON source_file.id = source_symbol.file_id JOIN symbols AS target_symbol ON target_symbol.id = edges.target_symbol_id JOIN files AS target_file ON target_file.id = target_symbol.file_id WHERE edges.source_symbol_id = ? OR edges.target_symbol_id = ? ORDER BY edges.edge_type, source_path, target_path", (primary["id"], primary["id"])).fetchall()
    if neighbors:
        text = "\n".join(f"- {row['edge_type']}: {row['source_path']}:{row['source_name']} -> {row['target_path']}:{row['target_name']}" for row in neighbors)
        sections.append(ContextSection("Relationships", text, 2, _estimate_tokens(text)))
    test_ids = [row[0] for row in connection.execute("SELECT source_symbol_id FROM edges WHERE target_symbol_id = ? AND edge_type = 'TESTS'", (primary["id"],))]
    tests = []
    for test_id in test_ids:
        row = connection.execute("SELECT symbols.qualified_name, symbols.kind, files.path, symbols.start_line, symbols.end_line, symbols.start_byte, symbols.end_byte FROM symbols JOIN files ON files.id = symbols.file_id WHERE symbols.id = ?", (test_id,)).fetchone()
        if row:
            item = dict(row)
            tests.append(_fenced(item["path"], _read_span(root, item)))
    if tests:
        text = "\n\n".join(tests)
        sections.append(ContextSection("Related Tests", text, 1, _estimate_tokens(text)))
    changes = connection.execute("SELECT commit_hash, change_type, message FROM git_changes JOIN files ON files.id = git_changes.file_id WHERE files.path = ? ORDER BY timestamp DESC LIMIT 8", (primary["path"],)).fetchall()
    lines = [f"- `{row['commit_hash'][:10]}` [{row['change_type']}] {row['message']}" for row in changes]
    for commit in git.history_for(str(primary["path"]), limit=5):
        if not any(commit.commit_hash.startswith(row["commit_hash"][:10]) for row in changes):
            lines.append(f"- `{commit.commit_hash[:10]}` {commit.message}")
    blamed = git.blame(str(primary["path"]), int(primary["start_line"]), int(primary["end_line"]))
    if blamed:
        lines.append("\nBlame:")
        lines.extend(f"- line {item.line}: `{item.commit_hash[:10]}` {item.author} - {item.summary}" for item in blamed)
    cochanged = git.cochanged(str(primary["path"]), limit=5)
    if cochanged:
        lines.append("\nFrequently co-changed files:")
        lines.extend(f"- `{path}` ({count} commits)" for path, count in cochanged)
    diff = git.diff(str(primary["path"]))
    if diff:
        lines.append(_fenced(primary["path"], diff, "diff"))
    if lines:
        text = "\n".join(lines)
        sections.append(ContextSection("Git Evidence", text, 3, _estimate_tokens(text)))
    return sections


def _read_span(root: Path, record: dict[str, object]) -> str:
    try:
        source = (root / str(record["path"])).read_bytes()
        return source[int(record["start_byte"]):int(record["end_byte"])].decode("utf-8", errors="replace")
    except OSError:
        return "[source unavailable]"


def _fenced(path: object, content: str, language: str | None = None) -> str:
    suffix = language or Path(str(path)).suffix.lstrip(".") or "text"
    return f"`{path}`\n\n```{suffix}\n{content.rstrip()}\n```"


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
