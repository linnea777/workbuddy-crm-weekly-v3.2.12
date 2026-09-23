from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ReportExportError(ValueError):
    """Blocking input or workbook error returned by the N05 node."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True, slots=True)
class ReportExportResult:
    output_path: Path
    week_id: str
    sheet_names: tuple[str, ...]
    insight_count: int
    alert_count: int
    quality_count: int
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "week_id": self.week_id,
            "sheet_names": list(self.sheet_names),
            "insight_count": self.insight_count,
            "alert_count": self.alert_count,
            "quality_count": self.quality_count,
            "warnings": list(self.warnings),
        }
