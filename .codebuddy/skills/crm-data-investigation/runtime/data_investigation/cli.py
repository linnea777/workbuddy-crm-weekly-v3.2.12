from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import InvestigationEngine, InvestigationError


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic CRM anomaly investigations")
    parser.add_argument("--metrics", required=True, help="Path to the aggregate metrics JSON bundle")
    parser.add_argument("--request", default="-", help="Path to request JSON; default reads stdin")
    parser.add_argument("--output", default="-", help="Path to output JSON; default writes stdout")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        metrics = _read_json(args.metrics)
        request = _read_json(args.request)
        result = InvestigationEngine(metrics).investigate(request)
    except (OSError, json.JSONDecodeError, InvestigationError, TypeError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    kwargs = {"ensure_ascii": False}
    if not args.compact:
        kwargs["indent"] = 2
    text = json.dumps(result, **kwargs) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0 if result["status"] != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

