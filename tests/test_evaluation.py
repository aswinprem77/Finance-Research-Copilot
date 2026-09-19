from pathlib import Path

import pytest

from src.evaluation.harness import _resolve, evaluate_benchmark, load_benchmark

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks/synthetic_baseline.json"


def test_loads_versioned_synthetic_benchmark():
    benchmark = load_benchmark(BENCHMARK)
    assert benchmark.benchmark_id == "synthetic-baseline-v1"
    assert benchmark.scope == "synthetic"
    assert sum(len(case.queries) for case in benchmark.retrieval_cases) == 4


def test_baseline_measures_without_claiming_prd_certification():
    report = evaluate_benchmark(load_benchmark(BENCHMARK), ROOT)
    assert report.certification_status == "provisional"
    assert report.numeric_accuracy.value == 100
    assert report.numeric_accuracy.passed
    assert report.xbrl_coverage.value == 40
    assert not report.xbrl_coverage.passed
    assert report.retrieval_precision_at_k.value == 100
    assert report.citation_support_proxy.value == 100
    assert report.answer_faithfulness.value is None
    assert report.answer_faithfulness.passed is None
    assert report.manual_time_saved.value is None
    assert report.pipeline_latency.value is not None
    assert report.pipeline_latency.value < 600
    assert not report.failures


def test_numeric_mismatch_is_reported():
    benchmark = load_benchmark(BENCHMARK)
    expected = benchmark.numeric_cases[0].expected_facts[0]
    expected.value += 1
    report = evaluate_benchmark(benchmark, ROOT)
    assert report.numeric_accuracy.value < 100
    assert not report.numeric_accuracy.passed
    assert any("expected revenue Q1 FY2023" in failure for failure in report.failures)


def test_unknown_retrieval_label_fails_fast():
    benchmark = load_benchmark(BENCHMARK)
    benchmark.retrieval_cases[0].queries[0].relevant_chunk_ids = ["not-a-real-chunk"]
    with pytest.raises(ValueError, match="unknown chunks"):
        evaluate_benchmark(benchmark, ROOT)


def test_benchmark_paths_cannot_escape_project_root(tmp_path):
    outside = tmp_path.parent / "outside.json"
    with pytest.raises(ValueError, match="escapes project root"):
        _resolve(tmp_path, "../outside.json")


def test_markdown_keeps_proxy_separate_from_faithfulness():
    markdown = evaluate_benchmark(load_benchmark(BENCHMARK), ROOT).to_markdown()
    assert "Citation support proxy" in markdown
    assert "Answer faithfulness | Not measured" in markdown
    assert "not Ragas/DeepEval answer faithfulness" in markdown


def test_real_scope_stays_provisional_while_external_metrics_are_unmeasured():
    benchmark = load_benchmark(BENCHMARK)
    benchmark.scope = "real_labeled"
    report = evaluate_benchmark(benchmark, ROOT)
    assert report.certification_status == "provisional"
    assert report.answer_faithfulness.value is None
    assert report.manual_time_saved.value is None
