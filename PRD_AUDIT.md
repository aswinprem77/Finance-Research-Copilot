# PRD review and implemented fixes

Review date: 2026-09-19. Specification: PRD.md (v2).
The working Git repository is the nested financial-research-copilot directory.

## Findings and fixes

| Finding | Implemented behavior | Main files |
|---|---|---|
| Stages existed only as manually chained demos | Automatic detection-to-memo pipeline, parallel structured/unstructured ingestion, CLI scheduling | src/pipeline/runner.py, src/pipeline/__main__.py |
| Detection state could suppress retries after a downstream failure | The pipeline records completion only after memo output; failed events retry on later polls; writes are atomic | src/pipeline/runner.py, src/trigger/state_store.py |
| Fiscal quarters inferred from calendar end-month; comparative facts reused filing fiscal year | Infer regular fiscal periods relative to the filing's reporting context and actual dates; reject unsupported durations | src/ingestion/xbrl_parser.py |
| Oldest revision always selected; historical processing could see future facts | Select latest available revision, with a filing-date cutoff in the pipeline | src/ingestion/xbrl_parser.py |
| First existing tag hid historical alternatives; empty/invalid tags counted as covered | Resolve tag alternatives per period and report missing if no usable observations exist | src/ingestion/xbrl_parser.py |
| Long-term debt silently represented total debt | Require a combined-debt tag or an explicit total-debt HTML row; otherwise report a gap | parser, table_fallback.py |
| Restatement detection mixed quarterly/YTD and comparative periods under fy/fp | Match actual start/end dates and accession values; scope pipeline flags to the processed accession | src/judgment/rubric.py |
| Fallback took first numeric value, ignored scale, and could use prior-year data | Require matching date and flow duration, apply thousands/millions/billions, reject ambiguous/spanned tables | src/retrieval/table_fallback.py |
| Synthetic fixture said “thousands” but supplied dollar-sized values | Correct raw table values to thousands and retain original normalized USD expectations | tests/fixtures/sample_filing_excerpt.html |
| Common div-based filing prose was skipped | Capture leaf div prose, keep inline-word spacing, ignore hidden inline-XBRL data | src/retrieval/html_ingest.py |
| Memos omitted figures without comparisons and hid prior-value provenance | Include standalone current facts, prior values/sources, specific fact citations and filing links | src/output/memo.py, src/schema/citations.py |
| Missing figures were invisible in automatic output | Print period coverage and unresolved required concepts; disclose retrieval/imagery/language-screen limits | runner.py |
| No runtime entry point, container, or CI | Add CLI, Dockerfile, secret-excluding build context, offline-test/demo GitHub workflow | README.md, Dockerfile, .github/workflows/tests.yml |

## Validation

106 offline tests passed, including 22 added regression/integration cases. Tests cover fiscal/comparative mapping, unusable tags, revision cutoffs, total debt semantics, table scales/dates/missing cells, unsupported layouts, div/hidden prose, restatement false positives, memo provenance, end-to-end output, retries, failed output writes, duplicate suppression and company failure isolation.

Live SEC smoke tests succeeded for NVIDIA companyfacts and for one complete 10-Q. The memo is in data/live/smoke/0001045810-26-000075.md (ignored runtime output). It reported 80% XBRL coverage and explicitly left total_debt unresolved. Financial values and judgment flags have not been manually certified.

Docker Engine 29.4.3 successfully built the image. The non-root container generated a demo memo in a named volume, suppressed the completed event on a second run, and completed a live SEC poll with no new filings from 2026-09-19. Hosted GitHub Actions has not run. No cloud services were deployed.

## Remaining PRD gaps

| PRD area | Remaining work |
|---|---|
| Semantic retrieval | TF-IDF and lexical reranking remain stand-ins; Azure embedding class is still a stub. Real embeddings and a cross-encoder need implementation/configuration and evaluation. |
| Judgment of changes | Language rules detect mentions, including liquidity/regulatory keywords. They do not establish novelty, negation, materiality or a change from the prior filing. Restatement differences are candidate signals without a calibrated materiality cutoff. |
| Peer comparisons | Calculator utility exists; peer-period alignment and automatic memo tables are not integrated. |
| Quality targets | No labeled 30-query benchmark, Ragas/DeepEval faithfulness evaluation, manually certified numeric accuracy, watchlist-wide 90% coverage, or time-saved benchmark. |
| Service/infrastructure | No FastAPI/UI, PostgreSQL/Redis, durable Qdrant server, LangSmith instrumentation, Azure deployment or PDF export. Docker/CI scaffolding was added, not deployed. |
| Extraction breadth | No extension-taxonomy mapping, YTD subtraction for missing Q2/Q3, robust transition-year calendars, or manual-correction interface. HTML fallback is intentionally limited to unambiguous flat tables. |
| Operational scope | One worker per state file; submissions recent list only; no distributed queue, full-history pagination, intraday acceptance-time snapshots or attachment discovery. |

Do not describe the project as fully PRD-complete or production-ready. The automatic CLI is a working research prototype with explicit remaining gaps.

## Source checks

SEC explains the companyfacts/submissions interfaces and the distinction between calendar frames and company financial calendars in its [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces). The shared SEC client serializes request starts for parallel ingestion in line with the [SEC automated request limit](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits). These references inform the client/parser review; they do not validate the project's financial outputs.
