from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .validation import validate_workbook_data
from .workbook import WorkbookData, load_knowledge_workbook


def _json_default(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def content_hash(data: WorkbookData) -> str:
    payload = {"activities": data.activities, "config": data.config}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_metadata(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            return dict(conn.execute("SELECT key, value FROM metadata"))
    except sqlite3.Error:
        return {}


def _snapshot(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT activity_id, record_json FROM activities").fetchall()
        return {activity_id: record_json for activity_id, record_json in rows}
    except sqlite3.Error:
        return {}


def _changes(old: dict[str, str], data: WorkbookData) -> dict[str, list[str]]:
    new = {
        str(row["activity_id"]): json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)
        for row in data.activities
    }
    return {
        "added": sorted(set(new) - set(old)),
        "modified": sorted(key for key in set(new) & set(old) if new[key] != old[key]),
        "deleted": sorted(set(old) - set(new)),
    }


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE activities (
            activity_id TEXT PRIMARY KEY,
            activity_name TEXT NOT NULL,
            party_type TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            brand_scope TEXT NOT NULL,
            region_scope TEXT NOT NULL,
            card_tier_scope TEXT NOT NULL,
            mechanism_summary TEXT NOT NULL,
            record_json TEXT NOT NULL
        );
        CREATE TABLE config (config_key TEXT PRIMARY KEY, config_value TEXT NOT NULL);
        CREATE INDEX idx_activity_dates ON activities(start_date, end_date);
        CREATE INDEX idx_activity_type ON activities(activity_type);
        """
    )


def _insert_data(conn: sqlite3.Connection, data: WorkbookData, metadata: dict[str, str]) -> None:
    conn.executemany("INSERT INTO metadata(key, value) VALUES(?, ?)", metadata.items())
    for row in data.activities:
        conn.execute(
            "INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["activity_id"], row["activity_name"], row["party_type"], row["activity_type"],
                row["start_date"], row["end_date"], row["brand_scope"], row["region_scope"],
                row["card_tier_scope"], row["mechanism_summary"],
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default),
            ),
        )
    conn.executemany("INSERT INTO config(config_key, config_value) VALUES(?, ?)", data.config.items())


def _append_log(log_path: Path, entry: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _backup_current(db_path: Path, backup_dir: Path, keep: int = 5) -> str | None:
    if not db_path.exists():
        return None
    metadata = _current_metadata(db_path)
    version = metadata.get("kb_version", datetime.now().strftime("unknown-%Y%m%d%H%M%S"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"activity_kb_{version}.sqlite"
    shutil.copy2(db_path, target)
    backups = sorted(backup_dir.glob("activity_kb_*.sqlite"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in backups[keep:]:
        stale.unlink()
    return str(target)


def sync_knowledge_base(
    workbook_path: str | Path,
    db_path: str | Path,
    *,
    backup_dir: str | Path,
    log_path: str | Path,
    rebuild: bool = False,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backups = Path(backup_dir).expanduser().resolve()
    log = Path(log_path).expanduser().resolve()
    data = load_knowledge_workbook(workbook_path)
    validation = validate_workbook_data(data, db)
    if not validation.valid:
        result = {"ok": False, "operation": "rebuild" if rebuild else "sync", "validation": validation.to_dict(), "index_preserved": db.exists()}
        _append_log(log, {"timestamp": datetime.now(timezone.utc).isoformat(), **result})
        return result

    digest = content_hash(data)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    version = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + digest[:8]
    changes = _changes(_snapshot(db), data)
    metadata = {
        "kb_version": version,
        "schema_version": data.config["schema_version"],
        "content_hash": digest,
        "synced_at": timestamp,
        "workbook_path": str(Path(workbook_path).expanduser().resolve()),
        "activity_count": str(len(data.activities)),
        "knowledge_base_status": "available",
        "validation_warnings": "0",
    }

    db.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(prefix="activity_kb_", suffix=".sqlite.tmp", dir=db.parent)
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(temp_path) as conn:
            _create_schema(conn)
            _insert_data(conn, data, metadata)
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("SQLite完整性检查失败")
        backup = _backup_current(db, backups)
        os.replace(temp_path, db)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    result = {
        "ok": True,
        "operation": "rebuild" if rebuild else "sync",
        "kb_version": version,
        "knowledge_base_status": "available",
        "activity_count": len(data.activities),
        "validation": validation.to_dict(),
        "changes": changes,
        "backup_created": backup,
        "db_path": str(db),
    }
    _append_log(log, {"timestamp": timestamp, **result})
    return result


def ensure_knowledge_base(
    workbook_path: str | Path,
    db_path: str | Path,
    *,
    backup_dir: str | Path,
    log_path: str | Path,
) -> dict[str, Any]:
    """Synchronize the generated index only when the maintained Excel changed."""
    db = Path(db_path).expanduser().resolve()
    data = load_knowledge_workbook(workbook_path)
    digest = content_hash(data)
    current = _current_metadata(db)
    if db.exists() and current.get("content_hash") == digest:
        return {
            "ok": True,
            "operation": "unchanged",
            "kb_version": current.get("kb_version"),
            "knowledge_base_status": current.get("knowledge_base_status", "available"),
            "activity_count": len(data.activities),
            "db_path": str(db),
        }
    return sync_knowledge_base(
        workbook_path,
        db,
        backup_dir=backup_dir,
        log_path=log_path,
    )


def status(db_path: str | Path) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    if not db.exists():
        return {"available": False, "knowledge_base_status": "unavailable", "db_path": str(db), "message": "尚未生成索引，请先运行sync"}
    metadata = _current_metadata(db)
    with sqlite3.connect(db) as conn:
        activity_count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    return {"available": True, **metadata, "active_activity_count": activity_count, "archived_activity_count": 0, "db_path": str(db)}


def rollback(db_path: str | Path, backup_dir: str | Path, version: str, log_path: str | Path) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backups = Path(backup_dir).expanduser().resolve()
    source = backups / f"activity_kb_{version}.sqlite"
    if not source.exists():
        available = [path.name.removeprefix("activity_kb_").removesuffix(".sqlite") for path in sorted(backups.glob("activity_kb_*.sqlite"))]
        return {"ok": False, "message": f"找不到版本：{version}", "available_versions": available}
    _backup_current(db, backups)
    temp = db.with_suffix(".rollback.tmp")
    shutil.copy2(source, temp)
    with sqlite3.connect(temp) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            temp.unlink(missing_ok=True)
            raise sqlite3.DatabaseError("备份索引完整性检查失败")
    os.replace(temp, db)
    result = {"ok": True, "operation": "rollback", "restored_version": version, "db_path": str(db)}
    _append_log(Path(log_path), {"timestamp": datetime.now(timezone.utc).isoformat(), **result})
    return result
