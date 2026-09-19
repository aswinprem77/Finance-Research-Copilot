"""Run the read API: python -m src.service"""
import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the read API over processed filings")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/live",
                        help="Directory holding records/ and memos/, as written by the pipeline")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Default is localhost: the API has no authentication of its own")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        parser.error("Serving needs uvicorn. Install it with `pip install -r requirements.txt`.")

    # create_app() reads this when uvicorn imports the module by name, which is
    # what --reload requires.
    os.environ["COPILOT_DATA_DIR"] = str(args.data_dir)
    print(f"Serving {args.data_dir} on http://{args.host}:{args.port} (docs at /docs)")
    uvicorn.run("src.service.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
