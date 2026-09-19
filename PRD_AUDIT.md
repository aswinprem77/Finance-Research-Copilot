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
| No repeatable quality evaluation | Add a versioned benchmark contract, deterministic metric runner, reports, tests, and CI execution; keep unsupported PRD metrics visibly unmeasured | src/evaluation, benchmarks, tests/test_evaluation.py |
| Manual review depended on editing large CSVs directly | Add a local-only review interface with SEC links, exact values, atomic label saves, progress, and strict compilation gates; never auto-label ground truth | src/evaluation/review_app.py, src/evaluation/review.html |
| PRD Section 5 Path B specified semantic embeddings and a cross-encoder; both were lexical stand-ins | Add a sentence-transformers bi-encoder and cross-encoder reranker, selected by profile, with asymmetric query encoding and lazy model loading; keep the lexical stack for hermetic tests and CI | src/retrieval/embeddings.py, rerank.py, providers.py |
| PRD Stage 4 specifies new/changed language flags; the rubric only detected keyword mentions in the current filing | Persist each filing's screened narrative and classify every passage against the company's previous filing as new, revised, unchanged or unestablished; unchanged boilerplate is labeled routine and separated in the memo | src/judgment/narrative.py, narrative_store.py, output/memo.py |
| Keyword screening only saw passages retrieval returned, so a disclosure outside the top-k was invisible | Screen every prose chunk deterministically; retrieval now only orders passages within a status | narrative.py, pipeline/runner.py |
| PRD Stage 3 specifies peer comparison, but label matching silently returned nothing across differing fiscal calendars | Align peers on actual period end date within half a quarter, snapshot peer facts to the subject's filing date, and render each row's period and offset with unaligned peers explained | src/analyst/peers.py, output/memo.py, pipeline/runner.py |
| Retrieval labels carried no record of which retrieval stack produced the passages | Stamp a stack fingerprint on the review queue, the compiled benchmark and the report; refuse to score labels under a different stack; rebuild the recorded stack when compiling | providers.py, prepare.py, compile.py, harness.py, review_app.py |

## Validation

227 offline tests pass. Tests cover fiscal/comparative mapping, unusable tags, revision cutoffs, total debt semantics, table scales/dates/missing cells, unsupported layouts, div/hidden prose, restatement false positives, memo provenance, end-to-end output, retries, failed output writes, duplicate suppression, company failure isolation, benchmark metrics, review exports, local review persistence/HTTP behavior, strict labeled-benchmark compilation, retrieval-profile selection, query/document prefix handling, cross-encoder ordering, stack-fingerprint enforcement, narrative screening and date normalization, new/revised/unchanged/unestablished classification, baseline durability and as-of lookup, memo severity separation, stability of the comparison under shifted chunk boundaries, and peer alignment across differing fiscal calendars. The test suite is pinned to the lexical stack so it needs no model download; one opt-in test loads real weights and is skipped by default.

Live SEC smoke tests succeeded for NVIDIA companyfacts and for one complete 10-Q. The memo is in data/live/smoke/0001045810-26-000075.md (ignored runtime output). It reported 80% XBRL coverage and explicitly left total_debt unresolved. Financial values and judgment flags have not been manually certified.

Docker Engine 29.4.3 successfully built the image. The non-root container generated a demo memo in a named volume, suppressed the completed event on a second run, and completed a live SEC poll with no new filings from 2026-09-19. Hosted GitHub Actions passed the offline tests and synthetic demo on commit `b49aa76`. No cloud services were deployed.

A real review draft was generated for the seven-company watchlist: seven recent 10-Qs, 35 retrieval queries, 175 result judgments to label, and 29 unique numeric checks. Draft XBRL coverage is 82.9% (29/35 concepts), below the PRD target. Labels are deliberately blank; these results cannot certify accuracy, precision, or faithfulness until reviewed.

## Remaining PRD gaps

| PRD area | Remaining work |
|---|---|
| Semantic retrieval | Implemented: BAAI/bge-small-en-v1.5 embeddings with a ms-marco-MiniLM cross-encoder reranker, default profile. The Azure embedding class is still a stub - no credentials are configured. Retrieval quality is not yet measured: that needs the labeled 30-query set, which must be labeled against this stack. |
| Judgment of changes | Language rules now establish change against the company's previous filing and label unchanged repeats routine. They still do not establish negation or materiality, and a passage reworded to the same meaning reads as new. Similarity thresholds and restatement materiality remain uncalibrated: no labeled sample of real filing-to-filing edits has been measured, so false-positive and false-negative rates are unknown. |
| Peer comparisons | Implemented: peers aligned by period end date within 45 days, rendered in the memo with each row's own fiscal label, period end and day offset, and unaligned peers listed with their nearest available period. Label-based matching returned no peers at all for NVIDIA's Q2 FY2027. Still limited to revenue, net income and the two margins, with no seasonality adjustment and no peer narrative comparison. |
| Quality targets | No labeled 30-query benchmark, Ragas/DeepEval faithfulness evaluation, manually certified numeric accuracy, watchlist-wide 90% coverage, or time-saved benchmark. |
| Service/infrastructure | The benchmark reviewer is a local standard-library UI, not the PRD product service. No FastAPI product API, PostgreSQL/Redis, durable Qdrant server, LangSmith instrumentation, Azure deployment or PDF export exists. Docker/CI scaffolding was added, not deployed. |
| Extraction breadth | No extension-taxonomy mapping, YTD subtraction for missing Q2/Q3, robust transition-year calendars, or manual-correction interface. HTML fallback is intentionally limited to unambiguous flat tables. |
| Operational scope | One worker per state file; submissions recent list only; no distributed queue, full-history pagination, intraday acceptance-time snapshots or attachment discovery. |

Do not describe the project as fully PRD-complete or production-ready. The automatic CLI is a working research prototype with explicit remaining gaps.

## Source checks

SEC explains the companyfacts/submissions interfaces and the distinction between calendar frames and company financial calendars in its [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces). The shared SEC client serializes request starts for parallel ingestion in line with the [SEC automated request limit](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits). These references inform the client/parser review; they do not validate the project's financial outputs.
