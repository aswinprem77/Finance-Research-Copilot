# Financial Research Copilot

SEC filing research with deterministic XBRL comparisons, hybrid retrieval, rule-based screening, and cited Markdown memos. [PRD.md](PRD.md) is the product specification; [PRD_AUDIT.md](PRD_AUDIT.md) records implemented fixes and remaining gaps. Read [PROGRESS.md](PROGRESS.md) when resuming development.

## Run on Windows

Requires Python 3.11 or newer.

```powershell
cd financial-research-copilot
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env  # only for a new setup; preserve an existing .env
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m src.pipeline --demo
```

If already inside the directory containing requirements.txt, omit the first command.
On Linux/macOS use `python -m venv venv` and `venv/bin/python` instead.

The demo uses synthetic fixtures and writes to `data/demo/memos/`. A second run skips completed accessions. To repeat in a separate workspace, supply `--state-path data/demo/replay.json --output-dir data/demo/replay`.

## Live polling

Set `SEC_USER_AGENT` in `.env` to your name/application and contact email. Existing credentials are never required for the offline demo. The live command fetches fresh companyfacts, submissions, and the primary HTML from SEC.

```powershell
.\venv\Scripts\python.exe -m src.pipeline --once
.\venv\Scripts\python.exe -m src.pipeline --once --since 2026-08-01
.\venv\Scripts\python.exe -m src.pipeline --interval 900
```

The default starting date is seven days before launch. The scheduled process keeps that starting date so outages can be retried. Stop with Ctrl+C. Configure companies with `--watchlist path/to/watchlist.json`.

Live memos and completion state are under `data/live/`, separate from the demo. One process must own each state file. A failed fetch, analysis, or memo write leaves the accession unacknowledged for the next poll. One failing company does not block other companies. Submissions pagination/backfill beyond the recent-filings endpoint is not implemented.

## What runs

1. Detect unseen 10-K, 10-Q, 8-K and amended filings.
2. Fetch and parse XBRL and HTML concurrently. Filter facts by the event's filing date.
3. Calculate current-period YoY/QoQ and margin changes. Attempt HTML fallback only for unresolved figures.
4. Retrieve narrative evidence with BM25 + local Qdrant over the configured retrieval stack, then rerank. Apply explicit numeric and language rules.
5. Align peers by period end date and build the peer table.
6. Screen every prose passage for litigation and liquidity/regulatory language, then classify each against the company's previous filing as new, revised, unchanged or unestablished.
7. Save a Markdown memo with current/prior sources, filing links, unresolved metrics, and human-review labels; then persist the narrative baseline and completion.

8-K memos screen narrative only. They do not relabel periodic companyfacts as 8-K financial results.

## Peer comparison

PRD Stage 3 asks for comparison "vs. peer set". Matching peers on `(fiscal_year, fiscal_period)` does not work across this watchlist: for NVIDIA's Q2 FY2027 it returns a value for NVIDIA and `None` for all six peers, because no peer labels that period the same way. Alignment is therefore by actual **period end date**, within 45 days — half a quarter, so the nearest candidate is never ambiguous.

On NVIDIA's 10-Q for the quarter ended 2026-07-26, four of six peers align: TXN (-26d), QCOM (-28d), AMD (-29d), INTC (-29d). Every row shows its own fiscal label, period end and day offset, so the alignment is visible rather than assumed.

Peers that drop out are listed with the reason and their nearest available period, because the two ways it happens mean different things. Broadcom's quarter ended 2026-08-02, only 7 days away, but was filed 2026-09-10 — after NVIDIA's filing, so nobody reading it could have seen those figures, and the as-of snapshot excludes them. Micron simply has no quarter near that date.

Quarters never align against annual periods. Peer facts are snapshotted to the subject's filing date, a failed peer fetch is named in the table rather than dropped, and companyfacts are fetched once per poll cycle. Disable with `peer_comparisons=False`. No seasonality adjustment is applied, and figures are each company's own reported period.

## Narrative change detection

PRD Stage 4 asks for *new* litigation language and *changes* in covenant or liquidity language. Keyword screening alone cannot support either claim, so each screened passage is compared against the same company's previous filing:

| Status | Meaning | Severity |
|---|---|---|
| `new` | No close counterpart in the prior filing | notable |
| `revised` | A counterpart exists, but the wording changed; the flag cites the edit | notable |
| `unchanged` | The prior filing says the same thing once dates are rolled forward | routine |
| `unestablished` | No prior filing is on record, so novelty cannot be claimed either way | notable |

Baselines live under `data/<demo|live>/narrative/<cik>/<accession>.json`, written only after the memo is durable, and overridable with `--narrative-dir`. A comparison only ever reads filings made strictly before the one being processed, so reprocessing an old filing cannot see a later one.

Comparison is per sentence: a passage is scored by the fraction of its sentences that also appear in the prior filing. Chunk boundaries follow a size budget, so one edit upstream shifts every later boundary; scoring whole passages read unchanged boilerplate as new (measured on two consecutive NVIDIA 10-Qs: 16 of 16 passages new or revised, none unchanged; sentence coverage gives 3 unchanged, 7 revised, 6 new on the same pair).

Similarity is `difflib`, not embedding cosine: filing prose is copy-pasted forward and then edited, a sequence matcher yields the actual sentences that changed, and the judgment path stays independent of the retrieval stack. A passage genuinely reworded to mean the same thing therefore reads as `new` — a known false positive. Dates are normalized before comparison; monetary amounts and ratios are not.

Screening covers every prose chunk, not just retrieved ones, so a disclosure outside the top-k is still seen. Retrieval only orders passages within a status, deciding which survive the per-topic cap.

**These thresholds are uncalibrated.** No labeled sample of real filing-to-filing edits has been measured, so false-positive and false-negative rates are unknown.

## Retrieval stack

Path B runs on one of two stacks, chosen by `RETRIEVAL_PROFILE` in `.env`, or per command with `--retrieval-profile`.

| Profile | Dense vectors | Rerank | Model download |
|---|---|---|---|
| `semantic` (default) | `BAAI/bge-small-en-v1.5` bi-encoder | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Yes, about 220 MB on first use |
| `lexical` | TF-IDF | Query-term overlap | No |

Override the models with `EMBEDDING_MODEL` and `RERANKER_MODEL`. Queries and documents are encoded separately so instruction-tuned models get their query prefix; the cross-encoder scores each (query, passage) pair jointly over the fused top candidates and can overturn the fusion order.

The lexical profile matches shared words, not meaning. It exists so the test suite, CI and the offline demo run with no download, and the offline demo and synthetic benchmark default to it. Retrieval-quality numbers measured on it are not the system's real numbers.

Each memo names the stack that produced its narrative evidence. Every review queue and compiled benchmark records a stack fingerprint, and the evaluation harness refuses to score labels against a stack that did not retrieve them, because changing the stack changes which passages reach the top-k.

## Validation and limits

The September 2026 audit passes 227 offline tests and ran a live NVIDIA 10-Q through memo generation. A seven-company real review draft now contains 35 retrieval queries and 29 unique numeric checks. Draft aggregate XBRL coverage is 82.9%; the figures and relevance labels still require manual verification, so this is not yet a certified benchmark.

A labeled evaluation benchmark, product API/UI, PostgreSQL/Redis, PDF export, and Azure deployment remain open. Fiscal mapping supports regular quarterly/annual calendars; transition fiscal years need review. HTML fallback supports flat, explicitly dated English/ISO headers and table-local scale labels; complex spans remain gaps.

## Evaluation baseline

Run the versioned offline benchmark and write JSON/Markdown reports to `data/evaluation/`:

```powershell
.\venv\Scripts\python.exe -m src.evaluation
```

The committed synthetic baseline measures numeric extraction, XBRL coverage, retrieval precision, a deterministic citation-support proxy, and pipeline latency. It deliberately leaves answer faithfulness and manual time saved unmeasured. See `benchmarks/README.md` for the requirements of the real, manually labeled benchmark.

Create the real manual-review queue with:

```powershell
.\venv\Scripts\python.exe -m src.evaluation.prepare --filings-per-company 2
```

The queue links every candidate to its SEC filing and leaves verification/relevance fields unset. Review those fields before promoting any case to benchmark ground truth.

Review the prepared queue in the local interface:

```powershell
.\venv\Scripts\python.exe -m src.evaluation.review_app
```

Open `http://127.0.0.1:8765`. The interface shows filing-level progress, opens the original SEC filing, saves each numeric verification and passage label directly to the review CSVs, and enables compilation only after every label is complete and each query has relevant evidence. Stop the server with Ctrl+C.

## Container and CI

```sh
docker build -t financial-research-copilot .
docker run --rm financial-research-copilot --demo
docker run --rm --env-file .env -v copilot-data:/app/data financial-research-copilot --interval 900
```

The Dockerfile runs as a non-root user and excludes local secrets. The image build, persistent-volume demo, duplicate suppression, and a live no-new-filings SEC poll were verified locally on Docker Engine 29.4.3. GitHub Actions installs dependencies, runs the offline tests, and runs the synthetic demo; the hosted workflow passed on commit `b49aa76`.

## Code map

- `src/pipeline/runner.py`: complete filing workflow and success-only acknowledgment
- `src/pipeline/__main__.py`: demo, one-shot, and scheduled CLI
- `src/ingestion/`: SEC client, XBRL normalization and coverage
- `src/retrieval/`: HTML blocks, chunks, fallback, hybrid search, reranking and stack selection
- `src/analyst/calculator.py`: deterministic comparisons and peer comparison utility
- `src/analyst/peers.py`: peer alignment by period end date, with the gaps named
- `src/judgment/rubric.py`: versioned screening rules
- `src/judgment/narrative.py`, `narrative_store.py`: prior-filing language comparison and its durable baselines
- `src/output/memo.py`: memo tables, citations and review notes
- `src/evaluation/review_app.py`: local human-labeling interface and guarded benchmark compilation
- `tests/`: offline unit and integration tests
