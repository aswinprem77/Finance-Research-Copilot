# Current progress

Updated 2026-09-19. This replaces the old session log, whose network restrictions and “orchestration not built” statements no longer describe this workspace. The previous text remains in Git history.

## Implemented

- XBRL parser supports regular non-calendar fiscal years, date-based comparative mapping, per-period tag alternatives, as-of filtering, current revisions, and derived Q4 provenance.
- Coverage reflects usable facts; the automatic pipeline computes coverage for the actual filing period. Long-term debt is no longer mislabeled total debt.
- HTML fallback chooses explicit date/duration columns and applies scale. Complex/ambiguous layouts remain unresolved.
- HTML ingestion supports leaf div prose and excludes hidden inline-XBRL blocks.
- Restatement screening compares actual start/end dates instead of filing fy/fp labels.
- Automatic pipeline fetches Path A/Path B concurrently, scopes comparisons to the filing, retrieves narrative evidence, screens findings, writes a memo, and acknowledges only after durable output.
- CLI supports offline demo, live one-shot polling, and a scheduled polling loop.
- Memo exposes missing figures, unpaired fallback values, current/prior provenance, and source links.
- Atomic JSON completion writes, per-filing failure isolation, separate demo/live state, Dockerfile and GitHub Actions workflow.
- Phase 5 evaluation foundation: versioned benchmark schema, deterministic numeric/coverage/retrieval/citation-support/latency metrics, JSON/Markdown reports, and explicit separation of synthetic results from PRD certification.

## Verified in this workspace

- 118 offline tests passing (84 before the audit).
- Offline automatic demo generated a memo; repeat polling skips completed events.
- Live SEC companyfacts and submissions reachable using the existing configured User-Agent.
- Live NVIDIA filing 0001045810-26-000075 (10-Q, filed 2026-08-26) generated a memo with 8 metric rows, 10 flags, and 80% XBRL coverage. Latest smoke run took about 1.7 seconds inside process_filing; this excludes submissions polling and is not a latency benchmark.
- Docker Engine 29.4.3: image build succeeded; the non-root container wrote a demo memo to a named volume, a second run skipped the completed accession, and a live SEC poll from 2026-09-19 completed without errors or new filings.
- Real benchmark draft prepared without download/processing errors: seven recent 10-Q filings, 35 retrieval queries (175 result labels), and 29 unique numeric checks. Current-period aggregate XBRL coverage is 82.9% (29/35 concepts). AMD/AVGO are 100%; NVDA/INTC/MU/TXN are 80%; QCOM is 60%. All review labels remain unset by design.
- Hosted CI remains unverified until the workflow runs on GitHub.

## Next work, in priority order

1. Complete `data/evaluation/real-draft/numeric_review.csv` and `retrieval_review.csv`, then compile/run the real benchmark. Calibrate missing debt concepts without conflating partial balances with total debt.
2. Add semantic embeddings/cross-encoder providers and measure retrieval against a labeled 30-query set.
3. Persist prior-filing narrative evidence and detect changes rather than keyword mentions. Calibrate rubric thresholds and measure false positives/negatives.
4. Integrate peer comparisons into memos with explicit alignment across different fiscal calendars.
5. Add measured faithfulness/numeric-accuracy/coverage reporting. No PRD success metric has been certified by these unit tests.
6. Add API/UI, durable database/queue, PDF output and cloud deployment as required for the next phase.

See PRD_AUDIT.md for the reviewable change list and detailed limitations. Use the CLI in README.md for the current automatic workflow. The old examples remain stage-level demonstrations.
