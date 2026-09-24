"""Extract and resolve imports, calls, references, and test relationships."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .repository import DiscoveredFile
from .symbols import SYMBOL_KINDS, _parser_for, module_name_for_path, parse_file


@dataclass(frozen=True)
class RawRelationship:
    source_file: str
    source_symbol: str
    target_name: str
    edge_type: str
    target_module: str | None = None


def index_relationships(connection, files: list[DiscoveredFile]) -> int:
    """Rebuild relationship and test rows for the current symbol index."""

    connection.execute("DELETE FROM edges")
    connection.execute("DELETE FROM tests")

    symbol_rows = connection.execute(
        """
        SELECT symbols.id, symbols.file_id, symbols.qualified_name,
               symbols.simple_name, symbols.kind, files.path,
               symbols.start_byte, symbols.end_byte
        FROM symbols
        JOIN files ON files.id = symbols.file_id
        """
    ).fetchall()
    symbols = [dict(row) for row in symbol_rows]
    relationships = []
    for file in files:
        if file.language not in SYMBOL_KINDS:
            continue
        relationships.extend(_extract_file_relationships(file, symbols))

    edges = []
    tests = []
    for relationship in relationships:
        source_id = _resolve_source(symbols, relationship)
        target_id = _resolve_target(symbols, relationship)
        if source_id is None or target_id is None or source_id == target_id:
            continue
        edges.append((source_id, target_id, relationship.edge_type, "tree-sitter", 0.7))
        if relationship.edge_type == "CALLS" and _is_test_symbol(relationship.source_symbol, relationship.source_file):
            edges.append((source_id, target_id, "TESTS", "tree-sitter", 0.8))

    connection.executemany(
        """
        INSERT OR REPLACE INTO edges(
            source_symbol_id, target_symbol_id, edge_type,
            resolution_method, confidence
        ) VALUES (?, ?, ?, ?, ?)
        """,
        edges,
    )

    for symbol in symbols:
        if symbol["kind"] == "function" and _is_test_symbol(symbol["simple_name"], symbol["path"]):
            connection.execute(
                "INSERT INTO tests(file_id, symbol_id, test_name, framework) VALUES (?, ?, ?, ?)",
                (symbol["file_id"], symbol["id"], symbol["simple_name"], "pytest"),
            )

    connection.commit()
    return connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]


def _extract_file_relationships(file: DiscoveredFile, symbols: list[dict]) -> list[RawRelationship]:
    source = file.path.read_bytes()
    tree = _parser_for(file.language).parse(source)
    definitions = [
        symbol
        for symbol in symbols
        if symbol["path"] == file.relative_path and symbol["kind"] != "module"
    ]
    module_name = module_name_for_path(file.relative_path)
    relationships: list[RawRelationship] = []
    bindings: dict[str, tuple[str, str | None]] = {}
    _collect_imports(
        tree.root_node,
        file.relative_path,
        source,
        module_name,
        relationships,
        bindings,
    )
    _collect_calls_and_references(
        tree.root_node,
        file.relative_path,
        source,
        module_name,
        definitions,
        bindings,
        relationships,
    )
    return relationships


def _collect_imports(node, file_path, source, module_name, relationships, bindings) -> None:
    if node.type in {"import_statement", "import_from_statement"}:
        text = _text(node, source).strip()
        parsed = _parse_import(text, module_name)
        for imported_module, imported_name, local_name in parsed:
            if imported_name is None:
                bindings[local_name] = (imported_module, None)
                relationships.append(
                    RawRelationship(file_path, module_name, imported_module, "IMPORTS", imported_module)
                )
            else:
                bindings[local_name] = (imported_module, imported_name)
                relationships.append(
                    RawRelationship(file_path, module_name, imported_module, "IMPORTS", imported_module)
                )

    for child in node.named_children:
        _collect_imports(child, file_path, source, module_name, relationships, bindings)


def _collect_calls_and_references(
    node,
    file_path,
    source,
    module_name,
    definitions,
    bindings,
    relationships,
    current_symbol: str | None = None,
) -> None:
    current_symbol = _containing_symbol(node.start_byte, node.end_byte, definitions) or current_symbol or module_name

    if node.type in {"import_statement", "import_from_statement"}:
        return

    if node.type in {"call", "call_expression", "new_expression"}:
        function_node = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        if function_node is not None:
            target_name = _last_identifier(_text(function_node, source))
            if target_name:
                module, imported_name = bindings.get(target_name, (None, None))
                relationships.append(
                    RawRelationship(
                        file_path,
                        current_symbol,
                        imported_name or target_name,
                        "CALLS",
                        module,
                    )
                )

    if node.type == "identifier" and not _is_definition_identifier(node, definitions):
        if node.parent is None or node.parent.type not in {"call", "call_expression", "new_expression"}:
            target_name = _text(node, source)
            module, imported_name = bindings.get(target_name, (None, None))
            relationships.append(
                RawRelationship(
                    file_path,
                    current_symbol,
                    imported_name or target_name,
                    "REFERENCES",
                    module,
                )
            )

    for child in node.named_children:
        _collect_calls_and_references(
            child,
            file_path,
            source,
            module_name,
            definitions,
            bindings,
            relationships,
            current_symbol,
        )


def _parse_import(text: str, current_module: str):
    if text.startswith("from "):
        match = re.match(r"from\s+([.\w]+)\s+import\s+(.+)", text, re.DOTALL)
        if not match:
            return []
        module = _resolve_relative_module(match.group(1), current_module)
        imported = match.group(2).split("#", 1)[0]
        result = []
        for item in imported.split(","):
            parts = item.strip().split()
            if not parts:
                continue
            original = parts[0]
            local = parts[2] if len(parts) >= 3 and parts[1] == "as" else original
            result.append((module, original, local))
        return result

    if text.startswith("import ") and " from " not in text and not re.search(r"['\"]", text):
        body = text.removeprefix("import ").split(";", 1)[0]
        result = []
        for item in body.split(","):
            parts = item.strip().split()
            if not parts:
                continue
            module = parts[0]
            local = parts[2] if len(parts) >= 3 and parts[1] == "as" else module.split(".")[0]
            result.append((module, None, local))
        return result

    if text.startswith("export "):
        return []
    match = re.search(r"\bfrom\s*['\"]([^'\"]+)['\"]", text)
    if not match:
        return []
    module = _resolve_relative_module(match.group(1), current_module)
    clause = text[len("import "):text.index("from")].strip()
    if clause.startswith("{"):
        names = []
        for item in clause.strip("{} ").split(","):
            parts = item.strip().split()
            if not parts:
                continue
            original = parts[0]
            local = parts[2] if len(parts) >= 3 and parts[1] == "as" else original
            names.append((module, original, local))
        return names
    if clause.startswith("*"):
        parts = clause.split()
        return [(module, None, parts[-1])] if len(parts) >= 3 and parts[-2] == "as" else []
    default_name = _last_identifier(clause)
    return [(module, "default", default_name)] if default_name else []


def _resolve_relative_module(module: str, current_module: str) -> str:
    if not module.startswith("."):
        return module
    base = current_module.split(".")[:-1]
    dots = len(module) - len(module.lstrip("."))
    base = base[: max(0, len(base) - dots + 1)]
    suffix = module[dots:]
    return ".".join([*base, suffix] if suffix else base)


def _resolve_source(symbols: list[dict], relationship: RawRelationship) -> int | None:
    matches = [
        symbol["id"]
        for symbol in symbols
        if symbol["path"] == relationship.source_file
        and symbol["qualified_name"] == relationship.source_symbol
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_target(symbols: list[dict], relationship: RawRelationship) -> int | None:
    candidates = symbols
    if relationship.target_module:
        modules = [
            symbol
            for symbol in symbols
            if symbol["kind"] == "module"
            and (
                symbol["qualified_name"] == relationship.target_module
                or symbol["qualified_name"].endswith("." + relationship.target_module)
            )
        ]
        if not modules:
            return None
        target_file_ids = {module["file_id"] for module in modules}
        candidates = [symbol for symbol in symbols if symbol["file_id"] in target_file_ids]

    if relationship.edge_type == "IMPORTS":
        candidates = [symbol for symbol in candidates if symbol["kind"] == "module"]
        if relationship.target_module:
            candidates = [
                symbol
                for symbol in candidates
                if symbol["qualified_name"] == relationship.target_module
                or symbol["qualified_name"].endswith("." + relationship.target_module)
            ]
    else:
        candidates = [symbol for symbol in candidates if symbol["simple_name"] == relationship.target_name]

    if len(candidates) == 1:
        return candidates[0]["id"]
    return None


def _containing_symbol(start_byte: int, end_byte: int, symbols: list[dict]) -> str | None:
    candidates = [
        symbol
        for symbol in symbols
        if symbol["start_byte"] <= start_byte and end_byte <= symbol["end_byte"]
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda symbol: symbol["end_byte"] - symbol["start_byte"])["qualified_name"]


def _is_definition_identifier(node, symbols: list[dict]) -> bool:
    return any(symbol["start_byte"] <= node.start_byte < symbol["end_byte"] and node.start_byte == symbol["start_byte"] for symbol in symbols)


def _is_test_symbol(name: str, path: str) -> bool:
    return name.startswith("test_") or "/tests/" in "/" + path or path.startswith("tests/")


def _last_identifier(text: str) -> str | None:
    identifiers = re.findall(r"[A-Za-z_$][\w$]*", text)
    return identifiers[-1] if identifiers else None


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
