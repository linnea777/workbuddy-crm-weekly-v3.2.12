from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .indexer import ensure_knowledge_base, rollback, status, sync_knowledge_base
from .search import search_activities, search_activities_batch
from .validation import validate_workbook_data
from .workbook import load_knowledge_workbook


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_ROOT = ROOT / "knowledge_base"
DEFAULT_WORKBOOK = KNOWLEDGE_ROOT / "activity_knowledge_base.xlsx"
DEFAULT_DB = KNOWLEDGE_ROOT / "data" / "activity_kb.sqlite"
DEFAULT_BACKUPS = KNOWLEDGE_ROOT / "backups"
DEFAULT_LOG = KNOWLEDGE_ROOT / "logs" / "sync_history.jsonl"


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="归因活动知识库维护与检索工具")
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK), help="知识库Excel路径")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite索引路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="校验Excel，不更新索引")
    sub.add_parser("sync", help="校验并增量同步索引")
    sub.add_parser("ensure", help="Excel内容变化时才同步索引")
    sub.add_parser("rebuild", help="完整重建索引")
    sub.add_parser("status", help="查看当前索引状态")
    search_parser = sub.add_parser("search", help="执行活动检索")
    search_parser.add_argument("--input", required=True, help="查询JSON文件")
    search_parser.add_argument("--top-k", type=int, default=None)
    rollback_parser = sub.add_parser("rollback", help="回滚到已备份版本")
    rollback_parser.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            data = load_knowledge_workbook(args.workbook)
            result = validate_workbook_data(data, args.db)
            _print(result.to_dict())
            return 0 if result.valid else 1
        if args.command == "ensure":
            result = ensure_knowledge_base(
                args.workbook,
                args.db,
                backup_dir=DEFAULT_BACKUPS,
                log_path=DEFAULT_LOG,
            )
            _print(result)
            return 0 if result.get("ok") else 1
        if args.command in {"sync", "rebuild"}:
            result = sync_knowledge_base(args.workbook, args.db, backup_dir=DEFAULT_BACKUPS, log_path=DEFAULT_LOG, rebuild=args.command == "rebuild")
            _print(result)
            return 0 if result.get("ok") else 1
        if args.command == "status":
            _print(status(args.db))
            return 0
        if args.command == "search":
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if "queries" in payload:
                result = search_activities_batch(payload["queries"], db_path=args.db)
            else:
                result = search_activities(payload.get("query", {}), payload["date_range"], args.top_k, db_path=args.db)
            _print(result)
            return 0 if result.get("knowledge_base_status") != "unavailable" else 1
        if args.command == "rollback":
            result = rollback(args.db, DEFAULT_BACKUPS, args.version, DEFAULT_LOG)
            _print(result)
            return 0 if result.get("ok") else 1
    except Exception as exc:
        _print({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
