from __future__ import annotations

import argparse
from pathlib import Path

from sideral_multimodels.processing import generate_output, parse_hours


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gerar frames multimodelo sintéticos")
    parser.add_argument("--config", required=True)
    parser.add_argument("--date", default="")
    parser.add_argument("--cycle", default="")
    parser.add_argument("--hours", default="")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--output-dir", default="output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    hours = parse_hours(args.hours)
    run_label = args.date or "current"
    if args.cycle:
        run_label = f"{run_label}_{args.cycle}z"
    generate_output(output_dir=output_dir, hours=hours, run_name=run_label)
    print(f"Gerados {len(hours)} leads em {output_dir / run_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
