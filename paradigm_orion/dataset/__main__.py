import argparse
import json
from .pipeline import build_dataset


def main():
    parser = argparse.ArgumentParser(description="Structure telemetry and explicit labels into reproducible training datasets.")
    parser.add_argument("--recipe", required=True, help="JSON recipe; feature hooks resolve relative to it")
    parser.add_argument("--batches", required=True, nargs="+", help="PostHog event/properties JSONL exports")
    parser.add_argument("--labels", required=True, help="Interval labels with source, version and confidence")
    parser.add_argument("--recordings", required=True, help="Recording to split-group mapping JSONL")
    parser.add_argument("--out", required=True, help="New output directory (existing data is never overwritten)")
    args = parser.parse_args()
    try:
        result = build_dataset(args.recipe, args.batches, args.labels, args.recordings, args.out)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Dataset build failed: {exc}\n")
    print(json.dumps({"output": args.out, "counts": result["counts"], "rejections": result["rejections"], "warnings": result["warnings"]}, indent=2))


if __name__ == "__main__":
    main()
