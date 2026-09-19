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
4. Retrieve narrative evidence with BM25 + local Qdrant + TF-IDF, then lexical reranking. Apply explicit numeric and language rules.
5. Save a Markdown memo with current/prior sources, filing links, unresolved metrics, and human-review labels; then persist completion.

8-K memos screen narrative only. They do not relabel periodic companyfacts as 8-K financial results.

## Validation and limits

The September 2026 audit passes 123 offline tests and ran a live NVIDIA 10-Q through memo generation. A seven-company real review draft now contains 35 retrieval queries and 29 unique numeric checks. Draft aggregate XBRL coverage is 82.9%; the figures and relevance labels still require manual verification, so this is not yet a certified benchmark.

Semantic embeddings, a cross-encoder, prior-filing language change detection, automated peer tables, a labeled evaluation benchmark, product API/UI, PostgreSQL/Redis, PDF export, and Azure deployment remain open. Fiscal mapping supports regular quarterly/annual calendars; transition fiscal years need review. HTML fallback supports flat, explicitly dated English/ISO headers and table-local scale labels; complex spans remain gaps.

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
- `src/retrieval/`: HTML blocks, chunks, fallback, hybrid search and reranking
- `src/analyst/calculator.py`: deterministic comparisons and peer comparison utility
- `src/judgment/rubric.py`: versioned screening rules
- `src/output/memo.py`: memo tables, citations and review notes
- `src/evaluation/review_app.py`: local human-labeling interface and guarded benchmark compilation
- `tests/`: offline unit and integration tests
