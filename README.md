# Jev Codebase Explorer

Local codebase intelligence for coding agents.

The current indexer is deterministic and Jev-free. It discovers Git-aware repository
files, extracts Tree-sitter symbols, and persists them in SQLite. Later milestones add
retrieval and optional Jev ranking through Vercel AI Gateway.

## Foundation

```text
python -m jev_codebase_explore.cli init .
python -m jev_codebase_explore.cli status . --json
python -m jev_codebase_explore.cli symbols . --name validate
```

The installed CLI is also available as:

```text
codescout init .
codescout status .
codescout symbols . --name validate
```

The local index is written to `.codebase/index.db`.
