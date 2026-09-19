# Evaluation benchmarks

Run the offline baseline:

```bash
python -m src.evaluation
```

The committed synthetic benchmark tests evaluator mechanics. It cannot certify the PRD targets.

A certification benchmark must use `scope: "real_labeled"`, cover all seven watchlist companies, contain at least 30 manually labeled retrieval queries, and record manually verified financial values from the cited filings. Generated outputs belong under `data/evaluation/` and are ignored by Git.

The deterministic citation-support proxy checks that labeled evidence exists in cited chunks. It is deliberately reported separately from answer faithfulness. Add Ragas or DeepEval only after the real query/answer/source set has been manually reviewed.

Prepare a real filing review queue with:

```bash
python -m src.evaluation.prepare --filings-per-company 2
```

This downloads the latest periodic filings for the configured watchlist into ignored runtime storage and creates `data/evaluation/real-draft/review_queue.json`. System values and retrieval results are only proposals. A reviewer must verify values from the linked SEC filing and label every retrieval result before the cases can become benchmark ground truth.

The same directory contains `numeric_review.csv`, `retrieval_review.csv`, and `filing_summary.csv` for spreadsheet review. Re-export them without downloading data again with `python -m src.evaluation.prepare --export-existing`.

After every numeric row has `verified=true` plus an independently checked `verified_value`, and every retrieval result has `relevant=true` or `false`, compile the benchmark:

```bash
python -m src.evaluation.compile
python -m src.evaluation --benchmark data/evaluation/real-watchlist-v1.json
```

Compilation refuses incomplete labels, queries without a relevant source chunk, or a PRD benchmark with fewer than five companies or 30 queries. HTML fallback values stay in the review sheet but are excluded from the XBRL numeric-accuracy metric.
