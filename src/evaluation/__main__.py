import argparse
import json
from pathlib import Path

from src.evaluation.harness import evaluate_benchmark, load_benchmark

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate PRD quality metrics against a labeled benchmark")
    parser.add_argument("--benchmark", type=Path, default=ROOT / "benchmarks/synthetic_baseline.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/evaluation")
    args = parser.parse_args()
    benchmark = load_benchmark(args.benchmark)
    report = evaluate_benchmark(benchmark, ROOT)
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
