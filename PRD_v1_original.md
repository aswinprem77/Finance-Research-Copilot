# Product Requirements Document
## Financial Research Copilot — Agentic RAG over SEC Filings

**Owner:** [Your name]
**Status:** Draft v1
**Last updated:** July 2026

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
- Extract and structure key financial data (not just retrieve raw text)
- Perform numeric comparison (YoY, QoQ, peer-vs-peer)
- Flag materially notable changes vs. routine noise, using an explicit rubric
- Generate a structured, source-cited memo as the final deliverable
- Prove system reliability via measured evaluation (retrieval precision, faithfulness/hallucination rate)

**Non-Goals**
- No investment recommendations or predictive price modeling
- No real-time intraday data (filings-only, not market data feeds)
- No support for non-US filing formats in v1 (SEC EDGAR only)
- No fine-tuning of a custom LLM — v1 uses prompting + retrieval only

---

## 4. Success Metrics

| Metric | Target |
|---|---|
| Retrieval precision (top-k relevance, manual eval) | ≥85% on a 30-query benchmark set |
| Answer faithfulness (claims traceable to source, via Ragas/DeepEval) | ≥90% |
| Numeric accuracy (calculator agent vs. manually verified figures) | 100% (deterministic, not LLM-guessed) |
| End-to-end pipeline latency (new filing → memo) | <10 minutes |
| Manual-review time saved (self-benchmarked: manual vs. system on same filing) | ≥80% reduction |

---

## 5. System Architecture — Agent Pipeline

**Stage 1 — Trigger Agent**
Polls SEC EDGAR's filing index on a schedule for new filings from a defined watchlist (5-10 companies, single sector recommended for meaningful peer comparison). On detection, kicks off the pipeline for that filing.

**Stage 2 — Retriever Agent**
- Ingests filing, separates prose sections from financial tables (different parsing logic for each)
- Applies semantic chunking (not fixed-size) so numeric context isn't split from its labels
- Indexes into Qdrant (dense) + BM25 (sparse) for hybrid retrieval
- Cross-encoder reranks top candidates before passing downstream

**Stage 3 — Analyst Agent (Financial Calculator)**
- Extracts structured figures (revenue, net income, debt, margins, etc.) from tables into a clean schema
- Computes deterministic comparisons in Python (not LLM arithmetic): YoY, QoQ, vs. peer set
- Outputs a structured data object, not prose — this is the ground truth the rest of the pipeline builds on

**Stage 4 — Judgment Agent**
- Applies an explicit, documented rubric to classify findings as notable vs. routine, e.g.:
  - Margin change beyond a defined threshold
  - New litigation or regulatory language detected
  - Debt covenant or liquidity language changes
  - Material restatement of prior-period figures
- Every flag must cite the specific source passage and the rubric rule that triggered it (no unexplained "AI thinks this is important")

**Stage 5 — Output Agent**
- Generates the final memo: executive summary, key metric changes (table), flagged items with citations, explicit "for human review" framing on judgment calls
- Delivery: Markdown/PDF output initially; optional Slack/email webhook as a stretch goal

**Orchestration:** Simple state machine (LangGraph or hand-rolled) coordinating the five stages; avoid over-engineering the orchestration layer in v1.

---

## 6. Data Sources

- **SEC EDGAR full-text search & filing API** (free, public) — 10-K, 10-Q, 8-K, earnings call transcripts where available
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

| Risk | Mitigation |
|---|---|
| Table parsing fails on inconsistent filing formats | Build a small manual-correction fallback; document known failure cases explicitly rather than hiding them |
| LLM hallucinates numbers | All numeric outputs come from the deterministic Calculator agent, never from LLM free generation |
| Judgment agent flags too much noise or misses real signals | Rubric is versioned and testable; track false-positive/false-negative rate against a manually labeled sample set |
| Scope creep (trying to build all 5 agents at once) | Phase-gated build plan (see roadmap); ship Stage 2+3 end-to-end before adding 1, 4, 5 |

---

## 9. Rollout / Build Phases

1. **Weeks 1-3:** Core retrieval pipeline (ingestion, chunking, hybrid search, reranking)
2. **Weeks 4-5:** Analyst agent — structured extraction + deterministic calculations
3. **Weeks 6-7:** Judgment agent (rubric-based flagging) + Trigger agent (scheduled polling)
4. **Weeks 8-9:** Output agent + full pipeline orchestration
5. **Weeks 10-12:** Dockerized deployment, evaluation harness, benchmark write-up, demo

---

## 10. Open Questions

- Which sector/watchlist companies to lock in for consistent peer comparison?
- Delivery channel for v1 output — Markdown file, simple web UI, or Slack webhook?
- How to source a "ground truth" labeled dataset for judgment agent evaluation (likely manual labeling of a small sample)?

---

## 11. Explicitly Out of Scope for v1 (possible v2 ideas)
- Multi-sector / cross-industry comparison
- Real-time market data integration
- Custom fine-tuned model
- Multi-user/team collaboration features
