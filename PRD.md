# Product Requirements Document
## Financial Research Copilot — Agentic RAG over SEC Filings

**Owner:** [Your name]
**Status:** Draft v2
**Last updated:** July 2026
**Changelog from v1:** Split ingestion into structured (XBRL) vs. unstructured (HTML/prose) paths instead of routing everything through table-parsing + RAG. See Section 5 and Section 8 for what changed and why.

---

## 1. Problem Statement

Equity/credit research analysts spend hours manually reading SEC filings (10-K, 10-Q, earnings transcripts) to extract financial trends, compare against prior periods and peers, and identify material risks buried in dense text and footnotes. This process is:
- **Slow** — a single filing review can take 1-3 hours per company
- **Error-prone** — manual number-pulling and comparison invites mistakes
- **Reactive** — analysts often only review filings when prompted, missing timely signals

**Goal:** Build a system that automates the end-to-end research workflow — from filing detection through analysis to a delivered, source-cited memo — cutting analyst time on routine filing review from ~2 hours to under 10 minutes, while remaining honest about what it can and can't judge.

---

## 2. Target User & Use Case

**Primary persona:** A junior equity/credit research analyst (or, for portfolio purposes, a simulated version of this role) who tracks a watchlist of companies and needs to stay current on new filings without manually reading every document.

**Primary use case:** "Alert me when something material changes in a filing from my watchlist, and give me a structured, source-cited summary I can act on or dig deeper into."

**Explicitly out of scope:** This is a decision-support tool, not an investment-decision tool. It does not recommend buy/sell/hold actions.

---

## 3. Goals & Non-Goals

**Goals**
- Automatically ingest new SEC filings for a defined watchlist
- Extract structured financial data primarily from **XBRL**, not by parsing rendered tables
- Retrieve and reason over unstructured prose (MD&A, risk factors, notes) via hybrid RAG
- Perform numeric comparison (YoY, QoQ, peer-vs-peer) deterministically
- Flag materially notable changes vs. routine noise, using an explicit rubric
- Generate a structured, source-cited memo as the final deliverable
- Prove system reliability via measured evaluation (retrieval precision, faithfulness/hallucination rate)

**Non-Goals**
- No investment recommendations or predictive price modeling
- No real-time intraday data (filings-only, not market data feeds)
- No support for non-US filing formats in v1 (SEC EDGAR only)
- No fine-tuning of a custom LLM — v1 uses prompting + retrieval only
- No vision/image pipeline in v1 — charts/graphics in filings are low-volume and not where materiality signal lives; explicitly deferred (see Section 11)

---

## 4. Success Metrics

| Metric | Target |
|---|---|
| Retrieval precision (top-k relevance, manual eval, unstructured path only) | ≥85% on a 30-query benchmark set |
| Answer faithfulness (claims traceable to source, via Ragas/DeepEval) | ≥90% |
| Numeric accuracy (XBRL-sourced figures vs. manually verified figures) | 100% (deterministic, not LLM-guessed) |
| XBRL coverage rate (% of required line items resolved without HTML-table fallback) | ≥90% across watchlist |
| End-to-end pipeline latency (new filing → memo) | <10 minutes |
| Manual-review time saved (self-benchmarked: manual vs. system on same filing) | ≥80% reduction |

---

## 5. System Architecture — Agent Pipeline

The core change from v1: **filings are not a single content type routed through one pipeline.** A 10-K/10-Q is really two content classes — machine-tagged structured data (XBRL) and unstructured narrative/HTML (prose, embedded tables) — and each needs a different extraction strategy. Everything downstream (Calculator, Judgment, Output) consumes both into one shared schema, but they don't take the same path in.

### Stage 1 — Trigger Agent
Polls SEC EDGAR's filing index on a schedule for new filings from a defined watchlist (5-10 companies, single sector recommended for meaningful peer comparison). On detection, kicks off the pipeline for that filing and fetches both:
- the filing's XBRL facts (via `data.sec.gov/api/xbrl/companyfacts/{CIK}.json`, or the filing-specific XBRL instance document)
- the filing's HTML document(s)

### Stage 2 — Ingestion Layer (split into two paths)

**Path A — Structured (XBRL), primary numeric source**
- Pulls tagged financial facts directly from SEC's XBRL API — revenue, net income, debt, margins, etc. — using the standardized US-GAAP taxonomy (e.g. `us-gaap:Revenues`, `us-gaap:NetIncomeLoss`)
- No parsing, no LLM involved, no chunking — this is a structured API call, not a retrieval problem
- Normalizes tags into the shared financial schema (Stage 3 input), with fiscal period and unit metadata preserved
- Logs any required line item that XBRL doesn't cover for a given filer, so the fallback path knows what it needs to fill in

**Path B — Unstructured (HTML prose + embedded tables), retrieval source**
- Separates prose sections (MD&A, risk factors, legal proceedings, notes) from HTML tables embedded in the filing body
- Applies semantic chunking (not fixed-size) so numeric context in embedded tables isn't split from its labels
- Indexes into Qdrant (dense) + BM25 (sparse) for hybrid retrieval
- Cross-encoder reranks top candidates before passing downstream
- HTML-table extraction here is a **fallback only** — used when Path A has no XBRL tag for a needed figure, or when the item in question is qualitative (e.g. debt covenant language) rather than numeric
- Images/charts embedded in the filing: not processed in v1 (out of scope, see Section 11). If a chart is materially relevant, this shows up as a documented known-gap, not a silent miss.

### Stage 3 — Analyst Agent (Financial Calculator)
- Consumes the shared schema populated primarily by Path A (XBRL), with Path B filling any gaps flagged during ingestion
- Every figure in the schema carries a `source: xbrl | html_table_fallback` tag — this makes it explicit downstream (and in the final memo) which numbers are fully deterministic vs. extracted with more risk
- Computes deterministic comparisons in Python (not LLM arithmetic): YoY, QoQ, vs. peer set
- Outputs a structured data object, not prose — this is the ground truth the rest of the pipeline builds on

### Stage 4 — Judgment Agent
- Applies an explicit, documented rubric to classify findings as notable vs. routine, e.g.:
  - Margin change beyond a defined threshold (from Stage 3's structured schema)
  - New litigation or regulatory language detected (from Path B retrieval)
  - Debt covenant or liquidity language changes (from Path B retrieval)
  - Material restatement of prior-period figures (from Path A, comparing current XBRL facts to previously filed values for the same period)
- Every flag must cite the specific source passage (Path B) or XBRL fact (Path A) and the rubric rule that triggered it — no unexplained "AI thinks this is important"

### Stage 5 — Output Agent
- Generates the final memo: executive summary, key metric changes (table, with source tag per figure), flagged items with citations, explicit "for human review" framing on judgment calls and on any `html_table_fallback`-sourced numbers
- Delivery: Markdown/PDF output initially; optional Slack/email webhook as a stretch goal

**Orchestration:** Simple state machine (LangGraph or hand-rolled) coordinating the five stages; Path A and Path B in Stage 2 run in parallel, both feeding Stage 3. Avoid over-engineering the orchestration layer in v1.

---

## 6. Data Sources

- **SEC EDGAR XBRL API** (`data.sec.gov/api/xbrl/...`) — primary source for structured financial facts, free, public
- **SEC EDGAR full-text search & filing API** — 10-K, 10-Q, 8-K, earnings call transcripts where available, for unstructured prose/table retrieval
- Watchlist: 5-10 companies in a single sector (recommend picking one you can reason about, to make peer comparisons meaningful)

---

## 7. Tech Stack

| Layer | Choice |
|---|---|
| Vector DB | Qdrant |
| Structured/metadata store | PostgreSQL |
| Cache/queue | Redis |
| API | FastAPI |
| Orchestration | LangGraph (or simple custom state machine) |
| LLM | Azure OpenAI (aligns with existing cert track) |
| Evaluation | Ragas, DeepEval |
| Observability | LangSmith |
| Containerization | Docker / Docker Compose |
| CI/CD | GitHub Actions |
| Deployment | Azure |

---

## 8. Key Risks & Mitigations

| Risk | Mitigation | Status vs. v1 |
|---|---|---|
| LLM/table-parser hallucinates numbers | XBRL is now the primary numeric source (deterministic API pull, no parsing). HTML-table extraction only used as a tagged fallback, flagged for human review in the memo. | **Severity reduced** — was top risk in v1, now scoped down |
| XBRL tag coverage gaps (some line items not tagged, or tagged inconsistently across filers) | Ingestion logs any unresolved required item; falls through to Path B fallback with explicit source tagging; track coverage rate as a metric (Section 4) | New risk surfaced by the v2 split |
| HTML-table fallback fails on inconsistent filing formats | Build a small manual-correction fallback; document known failure cases explicitly rather than hiding them; always tagged `html_table_fallback` in output | Carried over from v1, scope reduced |
| Judgment agent flags too much noise or misses real signals | Rubric is versioned and testable; track false-positive/false-negative rate against a manually labeled sample set | Unchanged |
| Scope creep (trying to build all agents/paths at once) | Phase-gated build plan (see roadmap); ship Path A (XBRL) + Calculator end-to-end before adding Path B retrieval, then Judgment/Output | Updated to reflect XBRL-first sequencing |

---

## 9. Rollout / Build Phases

1. **Weeks 1-2:** XBRL ingestion (Path A) + Analyst/Calculator agent on structured data only — get deterministic YoY/QoQ/peer comparisons working end-to-end on the easy path first
2. **Weeks 3-5:** Unstructured retrieval pipeline (Path B) — ingestion, semantic chunking, hybrid search, reranking, HTML-table fallback with source tagging
3. **Weeks 6-7:** Judgment agent (rubric-based flagging, consuming both paths) + Trigger agent (scheduled polling)
4. **Weeks 8-9:** Output agent + full pipeline orchestration (Path A/B parallel execution)
5. **Weeks 10-12:** Dockerized deployment, evaluation harness (incl. XBRL coverage rate metric), benchmark write-up, demo

---

## 10. Open Questions

- Which sector/watchlist companies to lock in for consistent peer comparison?
- Delivery channel for v1 output — Markdown file, simple web UI, or Slack webhook?
- How to source a "ground truth" labeled dataset for judgment agent evaluation (likely manual labeling of a small sample)?
- What's the acceptable XBRL coverage threshold before a filer/sector is considered too inconsistent for reliable Path A extraction?

---

## 11. Explicitly Out of Scope for v1 (possible v2 ideas)
- Multi-sector / cross-industry comparison
- Real-time market data integration
- Custom fine-tuned model
- Multi-user/team collaboration features
- Vision/image pipeline for charts and graphics embedded in filings
