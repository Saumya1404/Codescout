"""Tree-sitter symbol extraction and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from tree_sitter import Language, Parser
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

from .repository import DiscoveredFile


@dataclass(frozen=True)
class SymbolRecord:
    """A source definition extracted from a parsed file."""

    file_path: str
    qualified_name: str
    simple_name: str
    kind: str
    signature: str
    docstring: str | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


SYMBOL_KINDS = {
    "python": {
        "class_definition": "class",
        "function_definition": "function",
        "async_function_definition": "function",
    },
    "javascript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
    },
    "typescript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "abstract_class_declaration": "class",
    },
    "tsx": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "abstract_class_declaration": "class",
    },
}


def parse_file(file: DiscoveredFile) -> list[SymbolRecord]:
    """Parse a discovered source file and return its definitions."""

    if file.language not in SYMBOL_KINDS:
        return []
    source = file.path.read_bytes()
    return parse_source(file.relative_path, file.language, source)


def parse_source(file_path: str, language: str, source: bytes) -> list[SymbolRecord]:
    """Parse source bytes for a supported language."""

    parser = _parser_for(language)
    tree = parser.parse(source)
    records: list[SymbolRecord] = []
    _visit(tree.root_node, file_path, language, source, [], [], records)
    return records


def index_symbols(connection, files: list[DiscoveredFile]) -> int:
    """Replace symbols for discovered files and return the symbol count."""

    total = 0
    for file in files:
        file_id_row = connection.execute(
            "SELECT id FROM files WHERE path = ?", (file.relative_path,)
        ).fetchone()
        if file_id_row is None:
            continue

        file_id = file_id_row[0]
        connection.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        records = parse_file(file)
        connection.executemany(
            """
            INSERT INTO symbols(
                file_id, qualified_name, simple_name, kind, signature, docstring,
                start_line, end_line, start_byte, end_byte
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    file_id,
                    record.qualified_name,
                    record.simple_name,
                    record.kind,
                    record.signature,
                    record.docstring,
                    record.start_line,
                    record.end_line,
                    record.start_byte,
                    record.end_byte,
                )
                for record in records
            ],
        )
        total += len(records)

    connection.commit()
    return total


@lru_cache(maxsize=4)
def _parser_for(language: str) -> Parser:
    if language == "python":
        grammar = Language(tree_sitter_python.language())
    elif language == "javascript":
        grammar = Language(tree_sitter_javascript.language())
    elif language == "typescript":
        grammar = Language(tree_sitter_typescript.language_typescript())
    elif language == "tsx":
        grammar = Language(tree_sitter_typescript.language_tsx())
    else:
        raise ValueError(f"unsupported parser language: {language}")
    return Parser(grammar)


def _visit(node, file_path, language, source, scopes, scope_kinds, records) -> None:
    kind = SYMBOL_KINDS.get(language, {}).get(node.type)
    name = _node_name(node, source) if kind else None
    next_scopes = scopes
    next_scope_kinds = scope_kinds

    if kind and name:
        body = node.child_by_field_name("body")
        signature_end = body.start_byte if body is not None else node.end_byte
        signature = _decode(source[node.start_byte:signature_end]).strip()
        records.append(
            SymbolRecord(
                file_path=file_path,
                qualified_name=".".join([*scopes, name]),
                simple_name=name,
                kind=_effective_kind(kind, scope_kinds),
                signature=signature,
                docstring=_docstring(body, source),
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
            )
        )
        next_scopes = [*scopes, name]
        next_scope_kinds = [*scope_kinds, "class" if kind in {"class", "interface"} else "function"]

    for child in node.named_children:
        _visit(child, file_path, language, source, next_scopes, next_scope_kinds, records)


def _node_name(node, source) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return _decode(source[name_node.start_byte:name_node.end_byte]).strip()


def _effective_kind(kind: str, scope_kinds: list[str]) -> str:
    if kind == "function" and scope_kinds and scope_kinds[-1] == "class":
        return "method"
    return kind


def _docstring(body, source) -> str | None:
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type != "expression_statement" or not first.named_children:
        return None
    expression = first.named_children[0]
    if expression.type not in {"string", "string_fragment"}:
        return None
    value = _decode(source[expression.start_byte:expression.end_byte]).strip()
    return value[1:-1] if len(value) >= 2 else value


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")
