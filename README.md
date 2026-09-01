# Financial Research Copilot

Agentic RAG system over SEC filings. See `PRD.md` for full spec, `PROGRESS.md` for exactly
where the build stands right now — **read PROGRESS.md first in any new session.**

## Quickstart

```bash
python3 -m venv venv && source venv/bin/activate      # or your usual env setup
pip install -r requirements.txt
cp .env.example .env                                   # fill in SEC_USER_AGENT
pytest tests/ -v                                        # offline, no network needed
```

## Layout

```
src/
  schema/financial_schema.py   # shared data model, both ingestion paths write into this
  ingestion/
    xbrl_client.py             # Path A: SEC XBRL companyfacts API client
    xbrl_parser.py             # Path A: raw JSON -> FinancialFact objects
    coverage.py                 # XBRL coverage-rate metric (PRD v2 Section 4)
  analyst/
    calculator.py              # Stage 3: deterministic YoY/QoQ/margin/peer math, pure Python
  retrieval/                    # Path B (Phase 2): unstructured HTML retrieval
    html_ingest.py              # parse filing HTML -> ProseBlock/TableBlock
    chunking.py                  # structure-aware chunking (tables always atomic)
    table_fallback.py            # TableBlock -> FinancialFact, source=html_table_fallback
    embeddings.py                 # EmbeddingProvider interface (TF-IDF stand-in + Azure stub)
    hybrid_index.py               # BM25 + Qdrant (in-memory), merged via RRF
    rerank.py                      # reranking stage (lexical-overlap stand-in)
  pipeline/
    stage2_gap_fill.py           # wires Path A gaps -> Path B fallback, merges into one schema
    watchlist.py                  # loads config/watchlist.json, orchestrates coverage across it
  judgment/
    rubric.py                     # Stage 4: rubric-based flagging, every flag cites rule + source
  trigger/
    edgar_client.py                # Stage 1: SEC submissions polling + FilingEvent parsing
    state_store.py                  # persisted seen-accessions, so polling doesn't re-fire
    trigger_agent.py                 # orchestrates polling across the watchlist
  output/
    memo.py                        # Stage 5: generates the memo — required provenance note,
                                    # structurally cannot produce a buy/sell/hold recommendation
config/
  watchlist.json                # locked-in watchlist: 7 semiconductor companies, real CIKs
tests/                          # all offline, no network needed
  fixtures/
    sample_companyfacts.json    # synthetic XBRL data
    sample_filing_excerpt.html  # synthetic 10-Q excerpt
    sample_submissions.json      # synthetic SEC submissions data
examples/
  run_phase1_demo.py           # end-to-end Path A demo
  run_phase2_demo.py           # end-to-end Path B demo
  run_gap_fill_demo.py          # end-to-end Path A + Path B wired together
  run_watchlist_demo.py          # coverage checking across the whole watchlist
  run_judgment_demo.py            # rubric engine: all 4 rules with citations
  run_trigger_demo.py              # polling: first poll fires, second poll is silent
  run_output_demo.py                # FULL pipeline, one real memo (writes sample_memo_output.md)
data/
  raw/                         # cache dir for XBRL API responses (gitignored)
  processed/                   # (not used yet)
  trigger_state.json           # persisted seen-accessions (gitignored, created on first real run)
```

## Status

Phases 1-3 are done (XBRL + Calculator, Path B retrieval wired to Path A, watchlist locked,
Judgment agent, Trigger agent). **Phase 4's Output agent is built**: generates the memo
(executive summary, metric table with per-row data provenance, cited flags), and is
structurally incapable of a buy/sell/hold recommendation — there's no field for one and no
LLM in the module to generate free text. **Full pipeline orchestration (wiring Trigger's
new-filing events through Ingestion → Calculator → Judgment → Output automatically) is NOT
built yet** — `run_output_demo.py` calls each stage manually in sequence; that's the real
next step. 84 tests passing offline. Neither the live SEC API nor a real embedding/reranker
model has been exercised in this dev sandbox, and the rubric's threshold VALUES are
uncalibrated defaults pending real filing data. Full detail in `PROGRESS.md`.

## Try Phase 1 against a real filing (run locally, not in this sandbox)

```python
from src.ingestion.xbrl_client import SecXbrlClient
from src.ingestion.xbrl_parser import parse_company_facts
from src.analyst.calculator import compute_yoy
from src.schema.financial_schema import FinancialConcept

client = SecXbrlClient(user_agent="Your Name your.email@example.com", cache_dir="data/raw/xbrl_cache")
raw = client.get_company_facts(cik="320193")  # Apple's CIK, as an example
financials, missing = parse_company_facts(raw, cik="320193")
print("Missing tags:", missing)

results = compute_yoy(financials, FinancialConcept.REVENUE)
for r in results[-4:]:
    print(r)
```

## Try Phase 2 (Path B) against the synthetic fixture

```bash
python3 examples/run_phase2_demo.py
```

## Try Path A + Path B wired together

```bash
python3 examples/run_gap_fill_demo.py
```

Shows an XBRL coverage gap (missing `total_debt`) getting automatically filled from the
filing's HTML table, tagged `html_table_fallback` for downstream human-review flagging —
while the XBRL-resolved concepts (`revenue`, `net_income`) are left untouched.

## Try the watchlist

```bash
python3 examples/run_watchlist_demo.py
```

Loads `config/watchlist.json` (7 real semiconductor companies) and runs coverage checking
across all of them. In this sandbox it uses the same synthetic fixture as a stand-in for
every company — swap in `SecXbrlClient.get_company_facts` as the fetch function once you're
somewhere with real network access; `run_watchlist_coverage()` itself needs no changes.

## Try the Judgment agent

```bash
python3 examples/run_judgment_demo.py
```

Runs all 4 rubric rules (metric-change threshold, margin-change threshold, restatement
detection, litigation-language keywords) against the synthetic fixtures. Every flag prints
its `rule_id` and a citation back to the specific fact or passage that triggered it.
**The threshold values are reasonable defaults, not calibrated against real filings** — see
`src/judgment/rubric.py`'s module docstring and `PROGRESS.md` before treating flagged output
as meaningful for a real company.

## Try the Trigger agent

```bash
python3 examples/run_trigger_demo.py
```

Polls the watchlist twice against the same synthetic submissions data. First poll returns
every relevant filing (10-K/10-Q/8-K only — other form types are filtered out); second poll
returns nothing, since the state is already persisted. Swap in
`trigger.edgar_client.fetch_submissions` as the fetch function for real polling once you're
somewhere with real network access; `poll_watchlist_for_new_filings()` needs no changes.

## Try the full pipeline (the one worth running first)

```bash
python3 examples/run_output_demo.py
```

This is the only demo that chains every phase together: XBRL parse → coverage check → HTML
fallback fill → deterministic YoY comparison → rubric flagging → memo. Writes a real memo to
`examples/sample_memo_output.md`. Every number and flag in it is real output from real code —
the underlying financial data is the synthetic fixture, clearly stated in the memo's own
required `data_provenance_note`, not hidden in a README a reader might not see.

**What this demo does NOT do yet:** run automatically when the Trigger agent detects a new
filing. That wiring (Trigger → Ingestion → Calculator → Judgment → Output as one flow) is
the actual next piece of work — see `PROGRESS.md`.

Swap `TfidfEmbeddingProvider` for a real embedding model once you have one available (see
`src/retrieval/embeddings.py` and `PROGRESS.md` for what's a stand-in vs. production-ready).
