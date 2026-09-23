#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any


IMPLEMENTATION_VERSION = "3.2.2"


def _runtime_executable(candidate: str) -> str:
    expanded = os.path.expanduser(candidate)
    return os.path.abspath(shutil.which(expanded) or expanded)


def _ensure_runtime() -> None:
    candidates = [os.environ.get("AICRM_PYTHON"), sys.executable, shutil.which("python3.13"), shutil.which("python3.12"), shutil.which("python3.11"), shutil.which("python3.10")]
    for candidate in dict.fromkeys(item for item in candidates if item):
        executable = _runtime_executable(candidate)
        try:
            check = subprocess.run([executable, "-c", "import sys; assert sys.version_info >= (3, 10); import pandas, openpyxl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            continue
        if check.returncode:
            continue
        if os.path.normcase(executable) == os.path.normcase(_runtime_executable(sys.executable)):
            return
        environment = dict(os.environ)
        environment["AICRM_PYTHON"] = executable
        os.execve(executable, [executable, os.path.abspath(__file__), *sys.argv[1:]], environment)
    raise RuntimeError("需要Python 3.10+、pandas和openpyxl；可通过AICRM_PYTHON指定解释器。")


_ensure_runtime()

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import pandas as pd  # noqa: E402

from crm_weekly_metrics import CalculationError, CalculationRequest, calculate_metrics  # noqa: E402
from crm_weekly_metrics.calculator import _prepare  # noqa: E402
from input_loader import load_crm, load_mall  # noqa: E402
from rich_metrics import build_rich_metrics, activity_daily_payload  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_signature(paths: dict[str, Path], week_start: date) -> str:
    payload = {"week_start": week_start.isoformat(), "files": {name: _sha256(path) for name, path in sorted(paths.items())}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".json", delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=lambda value: value.isoformat())
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一次读取CRM/Mall并生成MetricBundle与RichMetricsBundle")
    parser.add_argument("--crm", required=True)
    parser.add_argument("--mall-n", required=True)
    parser.add_argument("--mall-s", required=True)
    parser.add_argument("--mall-w", required=True)
    parser.add_argument("--week-start", required=True, help="YYYY-MM-DD，必须是周一")
    parser.add_argument("--output", required=True, help="MetricBundle JSON")
    parser.add_argument("--rich-output", required=True, help="RichMetricsBundle JSON")
    parser.add_argument("--details-only", action="store_true", help="校验已有产物与来源后，仅补充活动日级数据")
    parser.add_argument("--include-member-days", action="store_true")
    parser.add_argument("--include-extended-members", action="store_true")
    parser.add_argument("--coverage-json", help="可选按来源/周一日期/区域声明的完整性清单")
    parser.add_argument("--audit-dir", help="可选输入审计目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = checkpoint = time.perf_counter()
    timings = {}
    def mark(stage):
        nonlocal checkpoint
        current = time.perf_counter()
        timings[stage] = round(current - checkpoint, 4)
        checkpoint = current
    def save_timing(ok):
        _write_json(Path(args.output).parent / ("metrics_detail_timing.json" if args.details_only else "metrics_timing.json"),
                    {"ok": ok, "elapsed_seconds": round(time.perf_counter() - started, 4), "stages": timings})
    try:
        week_start = date.fromisoformat(args.week_start)
        if week_start.weekday() != 0:
            raise CalculationError("PERIOD_INCOMPLETE", "week_start必须是周一", {"week_start": args.week_start})
        paths = {
            "crm": Path(args.crm).expanduser().resolve(),
            "mall_n": Path(args.mall_n).expanduser().resolve(),
            "mall_s": Path(args.mall_s).expanduser().resolve(),
            "mall_w": Path(args.mall_w).expanduser().resolve(),
        }
        if args.coverage_json:
            paths["coverage"] = Path(args.coverage_json).expanduser().resolve()
        signature = _input_signature(paths, week_start)
        mark("input_signature")
        crm_raw, crm_audit = load_crm(paths["crm"])
        mark("read_crm")
        mall_raw, mall_audit = load_mall({"N": paths["mall_n"], "S": paths["mall_s"], "W": paths["mall_w"]})
        mark("read_mall")
        coverage = json.loads(paths["coverage"].read_text()) if "coverage" in paths else {}
        request = CalculationRequest(week_start=week_start, crm=crm_raw, mall=mall_raw, coverage=coverage)
        prepared = _prepare(request)
        prepared[0].attrs["coverage"] = coverage
        prepared[1].attrs["coverage"] = coverage
        mark("normalize_inputs")
        if args.details_only:
            if not args.include_member_days or args.include_extended_members:
                raise ValueError("details-only仅用于活动日级数据")
            metric_bundle = json.loads(Path(args.output).read_text())
            rich_bundle = json.loads(Path(args.rich_output).read_text())
            for bundle in (metric_bundle, rich_bundle):
                if bundle.get("input_signature") != signature or bundle.get("implementation_version") != IMPLEMENTATION_VERSION:
                    raise ValueError("来源或版本已变化，请重新prepare")
            records, coverage_days, mall_days = activity_daily_payload(prepared[0], prepared[1], metric_bundle)
            rich_bundle["crm"].update(member_day_facts=records, member_day_coverage=coverage_days)
            rich_bundle["mall"]["day_facts"] = mall_days
            _write_json(Path(args.rich_output), rich_bundle)
            mark("activity_details_and_save")
            save_timing(True)
            print(json.dumps({"ok": True, "details_only": True, "member_day_count": len(records)}))
            return 0
        metric_bundle = calculate_metrics(request, prepared=prepared).as_dict()
        mark("calculate_metrics")
        metric_bundle.update({"input_signature": signature, "implementation_version": IMPLEMENTATION_VERSION, "input_audit": {"crm": crm_audit, "mall": mall_audit}})
        prepared_crm, prepared_mall, _, _ = prepared
        rich_bundle = build_rich_metrics(prepared_crm, prepared_mall, metric_bundle, input_signature=signature, include_member_days=args.include_member_days, include_extended_members=args.include_extended_members)
        mark("build_history_and_investigation_data")
        _write_json(Path(args.output), metric_bundle)
        _write_json(Path(args.rich_output), rich_bundle)
        if args.audit_dir:
            audit_dir = Path(args.audit_dir).expanduser().resolve()
            _write_json(audit_dir / "crm_ingestion_audit.json", crm_audit)
            _write_json(audit_dir / "mall_ingestion_audit.json", mall_audit)
        mark("save_artifacts")
        save_timing(True)
    except CalculationError as exc:
        try:
            save_timing(False)
        except OSError:
            pass
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, pd.errors.ParserError) as exc:
        try:
            save_timing(False)
        except OSError:
            pass
        print(json.dumps({"ok": False, "error": {"code": "METRICS_FAILED", "message": str(exc), "details": {}}}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps({"ok": True, "metric_bundle": str(Path(args.output).expanduser().resolve()), "rich_metrics": str(Path(args.rich_output).expanduser().resolve()), "week_id": metric_bundle["week_id"], "metric_count": len(metric_bundle["metrics"]), "input_signature": signature}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
