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
from pathlib import Path
from typing import Any, Mapping


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

from data_investigation import investigate_anomalies
from data_investigation.engine import InvestigationEngine  # noqa: E402


def _read(path: str) -> Any:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))


def _write(path: str, payload: Any) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".json", delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _queries(request: Any) -> list[Mapping[str, Any]]:
    if isinstance(request, Mapping):
        raw = request.get("queries", [request])
    else:
        raw = request
    if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError("request必须包含非空queries数组")
    if len(raw) > 50:
        raise ValueError("单批queries不得超过50条")
    return list(raw)


def _fact_id(week_id, query, fact):
    # Engine identity contains input/version/scope/period/metric/query semantics;
    # case membership is stored separately and must not change fact identity.
    return fact["fact_id"]


def _replace_ids(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: replacements.get(item, item) if key == "fact_id" and isinstance(item, str) else _replace_ids(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replacements.get(item, item) if isinstance(item, str) else _replace_ids(item, replacements) for item in value]
    return value


def _govern_result(bundle: Mapping[str, Any], request: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    queries = _queries(request)
    governed_results = []
    coverage: dict[str, str] = {}
    for index, (query, raw_result) in enumerate(zip(queries, result.get("results", []), strict=True)):
        item = dict(raw_result)
        item["query_index"] = index
        item["insight_case_id"] = str(query.get("insight_case_id", ""))
        replacements: dict[str, str] = {}
        for fact in item.get("facts", []):
            if isinstance(fact, Mapping) and fact.get("fact_id"):
                replacements[str(fact["fact_id"])] = _fact_id(str(bundle.get("week_id", "")), query, fact)
        item = _replace_ids(item, replacements)
        item["fact_ids"] = [fact.get("fact_id") for fact in item.get("facts", []) if isinstance(fact, Mapping) and fact.get("fact_id")]
        group_id = str(query.get("anomaly_group_id", ""))
        if item.get("status") == "INVALID_QUERY":
            coverage[group_id] = "NOT_APPLICABLE"
        elif item.get("facts"):
            coverage[group_id] = "INVESTIGATED"
        elif item.get("status") == "DATA_UNAVAILABLE":
            coverage.setdefault(group_id, "DATA_UNAVAILABLE")
        else:
            coverage.setdefault(group_id, "NOT_APPLICABLE")
        governed_results.append(item)
    if any(item.get("status") == "INVALID_QUERY" for item in governed_results):
        raise ValueError("调查请求不符合注册Schema")
    return {
        "schema_version": "3.0",
        "week_id": bundle.get("week_id"),
        "input_signature": bundle.get("input_signature"),
        "implementation_version": "3.2.2",
        "status": result.get("status"),
        "query_count": len(governed_results),
        "results": governed_results,
        "coverage": [{"anomaly_group_id": group_id, "status": status} for group_id, status in sorted(coverage.items())],
        "privacy": {
            "contains_member_identifiers": bool(bundle.get("privacy", {}).get("contains_member_identifiers")),
            "contains_transaction_rows": False,
            "model_can_read_member_records": False,
            "member_record_visibility": "DETERMINISTIC_CODE_AND_REPORT_ONLY",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读RichMetrics执行受控聚合调查")
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = _read(args.metrics)
        privacy = bundle.get("privacy", {})
        if (
            str(bundle.get("schema_version")) != "3.0"
            or privacy.get("contains_transaction_rows") is not False
            or privacy.get("model_can_read_member_records") is not False
        ):
            raise ValueError("只接受不含交易明细且会员聚合禁止模型读取的RichMetricsBundle 3.0")
        request = _read(args.request)
        _queries(request)
        engine=InvestigationEngine(bundle)
        namespace=hashlib.sha256(Path(args.metrics).read_bytes()+(RUNTIME/"data_investigation"/"engine.py").read_bytes()).hexdigest()
        cache_dir=Path(args.metrics).parent/"query-cache"/namespace
        cache_dir.mkdir(parents=True,exist_ok=True)
        results=[];hits=0
        for index,query in enumerate(_queries(request),1):
            semantic={k:v for k,v in query.items() if k not in {"query_id","insight_case_id","anomaly_group_id"}}
            key=hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            cache_file=cache_dir/(key+".json")
            if cache_file.is_file():
                item=_read(str(cache_file));hits+=1
                item.update(query_id=query.get("query_id"),anomaly_group_id=query.get("anomaly_group_id"))
            else:
                item=engine._run_safely(query,index)
                if item.get("status")=="OK":_write(str(cache_file),item)
            results.append(item)
        status="OK" if all(r.get("status")=="OK" for r in results) else "PARTIAL" if any(r.get("facts") for r in results) else "FAILED"
        result={"results":results,"status":status}

        governed = _govern_result(bundle, request, result)
        governed["cache_hits"]=hits
        governed["executed_queries"]=len(results)-hits
        _write(args.output, governed)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "INVESTIGATION_FAILED", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "output": str(Path(args.output).expanduser().resolve()), "query_count": governed["query_count"], "status": governed["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
