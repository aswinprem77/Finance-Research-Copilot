import argparse
import json
from pathlib import Path

from src.evaluation.harness import evaluate_benchmark, load_benchmark
from src.retrieval.providers import PROFILES, build_retrieval_stack

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate PRD quality metrics against a labeled benchmark")
    parser.add_argument("--benchmark", type=Path, default=ROOT / "benchmarks/synthetic_baseline.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/evaluation")
    parser.add_argument("--retrieval-profile", choices=PROFILES,
                        help="Retrieval stack to evaluate. A labeled benchmark records the stack "
                             "that produced its passages and refuses to score against another.")
    args = parser.parse_args()
    benchmark = load_benchmark(args.benchmark)
    # A synthetic benchmark measures evaluator mechanics, so it runs on the
    # lexical stack unless asked otherwise; no model download to reproduce it.
    default_profile = "lexical" if benchmark.scope == "synthetic" else None
    try:
        retrieval = build_retrieval_stack(args.retrieval_profile or default_profile)
        report = evaluate_benchmark(benchmark, ROOT, retrieval=retrieval)
    except ValueError as exc:
        parser.error(str(exc))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{benchmark.benchmark_id}.json"
    markdown_path = args.output_dir / f"{benchmark.benchmark_id}.md"
    json_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    print(report.to_markdown())
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
