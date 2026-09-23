# Jev Codebase Explorer

Local codebase intelligence for coding agents.

The first milestone creates a Git-aware repository index in SQLite. The indexer is
deterministic and Jev-free; later milestones add Tree-sitter symbols, retrieval, and
optional Jev ranking through Vercel AI Gateway.

## Foundation

```text
python -m jev_codebase_explore.cli init .
python -m jev_codebase_explore.cli status . --json
```

The installed CLI is also available as:

```text
codescout init .
codescout status .
```

The local index is written to `.codebase/index.db`.
