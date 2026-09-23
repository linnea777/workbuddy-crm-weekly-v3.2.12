from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".json", delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=lambda value: value.isoformat())
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target


def require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("FIELD_INVALID", f"{path}必须是JSON对象", {"path": path})
    return value


def require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError("FIELD_INVALID", f"{path}必须是数组", {"path": path})
    return value


def require_text(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError("FIELD_INVALID", f"{path}不能为空", {"path": path})
    return text
