"""Repository discovery and file metadata."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path


MAX_FILE_SIZE = 1_000_000

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}

FALLBACK_IGNORED_DIRECTORIES = {
    ".git",
    ".codebase",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}


@dataclass(frozen=True)
class DiscoveredFile:
    """A file that can be added to the repository index."""

    path: Path
    relative_path: str
    language: str
    size_bytes: int
    content_hash: str
    modified_at: float


def discover_files(root: Path) -> list[DiscoveredFile]:
    """Discover non-ignored text files using Git when available."""

    root = root.resolve()
    paths = _git_paths(root) if (root / ".git").exists() else None
    if paths is None:
        paths = _walk_paths(root)

    discovered = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_FILE_SIZE or _is_binary(path):
            continue

        relative_path = path.relative_to(root).as_posix()
        discovered.append(
            DiscoveredFile(
                path=path,
                relative_path=relative_path,
                language=LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unknown"),
                size_bytes=stat.st_size,
                content_hash=_sha256(path),
                modified_at=stat.st_mtime,
            )
        )

    return sorted(discovered, key=lambda item: item.relative_path)


def sync_files(connection, files: list[DiscoveredFile]) -> int:
    """Upsert discovered file metadata and return the number of indexed files."""

    connection.executemany(
        """
        INSERT INTO files(path, language, size_bytes, content_hash, modified_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            language = excluded.language,
            size_bytes = excluded.size_bytes,
            content_hash = excluded.content_hash,
            modified_at = excluded.modified_at
        """,
        [
            (
                file.relative_path,
                file.language,
                file.size_bytes,
                file.content_hash,
                file.modified_at,
            )
            for file in files
        ],
    )
    connection.commit()
    return len(files)


def _git_paths(root: Path) -> list[Path] | None:
    """Return tracked and non-ignored untracked paths, or None if Git is unavailable."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    return [root / os.fsdecode(raw_path) for raw_path in result.stdout.split(b"\0") if raw_path]


def _walk_paths(root: Path) -> list[Path]:
    ignore_patterns = _fallback_ignore_patterns(root)
    paths = []
    for current_root, directories, filenames in os.walk(root):
        relative_root = Path(current_root).relative_to(root)
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in FALLBACK_IGNORED_DIRECTORIES
            and not _matches_ignore(
                (relative_root / directory).as_posix(), directory, ignore_patterns
            )
        )
        for filename in sorted(filenames):
            relative_path = (relative_root / filename).as_posix()
            if not _matches_ignore(relative_path, filename, ignore_patterns):
                paths.append(Path(current_root) / filename)
    return paths


def _fallback_ignore_patterns(root: Path) -> list[str]:
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return []

    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    return [
        line.strip().lstrip("/")
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and not line.startswith("!")
    ]


def _matches_ignore(relative_path: str, basename: str, patterns: list[str]) -> bool:
    return any(
        fnmatch(relative_path, pattern) or fnmatch(basename, pattern)
        for pattern in patterns
    )


def _is_binary(path: Path) -> bool:
    try:
        return b"\0" in path.read_bytes()[:8192]
    except OSError:
        return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
