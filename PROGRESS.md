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
- Local benchmark review interface: exact system values, SEC source links, independent numeric verification, per-passage relevance labels, atomic CSV autosave, progress tracking, and guarded compile/evaluate output.
- Semantic Path B retrieval: a sentence-transformers bi-encoder (BAAI/bge-small-en-v1.5) with a cross-encoder reranker (ms-marco-MiniLM-L-6-v2), selected by retrieval profile. Queries and documents are encoded separately so instruction-tuned models receive their query prefix. Models load lazily and are configurable by environment variable.
- The lexical TF-IDF/overlap stack is retained as an explicit profile, not a fallback. The semantic profile raises rather than degrading quietly when a model is unavailable, so lexical numbers cannot be reported under a semantic label. Tests, CI and the offline demo run on the lexical profile and download nothing.
- Retrieval stack fingerprints: the review queue, compiled benchmark and evaluation report each record the stack that produced their passages. The harness refuses to score labels against a different stack, and compilation rebuilds the exact recorded stack instead of using the current environment.
- Prior-filing narrative change detection: each filing's screened passages are persisted, and the next filing's passages are classified as new, revised, unchanged or unestablished against them. Unchanged repeats are labeled routine and listed separately in the memo; revised passages quote the sentences that are not in the prior filing.
- Comparison is per sentence, scoring each passage by the fraction of its sentences already present in the prior filing. Chunk boundaries follow a size budget, so one edit upstream shifts every later boundary; whole-passage similarity read unchanged boilerplate as new.
- Screening covers every prose chunk rather than only retrieved passages, so a disclosure outside the top-k is still seen. Retrieval now only orders passages within a status, deciding which survive the per-topic cap.
- Peer comparison aligned by actual period end date within 45 days (half a quarter, so the nearest candidate is unambiguous), rendered in the memo with each row's own fiscal label, period end and day offset. Peer facts are snapshotted to the subject's filing date; a failed peer fetch is named in the table rather than dropped; companyfacts are fetched once per poll cycle.
- A filing with no stored predecessor yields `unestablished`, never `new`; the memo says so explicitly. Baselines are written only after the memo is durable, and lookup only reads filings made strictly earlier, so reprocessing cannot see a later filing.

## Verified in this workspace

- 227 offline tests passing (205 before peer comparison, 156 before narrative change detection, 123 before semantic retrieval, 84 before the audit).
- Offline automatic demo generated a memo; repeat polling skips completed events.
- Live SEC companyfacts and submissions reachable using the existing configured User-Agent.
- Live NVIDIA filing 0001045810-26-000075 (10-Q, filed 2026-08-26) generated a memo with 8 metric rows, 10 flags, and 80% XBRL coverage. Latest smoke run took about 1.7 seconds inside process_filing; this excludes submissions polling and is not a latency benchmark.
- Docker Engine 29.4.3: image build succeeded; the non-root container wrote a demo memo to a named volume, a second run skipped the completed accession, and a live SEC poll from 2026-09-19 completed without errors or new filings.
- Real benchmark draft prepared without download/processing errors: seven recent 10-Q filings, 35 retrieval queries (175 result labels), and 29 unique numeric checks. Current-period aggregate XBRL coverage is 82.9% (29/35 concepts). AMD/AVGO are 100%; NVDA/INTC/MU/TXN are 80%; QCOM is 60%. All review labels remain unset by design.
- Hosted GitHub Actions passed the offline test and synthetic-demo workflow on commit `b49aa76`. The workflow and Dockerfile have since changed to install CPU-only torch, and the image now bakes in the retrieval models; neither the rebuilt image nor the changed workflow has been run.
- Real model weights exercised locally: the opt-in semantic test retrieves a passage phrased as “initiated legal proceedings” for the query “pending lawsuit”, which the lexical stack cannot match. Offline demo and live filings both run end to end on the semantic profile, and each memo names the stack that produced its narrative evidence.
- Semantic review draft prepared from live SEC filings into `data/evaluation/real-draft-semantic/`: the same seven 10-Qs, 35 queries, 175 passage labels, 29 numeric checks, no errors. Aggregate current-period XBRL coverage is unchanged at 82.9% (29/35), as expected — this phase did not touch Path A. AMD/AVGO 100%; NVDA/INTC/MU/TXN 80% (total_debt unresolved); QCOM 60% (gross_profit and total_debt unresolved). All labels remain unset by design.
- Semantic `process_filing` latency across those seven filings ranged from 12.8s to 101.9s, against the PRD target of under 10 minutes. This excludes submissions polling and model load, and is a small sample, not a latency benchmark.
- On NVIDIA's filing the semantic stack surfaced four litigation and four liquidity passages where the lexical stack surfaced two of each. That records a change in what retrieval returns, not an improvement; only the labels can establish quality.
- The earlier `data/evaluation/real-draft/` queue is intact and now stamped `lexical|tfidf|lexical_overlap`, the stack that actually produced it, so the harness cannot score its labels under the semantic stack.
- Narrative change detection run on two consecutive live NVIDIA 10-Qs (0001045810-26-000052 filed 2026-05-20, then 0001045810-26-000075 filed 2026-08-26). The first filing produced 13 screened passages, all `unestablished`. The second produced 16 passages against that baseline.
- That run is what caught the whole-passage comparison being wrong: it classified all 16 as new or revised and none as unchanged. After switching to sentence coverage the same pair yields 3 unchanged, 7 revised and 6 new. Standard liquidity boilerplate moved from `new` to `revised`, which is what it is.
- Peer alignment checked against the cached watchlist companyfacts for NVIDIA's 10-Q (period ended 2026-07-26, filed 2026-08-26). Label matching via compute_peer_comparison returned a value for NVIDIA and None for all six peers. Date alignment returned four: TXN -26d, QCOM -28d, AMD -29d, INTC -29d, with NVIDIA at 62.0% net margin against 36.2/20.1/19.9/-68.4%.
- The two unaligned peers fail for different reasons, and the memo now says which. Broadcom's quarter ended 2026-08-02, 7 days from the subject, but was filed 2026-09-10, after the subject filing, so the as-of snapshot correctly excludes it; its nearest available period ends 2026-05-03 (-84d). Micron has no quarter near the date at all; nearest ends 2026-05-28 (-59d).
- The 6 remaining `new` flags include NVIDIA's AI-cloud land/power/shell guarantees, the SB Energy guarantee capped at $105 billion, and an indenture-covenant risk factor. All three are plausibly genuine given the $25.0 billion note issuance in June 2026, i.e. after the baseline filing, but none has been checked against the prior filing's text by hand. No labeled sample exists, so false-positive and false-negative rates remain unmeasured.

## Next work, in priority order

1. Run `python -m src.evaluation.review_app --queue-dir data/evaluation/real-draft-semantic`, independently verify all 29 values and label all 175 passages, then compile and run the real benchmark. This is the semantic-stack queue, so the labels will measure the stack the system actually ships with. Calibrate missing debt concepts without conflating partial balances with total debt.
2. Measure retrieval precision from those labels and decide whether bge-small plus the MiniLM cross-encoder clears the PRD's 85% target, or whether a larger model is warranted. No retrieval-quality number has been measured yet.
3. Label a sample of the narrative flags on real consecutive filings and calibrate SENTENCE_MATCH_SIMILARITY, UNCHANGED_COVERAGE and REVISED_COVERAGE against it. The mechanism is built and running; its thresholds are guesses and its false-positive/false-negative rates are unmeasured. Start with the NVIDIA pair already processed.
4. Extend peer comparison beyond revenue, net income and the two margins if an analyst wants more, and consider seasonality: aligned quarters still cover different weeks of trading, and nothing adjusts for that. Peer narrative comparison does not exist.
5. Add measured faithfulness/numeric-accuracy/coverage reporting. No PRD success metric has been certified by these unit tests.
6. Add API/UI, durable database/queue, PDF output and cloud deployment as required for the next phase.

See PRD_AUDIT.md for the reviewable change list and detailed limitations. Use the CLI in README.md for the current automatic workflow. The old examples remain stage-level demonstrations.
