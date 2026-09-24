"""Git history and working-tree evidence for indexed repositories."""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitCommit:
    commit_hash: str
    timestamp: float | None
    message: str


@dataclass(frozen=True)
class GitChange:
    commit_hash: str
    path: str
    change_type: str
    timestamp: float | None
    message: str


@dataclass(frozen=True)
class BlameLine:
    path: str
    line: int
    commit_hash: str
    author: str
    timestamp: float | None
    summary: str


class GitEvidence:
    """Small, read-only wrapper around Git commands."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def available(self) -> bool:
        return self._is_repo() and self._run(["rev-parse", "--git-dir"]).returncode == 0

    def branch(self) -> str:
        if not self._is_repo():
            return ""
        result = self._run(["branch", "--show-current"])
        return result.stdout.strip() if result.returncode == 0 else ""

    def commits(self, path: str | None = None, limit: int = 50) -> list[GitCommit]:
        if not self._is_repo():
            return []
        args = ["log", f"--max-count={limit}", "--date=unix", "--format=%H%x09%ct%x09%s"]
        if path:
            args.extend(["--", path])
        result = self._run(args)
        if result.returncode != 0:
            return []
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or len(parts[0]) < 7:
                continue
            commits.append(GitCommit(parts[0], _timestamp(parts[1]), parts[2]))
        return commits

    def changes(self, limit: int = 100) -> list[GitChange]:
        changes: list[GitChange] = []
        for commit in self.commits(limit=limit):
            result = self._run(["show", "--format=", "--name-status", "--find-renames", commit.commit_hash])
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 2 or not parts[0]:
                    continue
                path = parts[-1].replace("\\", "/")
                if path and not path.startswith("("):
                    changes.append(GitChange(commit.commit_hash, path, parts[0][0], commit.timestamp, commit.message))
        return changes

    def history_for(self, path: str, limit: int = 10) -> list[GitCommit]:
        return self.commits(path=path, limit=limit)

    def diff(self, path: str | None = None, staged: bool = False) -> str:
        if not self._is_repo():
            return ""
        args = ["diff"]
        if staged:
            args.append("--cached")
        args.append("--no-ext-diff")
        args.append("--unified=3")
        if path:
            args.extend(["--", path])
        result = self._run(args)
        return result.stdout if result.returncode == 0 else ""

    def cochanged(self, path: str, limit: int = 10) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter()
        for commit in self.commits(path=path, limit=100):
            result = self._run(["show", "--format=", "--name-only", commit.commit_hash])
            if result.returncode != 0:
                continue
            for changed_path in result.stdout.splitlines():
                changed_path = changed_path.replace("\\", "/").strip()
                if changed_path and changed_path != path:
                    counts[changed_path] += 1
        return counts.most_common(limit)

    def blame(self, path: str, start_line: int = 1, end_line: int | None = None) -> list[BlameLine]:
        if not self._is_repo():
            return []
        end_line = end_line or start_line
        result = self._run(["blame", "--line-porcelain", f"-L{start_line},{end_line}", "--", path])
        if result.returncode != 0:
            return []
        lines: list[BlameLine] = []
        current: dict[str, str] = {}
        source_line = start_line
        for raw_line in result.stdout.splitlines():
            if raw_line.startswith("\t"):
                lines.append(BlameLine(path, source_line, current.get("commit", ""), current.get("author", ""), _timestamp(current.get("author-time", "")), current.get("summary", "")))
                source_line += 1
                current = {}
                continue
            if not current and raw_line:
                current["commit"] = raw_line.split(" ", 1)[0]
                continue
            key, _, value = raw_line.partition(" ")
            if key in {"author", "author-time", "summary"}:
                current[key] = value
        return lines

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["git", *args], cwd=self.root, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        except OSError:
            return subprocess.CompletedProcess(args, 1, "", "git unavailable")

    def _is_repo(self) -> bool:
        return (self.root / ".git").exists()


def refresh_git_index(connection, root: Path, limit: int = 100) -> int:
    """Refresh persisted commit and file-change evidence."""
    git = GitEvidence(root)
    connection.execute("DELETE FROM git_changes")
    connection.execute("DELETE FROM git_commits")
    if not git.available():
        connection.commit()
        return 0
    commits = git.commits(limit=limit)
    connection.executemany("INSERT INTO git_commits(commit_hash, timestamp, message) VALUES (?, ?, ?)", [(item.commit_hash, item.timestamp, item.message) for item in commits])
    file_ids = {row["path"]: row["id"] for row in connection.execute("SELECT id, path FROM files")}
    changes = git.changes(limit=limit)
    connection.executemany(
        "INSERT INTO git_changes(commit_hash, file_id, change_type, timestamp, message) VALUES (?, ?, ?, ?, ?)",
        [(item.commit_hash, file_ids.get(item.path), item.change_type, item.timestamp, item.message) for item in changes],
    )
    connection.commit()
    return len(changes)


def _timestamp(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None
