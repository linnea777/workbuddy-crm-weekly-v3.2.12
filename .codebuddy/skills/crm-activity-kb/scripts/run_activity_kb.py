#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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

from activity_kb.indexer import ensure_knowledge_base, rollback, status, sync_knowledge_base  # noqa: E402
from activity_kb.search import search_activities, search_activities_batch  # noqa: E402
from activity_kb.validation import validate_workbook_data  # noqa: E402
from activity_kb.workbook import load_knowledge_workbook  # noqa: E402


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _govern_evidence(result: dict[str, Any]) -> dict[str, Any]:
    for query_result in result.get("results", [result]):
        for evidence in query_result.get("retrieved_evidence", []):
            evidence.setdefault("time_relation", evidence.get("relation_to_anomaly"))
            evidence.setdefault("applicable_scope", {"brand": evidence.get("brand_scope"), "region": evidence.get("region_scope"), "card_tier": evidence.get("card_tier_scope")})
            evidence.setdefault("activity_mechanism", evidence.get("mechanism_summary"))
            evidence.setdefault("matching_reason", evidence.get("match_reasons", []))
            evidence.setdefault("attribution_limit", evidence.get("attribution_limit"))
    return result


def _search_many(queries: list[dict[str, Any]], db: Path, max_concurrency: int) -> dict[str, Any]:
    batches = [queries[index:index + 50] for index in range(0, len(queries), 50)]
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        completed = list(executor.map(lambda batch: search_activities_batch(batch, db_path=db), batches))
    results = [item for batch in completed for item in batch.get("results", [])]
    errors = [item for batch in completed for item in batch.get("errors", [])]
    status_value = "unavailable" if any(batch.get("knowledge_base_status") == "unavailable" for batch in completed) else "available"
    kb_version = next((batch.get("kb_version") for batch in completed if batch.get("kb_version")), None)
    return _govern_evidence({"schema_version": "3.0", "knowledge_base_status": status_value, "kb_version": kb_version, "results": results, "errors": errors, "batch_count": len(batches)})


def build_parser() -> argparse.ArgumentParser:
    state_default = Path.cwd() / "crm-activity-kb-state"
    parser = argparse.ArgumentParser(description="自包含CRM活动知识库")
    parser.add_argument("--workbook", default=str(SKILL_ROOT / "assets" / "activity_knowledge_base.xlsx"))
    parser.add_argument("--state-dir", default=str(state_default), help="SQLite、日志和备份的用户输出目录")
    parser.add_argument("--max-concurrency", type=int, choices=(1, 2), default=2)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "ensure", "sync", "rebuild", "status", "versions"):
        commands.add_parser(name)
    search = commands.add_parser("search")
    search.add_argument("--input", required=True)
    search.add_argument("--top-k", type=int)
    restore = commands.add_parser("rollback")
    restore.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = Path(args.state_dir).expanduser().resolve()
    workbook = Path(args.workbook).expanduser().resolve()
    db = state_dir / "data" / "activity_kb.sqlite"
    backups = state_dir / "backups"
    log = state_dir / "logs" / "sync_history.jsonl"
    try:
        if args.command == "validate":
            result = validate_workbook_data(load_knowledge_workbook(workbook), db).to_dict()
            _print(result)
            return 0 if result["valid"] else 1
        if args.command == "ensure":
            result = ensure_knowledge_base(workbook, db, backup_dir=backups, log_path=log)
        elif args.command in {"sync", "rebuild"}:
            result = sync_knowledge_base(workbook, db, backup_dir=backups, log_path=log, rebuild=args.command == "rebuild")
        elif args.command == "status":
            result = status(db)
        elif args.command == "versions":
            versions = [item.name.removeprefix("activity_kb_").removesuffix(".sqlite") for item in sorted(backups.glob("activity_kb_*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)]
            result = {"ok": True, "current": status(db).get("kb_version"), "available_versions": versions}
        elif args.command == "search":
            payload = json.loads(Path(args.input).expanduser().resolve().read_text(encoding="utf-8"))
            if "queries" in payload:
                result = _search_many(list(payload["queries"]), db, args.max_concurrency)
            else:
                result = _govern_evidence(search_activities(payload.get("query", {}), payload["date_range"], args.top_k, db_path=db))
                result["schema_version"] = "3.0"
        elif args.command == "rollback":
            result = rollback(db, backups, args.version, log)
        else:
            raise ValueError(f"不支持的操作：{args.command}")
        _print(result)
        return 0 if args.command == "search" or result.get("ok", result.get("available", True)) else 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        _print({"ok": False, "knowledge_base_status": "unavailable", "error": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
