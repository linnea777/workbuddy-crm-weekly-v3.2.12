#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _runtime_executable(candidate: str) -> str:
    expanded = os.path.expanduser(candidate)
    return os.path.abspath(shutil.which(expanded) or expanded)


def _ensure_runtime() -> None:
    candidates = [os.environ.get("AICRM_PYTHON"), sys.executable, shutil.which("python3.13"), shutil.which("python3.12"), shutil.which("python3.11"), shutil.which("python3.10")]
    for candidate in dict.fromkeys(item for item in candidates if item):
        executable = _runtime_executable(candidate)
        try:
            check = subprocess.run([executable, "-c", "import sys; assert sys.version_info >= (3, 10); import openpyxl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            continue
        if check.returncode:
            continue
        if os.path.normcase(executable) == os.path.normcase(_runtime_executable(sys.executable)):
            return
        environment = dict(os.environ)
        environment["AICRM_PYTHON"] = executable
        os.execve(executable, [executable, os.path.abspath(__file__), *sys.argv[1:]], environment)
    raise RuntimeError("需要Python 3.10+和openpyxl；可通过AICRM_PYTHON指定解释器。")


_ensure_runtime()

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "runtime"))

from crm_report_exporter import ReportExportError  # noqa: E402
from v3_exporter import export_weekly_report_v3  # noqa: E402


def _read(path: str) -> Any:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成完整可审计CRM周报")
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--anomalies", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--insights", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--template", default=str(SKILL_ROOT / "assets" / "周报模板.xlsx"))
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = export_weekly_report_v3(args.template, args.output, _read(args.metrics), _read(args.anomalies), _read(args.plan), _read(args.insights), _read(args.evidence))
    except ReportExportError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "REPORT_OUTPUT_FAILED", "message": str(exc), "details": {}}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "report": result.as_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
