from __future__ import annotations

import json
import time
from pathlib import Path

from src.evaluation.models import Benchmark, EvaluationReport, MetricResult
from src.ingestion.coverage import compute_coverage, watchlist_coverage_rate
from src.ingestion.xbrl_parser import parse_company_facts
from src.pipeline.runner import process_filing
from src.retrieval.chunking import chunk_blocks
from src.retrieval.embeddings import TfidfEmbeddingProvider
from src.retrieval.html_ingest import parse_filing_html
from src.retrieval.hybrid_index import HybridIndex
from src.retrieval.rerank import rerank_lexical_overlap
from src.trigger.edgar_client import FilingEvent
from src.schema.financial_schema import FinancialConcept, FiscalPeriod


def load_benchmark(path: Path | str) -> Benchmark:
    path = Path(path)
    return Benchmark.model_validate_json(path.read_text(encoding="utf-8"))


def _resolve(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Benchmark path escapes project root: {relative}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _metric(value, target, unit, passed, numerator, denominator, qualification):
    return MetricResult(value=value, target=target, unit=unit, passed=passed,
                        numerator=numerator, denominator=denominator, qualification=qualification)


def evaluate_benchmark(benchmark: Benchmark, project_root: Path | str) -> EvaluationReport:
    root = Path(project_root).resolve()
    failures: list[str] = []

    numeric_correct = 0
    numeric_total = 0
    coverage_reports = []
    for case in benchmark.numeric_cases:
        raw = json.loads(_resolve(root, case.companyfacts_path).read_text(encoding="utf-8"))
        financials, _ = parse_company_facts(raw, case.cik, case.required_concepts, as_of=case.filed_date)
        available = {
            fact.concept for fact in financials.facts
            if fact.accession_number == case.accession_number
            and fact.period_end_date == case.period_end_date
            and (
                (fact.fiscal_year, fact.fiscal_period) == (case.fiscal_year, case.fiscal_period)
                or (case.fiscal_period == FiscalPeriod.FY and fact.concept == FinancialConcept.TOTAL_DEBT)
            )
        }
        missing = {concept: [] for concept in case.required_concepts if concept not in available}
        coverage_reports.append(compute_coverage(case.cik, case.required_concepts, missing))
        by_key = {(f.concept, f.fiscal_year, f.fiscal_period): f for f in financials.facts}
        for expected in case.expected_facts:
            numeric_total += 1
            actual = by_key.get((expected.concept, expected.fiscal_year, expected.fiscal_period))
            if actual is not None and abs(actual.value - expected.value) <= expected.absolute_tolerance:
                numeric_correct += 1
            else:
                found = "missing" if actual is None else str(actual.value)
                failures.append(f"{case.case_id}: expected {expected.concept.value} {expected.fiscal_period.value} "
                                f"FY{expected.fiscal_year}={expected.value}, found {found}")

    relevant_returned = 0
    retrieved_total = 0
    for case in benchmark.retrieval_cases:
        html = _resolve(root, case.filing_html_path).read_text(encoding="utf-8")
        chunks = chunk_blocks(parse_filing_html(html))
        available = {chunk.chunk_id for chunk in chunks}
        index = HybridIndex(TfidfEmbeddingProvider())
        try:
            index.build(chunks)
            for query in case.queries:
                unknown = set(query.relevant_chunk_ids) - available
                if unknown:
                    raise ValueError(f"{query.query_id} labels unknown chunks: {sorted(unknown)}")
                hits = rerank_lexical_overlap(query.query, index.search(query.query), top_k=query.top_k)
                returned = [hit.chunk.chunk_id for hit in hits]
                relevant = len(set(returned) & set(query.relevant_chunk_ids))
                relevant_returned += relevant
                retrieved_total += len(returned)
                if relevant == 0:
                    failures.append(f"{query.query_id}: no relevant chunk in top {query.top_k}; got {returned}")
        finally:
            index.close()

    supported_claims = 0
    claim_total = 0
    for case in benchmark.citation_cases:
        html = _resolve(root, case.filing_html_path).read_text(encoding="utf-8")
        chunks = {c.chunk_id: c for c in chunk_blocks(parse_filing_html(html))}
        for claim in case.claims:
            claim_total += 1
            cited_text = " ".join(chunks[cid].text for cid in claim.cited_chunk_ids if cid in chunks).lower()
            supported = (len(claim.cited_chunk_ids) == sum(cid in chunks for cid in claim.cited_chunk_ids)
                         and all(phrase.lower() in cited_text for phrase in claim.required_evidence_phrases))
            if supported:
                supported_claims += 1
            else:
                failures.append(f"{claim.claim_id}: cited chunks do not contain every labeled evidence phrase")

    latencies = []
    for case in benchmark.pipeline_cases:
        raw = json.loads(_resolve(root, case.companyfacts_path).read_text(encoding="utf-8"))
        html = _resolve(root, case.filing_html_path).read_text(encoding="utf-8")
        event = FilingEvent(case.company_cik, case.company_name, case.accession_number, case.form,
                            case.filing_date, case.report_date, case.primary_document)
        started = time.monotonic()
        process_filing(event, lambda _: raw, lambda _: html,
                       data_provenance_note=f"{benchmark.scope.upper()} EVALUATION CASE")
        latencies.append(time.monotonic() - started)

    numeric_value = numeric_correct / numeric_total * 100 if numeric_total else None
    coverage_value = watchlist_coverage_rate(coverage_reports) if coverage_reports else None
    retrieval_value = relevant_returned / retrieved_total * 100 if retrieved_total else None
    citation_value = supported_claims / claim_total * 100 if claim_total else None
    latency_value = max(latencies) if latencies else None
    real_scope = benchmark.scope == "real_labeled"
    qualification = ("Real manually labeled benchmark; full certification still requires faithfulness and time-saved measurements."
                     if real_scope else "Synthetic baseline; not a PRD certification.")

    return EvaluationReport(
        benchmark_id=benchmark.benchmark_id,
        scope=benchmark.scope,
        # This harness intentionally cannot certify the two externally measured metrics yet.
        certification_status="provisional",
        numeric_accuracy=_metric(numeric_value, 100, "%", None if numeric_value is None else numeric_value >= 100,
                                 numeric_correct, numeric_total, qualification),
        xbrl_coverage=_metric(coverage_value, 90, "%", None if coverage_value is None else coverage_value >= 90,
                              sum(r.resolved_concepts for r in coverage_reports), sum(r.total_concepts for r in coverage_reports), qualification),
        retrieval_precision_at_k=_metric(retrieval_value, 85, "%", None if retrieval_value is None else retrieval_value >= 85,
                                         relevant_returned, retrieved_total, qualification),
        citation_support_proxy=_metric(citation_value, 90, "%", None if citation_value is None else citation_value >= 90,
                                       supported_claims, claim_total, "Deterministic evidence-presence proxy; not answer faithfulness."),
        answer_faithfulness=_metric(None, 90, "%", None, None, None,
                                    "Requires Ragas/DeepEval over generated answers and manually reviewed sources."),
        pipeline_latency=_metric(latency_value, 600, "s", None if latency_value is None else latency_value < 600,
                                 None, len(latencies), qualification),
        manual_time_saved=_metric(None, 80, "%", None, None, None,
                                  "Requires timed human and system reviews of the same real filings."),
        failures=failures,
    )
