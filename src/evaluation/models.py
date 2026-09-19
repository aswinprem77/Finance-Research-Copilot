from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.schema.financial_schema import FinancialConcept, FiscalPeriod


class ExpectedFact(BaseModel):
    concept: FinancialConcept
    fiscal_year: int
    fiscal_period: FiscalPeriod
    value: float = Field(allow_inf_nan=False)
    absolute_tolerance: float = Field(default=0.0, ge=0.0)


class NumericCase(BaseModel):
    case_id: str
    companyfacts_path: str
    cik: str
    accession_number: str
    filed_date: date
    period_end_date: date
    fiscal_year: int
    fiscal_period: FiscalPeriod
    required_concepts: list[FinancialConcept]
    expected_facts: list[ExpectedFact]


class RetrievalQuery(BaseModel):
    query_id: str
    query: str
    relevant_chunk_ids: list[str] = Field(min_length=1)
    top_k: int = Field(default=1, ge=1)


class RetrievalCase(BaseModel):
    case_id: str
    filing_html_path: str
    queries: list[RetrievalQuery] = Field(min_length=1)


class CitationClaim(BaseModel):
    claim_id: str
    claim: str
    cited_chunk_ids: list[str] = Field(min_length=1)
    required_evidence_phrases: list[str] = Field(min_length=1)


class CitationCase(BaseModel):
    case_id: str
    filing_html_path: str
    claims: list[CitationClaim] = Field(min_length=1)


class PipelineCase(BaseModel):
    case_id: str
    companyfacts_path: str
    filing_html_path: str
    company_cik: str
    company_name: str
    accession_number: str
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str


class Benchmark(BaseModel):
    benchmark_id: str
    description: str
    scope: Literal["synthetic", "real_labeled"]
    numeric_cases: list[NumericCase] = Field(default_factory=list)
    retrieval_cases: list[RetrievalCase] = Field(default_factory=list)
    citation_cases: list[CitationCase] = Field(default_factory=list)
    pipeline_cases: list[PipelineCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_cases(self):
        if not any((self.numeric_cases, self.retrieval_cases, self.citation_cases, self.pipeline_cases)):
            raise ValueError("Benchmark must contain at least one evaluation case")
        query_ids = [q.query_id for case in self.retrieval_cases for q in case.queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Retrieval query IDs must be unique")
        return self


class MetricResult(BaseModel):
    value: float | None
    target: float
    unit: str
    passed: bool | None
    numerator: int | None = None
    denominator: int | None = None
    qualification: str


class EvaluationReport(BaseModel):
    benchmark_id: str
    scope: str
    certification_status: Literal["provisional", "eligible_for_certification"]
    numeric_accuracy: MetricResult
    xbrl_coverage: MetricResult
    retrieval_precision_at_k: MetricResult
    citation_support_proxy: MetricResult
    answer_faithfulness: MetricResult
    pipeline_latency: MetricResult
    manual_time_saved: MetricResult
    failures: list[str]

    def to_markdown(self) -> str:
        lines = [
            f"# Evaluation Report - {self.benchmark_id}", "",
            f"**Scope:** {self.scope}",
            f"**Certification status:** {self.certification_status}", "",
            "| Metric | Result | PRD target | Status | Qualification |",
            "|---|---:|---:|---|---|",
        ]
        for label, metric in (
            ("Numeric accuracy", self.numeric_accuracy),
            ("XBRL coverage", self.xbrl_coverage),
            ("Retrieval precision@k", self.retrieval_precision_at_k),
            ("Citation support proxy", self.citation_support_proxy),
            ("Answer faithfulness", self.answer_faithfulness),
            ("Pipeline latency", self.pipeline_latency),
            ("Manual time saved", self.manual_time_saved),
        ):
            value = "Not measured" if metric.value is None else f"{metric.value:.2f}{metric.unit}"
            target = f"{metric.target:g}{metric.unit}"
            status = "Not measured" if metric.passed is None else ("PASS" if metric.passed else "FAIL")
            lines.append(f"| {label} | {value} | {target} | {status} | {metric.qualification} |")
        if self.failures:
            lines.extend(["", "## Failed checks", ""] + [f"- {failure}" for failure in self.failures])
        lines.extend([
            "", "## Interpretation", "",
            "Synthetic results validate the evaluator and pipeline mechanics only. PRD targets require a real, manually labeled watchlist benchmark.",
            "Citation support is a deterministic evidence-presence check; it is not Ragas/DeepEval answer faithfulness.",
        ])
        return "\n".join(lines) + "\n"
