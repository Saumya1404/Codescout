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
codescout search "validate refund" .
codescout search "validate_ref" . --exact --json
codescout context-pack . --issue issue.txt --symbol validate_refund --max-tokens 5000
codescout investigate . --issue issue.txt --ranker heuristic
codescout investigate . --issue issue.txt --ranker jev --json
codescout compare . --issue issue.txt --json
codescout git-history src/shop/refunds.py .
codescout blame src/shop/refunds.py . --start 1 --end 20
codescout cochanges src/shop/refunds.py .
codescout callers validate_refund .
codescout callees test_partial_refund_is_valid .
codescout impact test_partial_refund_is_valid .
```

The local index is written to `.codebase/index.db`.

## Jev ranking

Set `AI_GATEWAY_API_KEY` before using the live Jev ranker:

```text
AI_GATEWAY_API_KEY=...
codescout compare . --issue issue.txt --json
```

The comparison reports heuristic and Jev latency, selected candidates, Jev input-token
usage, and a reminder that ranking quality requires labeled issue cases. Jev receives only
the bounded candidate cards produced by local retrieval; it never generates paths or edits.
