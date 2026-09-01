# Progress Tracker — Financial Research Copilot

**Read this file first in any new session.** It tells you exactly where the build stands, what's working, what's not, and what to do next. Update it before you run out of context — that's the whole point of this file.

---

## Current phase (per PRD v2 roadmap)

**Phase 1 (Weeks 1-2): XBRL ingestion + Calculator** — DONE, with one caveat (see below).
**Phase 2 (Weeks 3-5): Unstructured retrieval (Path B)** — Core pipeline built and tested, wired to Path A (gap-fill orchestration), and watchlist locked in — see "Key decisions locked in" below.
**Phase 3 (Weeks 6-7): Judgment agent + Trigger agent** — Both built: rubric engine (4 rules, all tested, every flag cites its rule + source) and Trigger agent (EDGAR-submissions polling + persisted seen-state, 14 tests). **Phase 3 is feature-complete per the PRD's own Stage 1/Stage 4 scope.**
**Phase 4 (Weeks 8-9): Output agent** — Built this session (`src/output/memo.py`): generates the memo (executive summary, metric table with per-row data provenance, flagged items with citations), structurally incapable of a buy/sell/hold recommendation (no field for one exists, not a prompt instruction). Full pipeline orchestration (Trigger → Ingestion → Calculator → Judgment → Output as one runnable flow) is NOT built yet — see below.

**Note on sessions so far:** the dev sandbox's working directory has now reset TWICE across this project's build (fresh container each time) — exactly the "file system resets between tasks" behavior PROGRESS.md's whole existence anticipates. Both times, recovery was clean: unzip the last delivered zip from `/mnt/user-data/outputs/`, reinstall requirements, confirm the test count matches what was last verified, then continue. No work has been lost either time. If a new session starts and `/home/claude/financial-research-copilot` doesn't exist, this is normal — don't treat it as an error, just recover from the zip and move on.

**Note on sequencing:** earlier sessions' plan was "don't start Phase 3 until the real-environment smoke test runs" — that test STILL hasn't run (still blocked on this sandbox's lack of network access to `data.sec.gov`/real filings, see below), but the user explicitly decided to proceed to Phase 3 anyway rather than wait indefinitely on an environment-dependent step. That's a legitimate call for them to make — the rubric engine's MECHANISM doesn't need real data to build or test correctly (same as everything else in this project); only the specific THRESHOLD VALUES need real-data calibration eventually. Don't re-litigate this decision; do keep flagging, clearly, which parts still need real-world validation.

## Phase 1 — what's built and working

- [x] Shared schema (`src/schema/financial_schema.py`) — `FinancialFact`, `CompanyFinancials`, `FactSource` (`xbrl` / `html_table_fallback`), `FinancialConcept`, `FiscalPeriod`. Both Path A and Path B populate this same schema.
- [x] `src/ingestion/xbrl_client.py` — SEC XBRL companyfacts client. Rate-limited (stays under SEC's 10 req/sec), local file caching, requires a real User-Agent (validated at construction).
- [x] `src/ingestion/xbrl_parser.py` — raw JSON → `FinancialFact`s. Tag-fallback list per concept (`TAG_FALLBACKS`). Correctly derives fiscal period from duration span rather than trusting form type alone.
  - **Fixed this session:** the span-based period inference didn't distinguish a discrete ~3-month quarter from a ~6-/9-month YTD cumulative duration reported under the *same* fy/fp label (both map to the same end-month) — real 10-Qs commonly report both for the same concept. Now only spans of 75-100 days are accepted as a discrete quarter; anything between quarter-length and year-length is filtered out rather than silently misclassified. See `_infer_fiscal_period()` docstring. Fixture updated with a real YTD-duplicate case to exercise this; regression test: `test_parser_filters_ytd_cumulative_duplicates`.
  - **Fixed this session (separate issue, same file):** XBRL has no raw Q4-only duration fact for filers who only tag the full fiscal year — Q4 was previously silently absent from every comparison. `_derive_missing_q4_facts()` now derives Q4 = FY - Q1 - Q2 - Q3 for flow concepts (revenue, net income, gross profit, operating income) when all four inputs are directly resolved and no discrete Q4 is already reported (never overrides a real reported value). Deliberately excludes `TOTAL_DEBT` and other balance-sheet/instant concepts — a snapshot figure has no "FY total" to subtract from; its Q4 value is just the fiscal-year-end balance, already resolved directly. Derived facts stay `source=XBRL` (still fully deterministic) but get `source_tag="derived:FY-Q1-Q2-Q3 (...)"` so provenance stays honest. `compute_qoq`/`compute_yoy` needed zero changes — they just read whatever's in `facts_for(concept)`. Tests: `test_q4_derived_when_fy_and_all_three_quarters_present`, `test_q4_not_overridden_when_already_directly_reported`, `test_q4_not_derived_with_partial_data`, `test_qoq_and_yoy_pick_up_derived_q4_with_no_extra_wiring`, `test_total_debt_never_gets_derived_q4`.
  - **Also fixed this session:** three stale docstring claims caught while in this file — `calculator.py`'s module docstring still said peer comparison "is NOT implemented yet" (it was added two sessions ago); `xbrl_parser.py` had two separate comments still calling the coverage-rate metric "not yet instrumented/wired up" (`coverage.py` has existed since then too). Fixed all three. Worth a periodic grep for "not yet" / "NOT implemented" across the codebase — these drift silently once the referenced work actually ships.
- [x] `src/analyst/calculator.py` — `compute_yoy`, `compute_qoq` (handles fiscal-year rollover), `compute_margin`, and (added this session) `compute_peer_comparison` — same-period figure across a peer set, order-preserving, `None` for peers missing the concept rather than dropping them silently.
- [x] `src/ingestion/coverage.py` (added this session) — turns the parser's `missing` dict into the actual XBRL coverage-rate metric named in PRD v2 Section 4, per-company (`CoverageReport`) and aggregated across a watchlist (`watchlist_coverage_rate`, resolved/total weighted by concept count — not a plain average of per-company rates).
- [x] Tests: `test_calculator.py`, `test_peer_and_coverage.py` — all passing (see verified run below).

### Known gap — carried over, still true
`xbrl_client.get_company_facts()` has never touched the real SEC endpoint. **This sandbox cannot reach `data.sec.gov` in any session** — its outbound network is allow-listed to package registries and GitHub only, which is a platform-level restriction, not something that changes next session. This step needs your own machine (or any environment with normal internet access): run `get_company_facts()` against a real CIK and confirm the parser holds up — real filings may have tag-name or unit quirks the fixture doesn't cover. The fixture now includes a realistic YTD-duplicate case, but real filings will have more variety than any hand-built fixture anticipates.

## Phase 2 — what's built and working

Path B per PRD v2 Section 5, Stage 2: HTML prose/table ingestion, semantic (structure-aware) chunking, hybrid BM25+dense retrieval, reranking, HTML-table fallback extraction.

- [x] `src/retrieval/html_ingest.py` — parses filing HTML into ordered `ProseBlock`/`TableBlock` objects, each tagged with its nearest heading as `section`. Uses BeautifulSoup+lxml, genuinely real, no stand-in.
- [x] `src/retrieval/chunking.py` — structure-aware chunking: prose merges within a section up to a char budget, **never** across a section boundary; a table is **always** its own chunk, never merged with prose or split — so a number is never separated from the row/column label that gives it meaning (this was the specific requirement PRD v2 called out).
- [x] `src/retrieval/table_fallback.py` — extracts `FinancialFact`s from `TableBlock`s via conservative label-pattern matching (`LABEL_PATTERNS`), tagged `source=HTML_TABLE_FALLBACK`. Unmatched row labels are skipped, not guessed at — same philosophy as `TAG_FALLBACKS` in Phase 1.
- [x] `src/retrieval/hybrid_index.py` — real BM25 (`rank_bm25`) + real Qdrant vector search (`qdrant-client`, local in-memory mode — genuine vector search, no server process needed), merged via Reciprocal Rank Fusion (rank-based, so it doesn't need the two different score scales normalized against each other).
- [x] `src/retrieval/embeddings.py` — `EmbeddingProvider` protocol, so swapping the real embedding model in later touches no other file.
- [x] `src/retrieval/rerank.py` — reranking stage, same swappable-interface approach.
- [x] Synthetic fixture: `tests/fixtures/sample_filing_excerpt.html` — fake 10-Q excerpt (MD&A with a results table, Risk Factors with a litigation paragraph) for offline testing.
- [x] Tests: `test_html_ingest.py`, `test_chunking.py`, `test_table_fallback.py`, `test_hybrid_index.py`, `test_rerank.py` — all passing.
- [x] End-to-end demo: `examples/run_phase2_demo.py`, run and verified this session.

### What's real vs. a stand-in in Phase 2 (important — read before trusting retrieval quality)

| Piece | Status |
|---|---|
| HTML parsing (BeautifulSoup) | **Real.** Works on any HTML you give it. |
| Chunking logic | **Real.** Structure-aware boundaries are genuine, not simplified for the demo. |
| BM25 (`rank_bm25`) | **Real.** Production-appropriate as-is. |
| Qdrant usage (in-memory mode) | **Real vector search**, just not persisted to disk — swap `QdrantClient(":memory:")` for a server URL for production; no other code changes. |
| **Embeddings (`TfidfEmbeddingProvider`)** | **Stand-in.** Real TF-IDF vectors (not mocked), but lexical, not semantic — will miss synonyms/paraphrases. This sandbox has no path to Hugging Face Hub or Azure OpenAI to get a real embedding model, same network restriction as the SEC API. `AzureOpenAIEmbeddingProvider` is stubbed with a clear `NotImplementedError` — implement it from an environment with Azure credentials; `hybrid_index.py` needs no changes to accept it. |
| **Reranker (`rerank_lexical_overlap`)** | **Stand-in**, same reason — a real cross-encoder also needs a Hugging Face model download this sandbox can't do. Interface is swappable the same way. |
| Table→fact label matching | **Real but narrow** — conservative pattern list, will need to grow against real filings, exactly like `TAG_FALLBACKS` did in Phase 1. |

None of this blocks progress: the *pipeline shape* (ingest → chunk → index → search → rerank → fallback-extract) is fully real and tested end-to-end. What's swappable is specifically the two pieces that need a downloaded model or paid API this sandbox can't reach — both isolated behind interfaces for exactly that reason.

### Not done yet in Phase 2
- [ ] Nothing runs against a *real* filing yet — only the synthetic fixture. Next real-world task, same shape as Phase 1's: run the full Path B pipeline against an actual downloaded 10-Q HTML file and see what breaks (real filing HTML is messier than the fixture — nested tables, inline styling standing in for structure, multiple tables per section).
- [ ] No persistence — the in-memory Qdrant index and everything else lives only for the life of one Python process. Fine for this dev stage; will matter once Trigger/Judgment/Output agents need to query facts ingested in an earlier run.
- [x] **Path A + Path B are now wired together** (`src/pipeline/stage2_gap_fill.py`, added this session): `fill_coverage_gaps_from_html()` takes a `CoverageReport`'s missing concepts, parses the filing HTML, and merges in only the matching HTML-table facts — XBRL-resolved concepts are never touched or overridden. Verified end-to-end: `examples/run_gap_fill_demo.py` (see output below) and `tests/test_stage2_gap_fill.py`.
- [ ] Table→fact extraction always takes "the first numeric cell" as the current period's value — correct for the fixture's label|current|prior layout, but real filings vary this layout more; worth a second look once tested against real HTML.

## Phase 3 — what's built and working

Judgment agent per PRD v2 Section 5, Stage 4: rubric-based flagging, every flag citing its rule + source (`src/judgment/rubric.py`).

- [x] `flag_metric_changes()` — flags a YoY/QoQ `ComparisonResult` (from `calculator.py`) whose percent change exceeds a threshold (default 20%). Works on whatever `compute_yoy`/`compute_qoq` already produce — no new data path needed.
- [x] `flag_margin_changes()` — flags a YoY margin move exceeding a threshold in PERCENTAGE POINTS, not percent (default 5pp) — deliberately distinct units since a margin moving from 40%→35% is very different from a raw metric moving -12.5%.
- [x] `detect_restatements()` — a SEPARATE pass over raw XBRL JSON (not an extension of `parse_company_facts()`'s return signature, to avoid a breaking change to every existing call site) that flags when the same (concept, fiscal_year, fiscal_period) was reported with different values across different accession numbers. Tested with an inline minimal fixture in `test_judgment_rubric.py`, not the shared fixture — the shared one has no restatement scenario in it, deliberately kept clean as the "normal" case.
- [x] `flag_litigation_language()` — keyword-based flagging on Path B prose chunks (never table chunks, checked explicitly). Same "narrow, explicit, grow it later" philosophy as `LABEL_PATTERNS` in `table_fallback.py` — a keyword match is explainable; an LLM's internal judgment call isn't, which is the actual point of this whole module existing.
- [x] Tests: `test_judgment_rubric.py`, 12 tests, all passing. Demo: `examples/run_judgment_demo.py`, run and verified this session.

### Important caveat — read before trusting any flag output
**The threshold VALUES (20% metric change, 5pp margin change) are reasonable defaults, not calibrated against real filings or this specific watchlist's normal volatility.** The MECHANISM (rubric evaluation, citation tracking, versioning) is fully real and tested; the SPECIFIC NUMBERS are a starting point. Recalibrating later means changing the constants in `rubric.py`, not the logic — same "real mechanism, provisional specifics" pattern as `TfidfEmbeddingProvider` in Phase 2. Per the PRD's own risk table ("track false-positive/false-negative rate against a manually labeled sample set"): that tracking needs real flagged output from real filings to label against — can't be done against synthetic data in good faith, and hasn't been claimed as done here.

### Not done yet in Phase 3
- [ ] Nothing wires the Judgment agent's output to a specific filing's actual retrieved chunks/facts automatically — the demo calls each rubric function manually with the right inputs. A real pipeline run (Trigger → Ingestion → Calculator → Judgment) needs an orchestrator tying these together, which doesn't exist yet (see PRD Section 5 "Orchestration: simple state machine"). The Trigger agent below detects new filings but doesn't yet CALL into Ingestion/Calculator/Judgment for them — that's the Phase 4 orchestration task, not this one.
- [ ] `LITIGATION_KEYWORDS` is a short starter list (9 terms) — real Risk Factors sections will use language this doesn't catch yet (regulatory investigations, product recalls, IP disputes, etc. don't overlap much with the current litigation-specific list). Same "grow it as you test against real filings" story as every other pattern list in this project.
- [ ] No rubric rule for "debt covenant or liquidity language changes" yet (PRD Section 5 names this explicitly as one of four example rules — this session built the other three plus restatement detection instead).

## Trigger agent — what's built and working

Stage 1 per PRD v2 Section 5: polls SEC EDGAR for new filings against the watchlist. `src/trigger/`.

- [x] `edgar_client.py` — `fetch_submissions(cik, user_agent)` (real endpoint, same untested-live status as `xbrl_client.py` — see caveat below) + `parse_recent_filings()`, which normalizes SEC's parallel-array `filings.recent` block into `FilingEvent` objects, filtered to `RELEVANT_FORMS = {10-K, 10-Q, 8-K}` (PRD Section 6's data sources — a CIK's real filing history includes many other form types that aren't in scope, filtered here so nothing downstream has to re-decide this).
- [x] `state_store.py` — persisted (file-backed JSON) set of seen accession numbers, so repeated polling only fires on genuinely new filings. Default path `data/trigger_state.json`; tests use `tempfile` so they never touch the real project state.
- [x] `trigger_agent.py` — `poll_watchlist_for_new_filings()`: for each watchlist company, fetch (dependency-injected, same pattern as `watchlist.py`), diff against seen-state, return only new `FilingEvent`s, update state. Detection only — does NOT call into the rest of the pipeline for each new event; see "Not done yet" above.
- [x] Tests: `test_edgar_client.py` (5), `test_state_store.py` (4), `test_trigger_agent.py` (5) — 14 total, all passing. Demo: `examples/run_trigger_demo.py`, run and verified.

### A real gotcha caught building this (worth knowing, not just historical)
Real SEC accession numbers are prefixed by the filing company's own CIK, so two different companies never share one literally. The first version of the multi-company test reused one synthetic fixture verbatim for two different CIKs, creating an accession-number collision that could never happen with real data — the seen-state correctly treated the second company's "filings" as already-seen, which LOOKED like a bug (only 3 events instead of the expected 6) but was actually the test's fixture design being wrong, not the code. Fixed by synthesizing distinct per-company accession numbers in the stand-in fetch (see `_stand_in_fetch_distinct_per_company` in `test_trigger_agent.py`) — and kept the collision case as its own documented test (`test_identical_accession_numbers_across_companies_would_collide`) rather than just deleting the confusion. `run_trigger_demo.py` uses the same per-company synthesis for the same reason.

### Still needs the real-environment smoke test, same as everything else
`fetch_submissions()` is written to SEC's real API shape but has never touched `data.sec.gov` — identical situation to `xbrl_client.get_company_facts()`. Add this to the same real-machine test run.

## Output agent — what's built and working

Stage 5 per PRD v2 Section 5: generates the memo. `src/output/memo.py`.

**Note on sequencing:** the previous session flagged Output as worth holding off on until the real-environment smoke test ran, specifically because — unlike Judgment/Trigger, where the MECHANISM didn't need real data — Output's whole job is representing trustworthiness to a human reader, so what it's built against matters more. The user said to continue anyway. That's their call to make, same as the earlier Phase 3 sequencing decision. The way this session resolved the actual underlying concern (not just noted it and moved on): made caveat/provenance surfacing a STRUCTURAL requirement of the memo rather than a documentation note — see below. Don't treat "we proceeded anyway" as evidence the concern didn't matter; treat the design of this module as the answer to it.

- [x] `generate_memo()` — **`data_provenance_note` is a required parameter with no default.** A caller physically cannot generate a memo without stating in plain language what the numbers are based on (real data with a date, or synthetic test fixture, etc.) — this can't be forgotten or silently skipped, by construction, not convention.
- [x] `build_metric_rows()` — turns Calculator's `ComparisonResult`s into memo rows, and for EACH row looks up the underlying `FinancialFact`'s actual provenance (xbrl / derived / html_table_fallback) — `ComparisonResult` itself doesn't carry this, confirmed by re-reading `calculator.py` before building on top of it rather than assuming.
- [x] `Memo.to_markdown()` — renders the disclaimer (verbatim quotes PRD Section 2's non-goal), the required provenance note, a deterministic (non-LLM, template-based) executive summary, the metric table with a provenance column, and every flag with its citation plus an explicit "For human review" line.
- [x] **Structurally cannot produce a buy/sell/hold recommendation** — there is no field for one anywhere in `Memo` or `MetricRow`, and no code path adds one. This is enforced by there being no LLM anywhere in this module (same as every other stage) — it only ever emits values it was explicitly given.
- [x] Tests: `test_output_memo.py`, 7 tests, all passing — including an actual end-to-end integration test that reuses real XBRL parsing, gap-fill, Calculator, and Judgment output together, not mocked pieces.
- [x] Demo: `examples/run_output_demo.py` — runs the FULL pipeline (Path A → Path B gap-fill → Calculator → Judgment → Output) in one script and writes a real memo to `examples/sample_memo_output.md`. This is the first demo that isn't phase-isolated.

### A real test-design catch worth knowing
The first version of `test_memo_never_contains_recommendation_language` naively banned the words "buy"/"sell"/"hold" from the ENTIRE rendered memo — and failed, because the disclaimer itself correctly says "does not... suggest a buy/sell/hold action," which contains the word "buy" as a substring. That's the disclaimer doing its job, not a violation. Fixed by checking only the content AFTER the disclaimer line. Worth remembering when writing similar "never contains X" tests elsewhere: a keyword ban can't distinguish "does this" from "explicitly says it doesn't do this."

### Not done yet in Phase 4
- [ ] **Full pipeline orchestration — not built.** `run_output_demo.py` calls each stage manually in sequence; there's no state machine or orchestrator (LangGraph or hand-rolled, per PRD Section 5) that the Trigger agent's new-filing events actually feed into automatically. This is genuinely the next PRD component, not optional polish.
- [ ] Markdown output only — PDF and Slack/email webhook (PRD's stretch goals) aren't built.
- [ ] No handling yet for a memo covering MULTIPLE concepts/companies at once cleanly — the demo does one company, one concept (revenue). Extending `build_metric_rows` to loop over `TARGET_CONCEPTS` and multiple companies is straightforward but not done.

## Immediate next steps (in order)

1. **From your own machine** (this sandbox genuinely cannot do this step, in this or any future session): smoke-test `xbrl_client.get_company_facts()` AND `trigger_agent.edgar_client.fetch_submissions()` against real CIKs, and run the Phase 2 pipeline against one real downloaded 10-Q HTML file. **Still the single most valuable thing to do** — every threshold and pattern list in this project is tuned against synthetic data only.
2. Full pipeline orchestration — wire Trigger's new-filing events into Ingestion → Calculator → Judgment → Output as one runnable flow (PRD Section 5's "simple state machine"). This is what turns 6 separate demos into one working product.
3. Debt covenant / liquidity language rubric rule, growing `LITIGATION_KEYWORDS`, multi-concept/multi-company memo output — all lower-effort extensions once 1 and 2 are further along.

## Key decisions locked in (don't re-litigate these)

- XBRL is the primary numeric source; HTML table parsing is fallback-only, explicitly tagged.
- All numeric computation happens in deterministic Python, never LLM-generated.
- Vision/image pipeline is explicitly out of scope for v1.
- Chunking never splits a table from its labels, and never merges prose across a section boundary.
- Hybrid retrieval merge uses Reciprocal Rank Fusion, not raw score blending (the two rankers' scores aren't on comparable scales).
- **Watchlist: Semiconductors — NVDA, AMD, INTC, AVGO, QCOM, MU, TXN** (`config/watchlist.json`, CIKs verified against live SEC filing records). Rationale for the sector is in the config file. Don't re-pick this without a real reason — re-litigating it resets the "peer comparison is ready to run for real" progress made this session.
- Target stack per PRD Section 7: Qdrant, Postgres, Redis, FastAPI, LangGraph, Azure OpenAI, Ragas/DeepEval, LangSmith, Docker, GitHub Actions, Azure. Still only Qdrant (in-memory) is touched so far — Postgres/Redis/FastAPI/Docker are all still ahead (Phase 4+).

## Notes / gotchas learned so far

- SEC EDGAR requires a descriptive `User-Agent` header (e.g. `"YourName YourEmail@example.com"`) or it rejects the request. Set via `SEC_USER_AGENT` in `.env` (see `.env.example`). Never commit a real `.env`.
- US-GAAP tags are NOT perfectly consistent across filers (`Revenues` vs. `RevenueFromContractWithCustomerExcludingAssessedTax`, etc.) — `TAG_FALLBACKS` in `xbrl_parser.py` tries a priority list per concept; grow it as you test real filers.
- XBRL duration facts for the same fy/fp label can be either a discrete quarter OR a YTD cumulative — see the Phase 1 fix above. The same *class* of problem (one label, multiple durations/granularities) shows up again in Phase 2 if a filer's HTML table mixes quarterly and YTD columns without a clear header — worth watching for once tested against real filings.
- Relatedly: don't assume every filer reports a discrete Q4 duration fact at all — many only tag the full fiscal year in their 10-K, meaning Q4 must be derived (FY - Q1 - Q2 - Q3, now implemented). This ONLY applies to flow/duration concepts (revenue, income, margin items) — balance-sheet/instant concepts (debt, assets, liabilities) don't have this problem, since their "Q4" value is just whatever balance was reported as of fiscal year end, already captured directly.
- Real cross-encoders and real embedding models both need a Hugging Face Hub download; this sandbox's network allowlist doesn't include huggingface.co (only package registries + GitHub). Same class of constraint as `data.sec.gov` — plan for a non-sandbox environment for anything needing those.

## Last verified run (this session)

```
$ pytest -v
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0
collected 84 items

tests/test_calculator.py ..............                                [ 16%]
tests/test_chunking.py .....                                          [ 22%]
tests/test_edgar_client.py .....                                      [ 28%]
tests/test_html_ingest.py .....                                       [ 33%]
tests/test_hybrid_index.py .....                                      [ 39%]
tests/test_judgment_rubric.py ............                            [ 54%]
tests/test_output_memo.py .......                                     [ 63%]
tests/test_peer_and_coverage.py ......                                [ 70%]
tests/test_rerank.py ...                                              [ 73%]
tests/test_stage2_gap_fill.py .....                                   [ 79%]
tests/test_state_store.py ....                                        [ 85%]
tests/test_table_fallback.py ....                                     [ 89%]
tests/test_trigger_agent.py .....                                     [ 95%]
tests/test_watchlist.py ....                                          [100%]

============================== 84 passed in 2.47s ===============================
```

Phase 1 demo output now includes the derived Q4 flowing through automatically (zero changes
needed in calculator.py):

```
$ python3 examples/run_phase1_demo.py
-- YoY: Revenue --
  Q1 FY2024: $1,050,000  (vs Q1 FY2023: $1,000,000, +5.00%)
  Q2 FY2024: $1,250,000  (vs Q2 FY2023: $1,100,000, +13.64%)
  Q3 FY2024: $1,300,000  (vs Q3 FY2023: $1,150,000, +13.04%)
  Q4 FY2024: $1,790,000  (vs Q4 FY2023: $1,300,000, +37.69%)   <- derived, not directly tagged
```

All seven demo scripts run cleanly end-to-end against their synthetic fixtures. `run_output_demo.py` is the one worth reading in full — it's the first demo that chains every phase into one real memo instead of exercising one stage in isolation:

```
$ python3 examples/run_output_demo.py
# Filing Research Memo — SYNTHETIC TEST CO (0000320193)

**Data basis:** SYNTHETIC TEST FIXTURE — not a real company. This memo demonstrates the full
pipeline running end-to-end; every number and flag below is real output from real code, but
the underlying financial data is fabricated for testing. See PROGRESS.md before treating any
of this as representative of real filings.
**Rubric version:** v1

> This is a decision-support summary, not an investment recommendation. It does not and will
not suggest a buy/sell/hold action (PRD Section 2).

## Executive Summary
- 2 item(s) flagged for review this period.
- 1 figure(s) below are derived (e.g. Q4 = FY - Q1 - Q2 - Q3), not directly reported by the filer.

## Key Metric Changes

| Metric | Period | Comparison | Value | Change | Source |
|---|---|---|---|---|---|
| revenue | Q1 FY2024 | vs Q1 FY2023 (YoY) | $1,050,000 | +5.00% | XBRL |
| revenue | Q2 FY2024 | vs Q2 FY2023 (YoY) | $1,250,000 | +13.64% | XBRL |
| revenue | Q3 FY2024 | vs Q3 FY2023 (YoY) | $1,300,000 | +13.04% | XBRL |
| revenue | Q4 FY2024 | vs Q4 FY2023 (YoY) | $1,790,000 | +37.69% | Derived |

## Flagged Items

**[METRIC_CHANGE_THRESHOLD]** revenue changed +37.69% (YoY): $1,300,000 -> $1,790,000
- Cites: revenue: Q4 FY2024 vs Q4 FY2023
- For human review: yes — every Judgment agent flag is a screen, not a verdict (PRD Section 5).

**[LITIGATION_LANGUAGE]** There have been no material changes to the risk factors previously
disclosed... a former supplier filed a complaint...
- Cites: Item 1A. Risk Factors (prose-6)
- For human review: yes — every Judgment agent flag is a screen, not a verdict (PRD Section 5).
```

Full untruncated output (and the same memo as a standalone file) is at `examples/sample_memo_output.md` after running the script.

```
$ python3 examples/run_trigger_demo.py
Watchlist: Semiconductors (7 companies)

-- First poll --
  21 new filing(s) detected
    0001045810  10-Q   2024-10-20  0001045810-24-000090
    0001045810  8-K    2024-09-05  0001045810-24-000080
    0001045810  10-K   2024-02-15  0001045810-24-000060
    0000002488  10-Q   2024-10-20  0000002488-24-000090
    0000002488  8-K    2024-09-05  0000002488-24-000080
    ... and 16 more

-- Second poll (same underlying data) --
  0 new filing(s) detected  <- correctly empty, already seen
```

```
$ python3 examples/run_judgment_demo.py
Rubric version: v1

-- Metric change flags (YoY revenue, default 20% threshold) --
  [METRIC_CHANGE_THRESHOLD] revenue changed +37.69% (YoY): $1,300,000 -> $1,790,000
      cites: revenue: Q4 FY2024 vs Q4 FY2023

-- Margin change flags (net margin, threshold lowered to 1pp for this demo) --
  [MARGIN_THRESHOLD] Margin moved -2.38pp: 10.00% -> 7.62%
      cites: net_income/revenue margin, Q1 FY2024 vs FY2023
  [MARGIN_THRESHOLD] Margin moved +1.09pp: 10.91% -> 12.00%
      cites: net_income/revenue margin, Q2 FY2024 vs FY2023

-- Restatement check (revenue) --
  (none)

-- Litigation-language check (HTML fixture, Risk Factors section) --
  [LITIGATION_LANGUAGE] There have been no material changes... a former supplier filed a complaint...
      cites: Item 1A. Risk Factors (prose-6)
```

```
$ python3 examples/run_watchlist_demo.py
Watchlist: Semiconductors (7 companies)
  NVDA   NVIDIA Corporation (CIK 0001045810)
  AMD    Advanced Micro Devices, Inc. (CIK 0000002488)
  INTC   Intel Corporation (CIK 0000050863)
  AVGO   Broadcom Inc. (CIK 0001730168)
  QCOM   QUALCOMM Incorporated (CIK 0000804328)
  MU     Micron Technology, Inc. (CIK 0000723125)
  TXN    Texas Instruments Incorporated (CIK 0000097476)

-- Coverage per company (target: ['revenue', 'net_income', 'total_debt']) --
   NOTE: every company below is using the SAME synthetic fixture as a stand-in --
   this demonstrates the orchestration loop, not real per-company coverage.

  NVDA  : 2/3 (66.7%) — missing ['total_debt']
  [...same pattern for all 7 -- all using the same stand-in fixture...]

Aggregate watchlist coverage rate: 66.7%  (PRD Section 4 target: >=90%)
```

```
$ python3 examples/run_gap_fill_demo.py
-- XBRL coverage before HTML fallback --
  2/3 resolved (66.7%)
  Missing: ['total_debt']

-- After HTML-table fallback --
  Filled from HTML: ['total_debt']
  Still missing (no HTML match either): []

-- All facts for Q2 2024, with provenance --
  revenue: $1,250,000  [source=xbrl]
  net_income: $150,000  [source=xbrl]
  total_debt: $2,100,000  [source=html_table_fallback]  <- HUMAN REVIEW (not XBRL)
```

```
$ python3 examples/run_phase1_demo.py
Company: SYNTHETIC TEST CO (CIK 0000320193)

-- YoY: Revenue --
  Q1 FY2024: $1,050,000  (vs Q1 FY2023: $1,000,000, +5.00%)
  Q2 FY2024: $1,250,000  (vs Q2 FY2023: $1,100,000, +13.64%)

-- QoQ: Revenue --
  Q2 FY2023 vs Q1 FY2023: +10.00%
  Q3 FY2023 vs Q2 FY2023: +4.55%
  Q4 FY2023 vs Q3 FY2023: +13.04%
  Q1 FY2024 vs Q4 FY2023: -19.23%
  Q2 FY2024 vs Q1 FY2024: +19.05%

-- XBRL coverage --
  2/3 target concepts resolved (66.7%)
  Missing: ['total_debt']

-- Peer comparison (Revenue, Q1 2024) --
  SYNTHETIC TEST CO: $1,050,000
  Peer Co B (SYNTHETIC): $1,050,000
```

```
$ python3 examples/run_phase2_demo.py
Parsed 8 blocks from the filing excerpt.
Chunked into 4 chunks (1 table, 3 prose — table always kept atomic).

-- Hybrid search: 'lawsuit litigation supplier breach of contract' --
  fused=0.0333  bm25=2.77  [prose] (Risk Factors chunk, containing the actual litigation paragraph)

-- HTML-table fallback extraction --
  revenue: $1,250,000 (source=html_table_fallback)
  gross_profit: $524,000 (source=html_table_fallback)
  net_income: $150,000 (source=html_table_fallback)
```

Re-run both yourself after `pip install -r requirements.txt` to confirm your environment matches — full untruncated output is longer than what's excerpted here.
