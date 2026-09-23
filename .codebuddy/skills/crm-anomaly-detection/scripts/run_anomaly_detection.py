#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
            check = subprocess.run([executable, "-c", "import sys; assert sys.version_info >= (3, 10)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            continue
        if check.returncode:
            continue
        if os.path.normcase(executable) == os.path.normcase(_runtime_executable(sys.executable)):
            return
        environment = dict(os.environ)
        environment["AICRM_PYTHON"] = executable
        os.execve(executable, [executable, os.path.abspath(__file__), *sys.argv[1:]], environment)
    raise RuntimeError("需要Python 3.10+；可通过AICRM_PYTHON指定解释器。")


_ensure_runtime()

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

from crm_anomaly_detector import DetectorConfig, detection_input_from_metric_bundle, detect_anomalies  # noqa: E402
from governance import enrich_anomaly_bundle  # noqa: E402


def _read_json(path: Path) -> Any:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".json", delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从MetricBundle生成完整AnomalyBundle候选池")
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        metric_bundle = _read_json(Path(args.metrics))
        config = DetectorConfig.from_json(args.config) if args.config else None
        detected = detect_anomalies(detection_input_from_metric_bundle(metric_bundle), config).to_dict()
        anomaly_bundle = enrich_anomaly_bundle(detected, metric_bundle)
        _write_json(Path(args.output), anomaly_bundle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "ANOMALY_DETECTION_FAILED", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2

    severity_counts: dict[str, int] = {}
    mandatory_count = 0
    for group in anomaly_bundle["anomaly_groups"]:
        severity = str(group.get("severity", "UNKNOWN"))
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        mandatory_count += int(group.get("must_investigate") is True)
    print(json.dumps({"ok": True, "output": str(Path(args.output).expanduser().resolve()), "anomaly_group_count": len(anomaly_bundle["anomaly_groups"]), "signal_count": len(anomaly_bundle["anomaly_signals"]), "must_investigate_count": mandatory_count, "severity_counts": severity_counts, "data_quality_count": len(anomaly_bundle["data_quality_status"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
