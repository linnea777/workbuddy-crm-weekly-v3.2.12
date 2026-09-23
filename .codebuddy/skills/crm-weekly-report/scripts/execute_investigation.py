#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from common import ContractError, read_json, require_mapping, write_json
from validate_plan import validate_first_round, validate_second_round


BATCH_SIZE = 50
STATUS_RANK = {"NOT_APPLICABLE": 0, "DATA_UNAVAILABLE": 1, "UPSTREAM_SUFFICIENT": 2, "INVESTIGATED": 3}


def _flatten_queries(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    queries: list[dict[str, Any]] = []
    owners: dict[str, str] = {}
    for case in plan.get("selected_cases", []):
        case_id = str(case["insight_case_id"])
        for group_id in case.get("anomaly_group_ids", []):
            owners[str(group_id)] = case_id
        queries.extend(dict(query) for query in case.get("queries", []))
    return queries, owners


def _run_batch(script: Path, metrics: Path, queries: list[dict[str, Any]], output_dir: Path, batch_index: int) -> dict[str, Any]:
    request_path = output_dir / f"investigation_request_{batch_index:03d}.json"
    result_path = output_dir / f"investigation_result_{batch_index:03d}.json"
    write_json(request_path, {"queries": queries})
    command = [sys.executable, str(script), "--metrics", str(metrics), "--request", str(request_path), "--output", str(result_path)]
    last_error = ""
    for attempt in (1, 2):
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False, env=dict(os.environ), timeout=300)
        except subprocess.TimeoutExpired:
            last_error="BATCH_TIMEOUT: 300 seconds"
            continue
        if completed.returncode == 0 and result_path.is_file():
            return {"batch_index": batch_index, "attempts": attempt, "ok": True, "payload": read_json(result_path)}
        last_error = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
    return {"batch_index": batch_index, "attempts": 2, "ok": False, "error": last_error}


def execute(
    rich_metrics: dict[str, Any],
    plan: dict[str, Any],
    *,
    metrics_path: Path,
    data_script: Path,
    max_concurrency: int,
    temporary_dir: Path,
) -> dict[str, Any]:
    queries, owners = _flatten_queries(plan)
    batches = [queries[index:index + BATCH_SIZE] for index in range(0, len(queries), BATCH_SIZE)]
    completed_batches: list[dict[str, Any]] = []
    if batches:
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = {executor.submit(_run_batch, data_script, metrics_path, batch, temporary_dir, index): index for index, batch in enumerate(batches)}
            for future in as_completed(futures):
                completed_batches.append(future.result())
        completed_batches.sort(key=lambda item: item["batch_index"])
        failed = [item for item in completed_batches if not item["ok"]]
        if failed:
            raise ContractError("INVESTIGATION_BATCH_FAILED", "调查批次重试后仍失败", {"failed_batches": [{"batch_index": item["batch_index"], "error": item["error"]} for item in failed]})

    results: list[dict[str, Any]] = []
    coverage: dict[str, str] = {}
    query_cursor = 0
    for batch in completed_batches:
        payload = batch["payload"]
        for result in payload.get("results", []):
            item = dict(result)
            item["query_index"] = query_cursor
            results.append(item)
            group_id = str(item.get("anomaly_group_id", ""))
            if item.get("facts") and item.get("status")=="OK":
                status = "INVESTIGATED"
            elif item.get("facts"):
                status = "DATA_UNAVAILABLE"
            elif item.get("status") == "DATA_UNAVAILABLE":
                status = "DATA_UNAVAILABLE"
            else:
                status = "NOT_APPLICABLE"
            if STATUS_RANK[status] > STATUS_RANK.get(coverage.get(group_id, "NOT_APPLICABLE"), -1):
                coverage[group_id] = status
            query_cursor += 1
    queried = {str(query.get("anomaly_group_id")) for query in queries}
    for group_id in owners:
        if group_id not in queried:
            coverage[group_id] = "UPSTREAM_SUFFICIENT"
        elif group_id not in coverage:
            coverage[group_id] = "DATA_UNAVAILABLE"
    return {
        "schema_version": "3.0",
        "week_id": rich_metrics.get("week_id"),
        "input_signature": rich_metrics.get("input_signature"),
        "implementation_version": "3.2.2",
        "query_count": len(queries),
        "cache_hits":sum(b["payload"].get("cache_hits",0) for b in completed_batches),
        "status":"PARTIAL" if any(r.get("status")!="OK" for r in results) else "OK",
        "batch_size": BATCH_SIZE,
        "batch_count": len(batches),
        "max_concurrency": max_concurrency,
        "batches": [{"batch_index": item["batch_index"], "attempts": item["attempts"], "ok": item["ok"]} for item in completed_batches],
        "results": results,
        "coverage": [{"anomaly_group_id": group_id, "insight_case_id": owners[group_id], "status": coverage[group_id]} for group_id in sorted(owners)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按50条拆批、最多2批并发执行调查")
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--anomalies", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--frozen-plan")
    parser.add_argument("--round", type=int, choices=(1, 2), required=True)
    parser.add_argument("--max-concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-script", required=True)
    args = parser.parse_args(argv)
    try:
        rich = require_mapping(read_json(args.metrics), "rich_metrics")
        anomalies = require_mapping(read_json(args.anomalies), "anomaly_bundle")
        plan = require_mapping(read_json(args.plan), "plan")
        if args.round == 1:
            validate_first_round(anomalies, plan)
        else:
            if not args.frozen_plan:
                raise ContractError("INPUT_MISSING", "第二轮必须提供--frozen-plan")
            validate_second_round(plan, require_mapping(read_json(args.frozen_plan), "frozen_plan"))
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="crm-investigation-", dir=output.parent) as directory:
            payload = execute(rich, plan, metrics_path=Path(args.metrics).expanduser().resolve(), data_script=Path(args.data_script).expanduser().resolve(), max_concurrency=args.max_concurrency, temporary_dir=Path(directory))
        write_json(output, payload)
        print(json.dumps({"ok": True, "output": str(output), "query_count": payload["query_count"], "batch_count": payload["batch_count"]}, ensure_ascii=False))
        return 0
    except (ContractError, OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        error = exc.as_dict() if isinstance(exc, ContractError) else {"code": "INVESTIGATION_EXECUTION_FAILED", "message": str(exc), "details": {}}
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
