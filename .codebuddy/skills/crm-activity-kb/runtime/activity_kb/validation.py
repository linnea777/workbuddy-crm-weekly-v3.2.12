from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .workbook import WorkbookData


ACTIVITY_TYPES = {
    "multiple_points", "points_redemption", "gift_with_purchase", "cashback",
    "prize_draw", "popup", "celebrity_event", "brand_event",
    "competitor_promotion", "other",
}
PARTY_TYPES = {"own", "competitor"}
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{5,79}$")


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    location: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: int
    warnings: int
    activity_count: int
    issues: list[Issue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "activity_count": self.activity_count,
            "issues": [asdict(issue) for issue in self.issues],
        }


def _text(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value).strip()


def validate_workbook_data(data: WorkbookData, existing_db: str | Path | None = None) -> ValidationResult:
    del existing_db
    issues: list[Issue] = []

    def error(code: str, location: str, message: str) -> None:
        issues.append(Issue("ERROR", code, location, message))

    ids: list[str] = []
    required = (
        "activity_id", "activity_name", "party_type", "activity_type",
        "start_date", "end_date", "brand_scope", "region_scope",
        "card_tier_scope", "mechanism_summary",
    )
    for index, row in enumerate(data.activities, 4):
        loc = f"Activities!{index}"
        activity_id = _text(row, "activity_id")
        ids.append(activity_id)
        for field in required:
            if not _text(row, field):
                error("REQUIRED_FIELD", loc, f"{field}不能为空")
        if activity_id and not ID_RE.fullmatch(activity_id):
            error("INVALID_ACTIVITY_ID", loc, "activity_id只能包含大写字母、数字和连字符")
        if _text(row, "party_type") not in PARTY_TYPES:
            error("INVALID_ENUM", loc, f"party_type无效：{_text(row, 'party_type')}")
        if _text(row, "activity_type") not in ACTIVITY_TYPES:
            error("INVALID_ENUM", loc, f"activity_type无效：{_text(row, 'activity_type')}")
        try:
            start = date.fromisoformat(_text(row, "start_date"))
            end = date.fromisoformat(_text(row, "end_date"))
            if start > end:
                error("DATE_ORDER", loc, "start_date不能晚于end_date")
        except ValueError:
            error("INVALID_DATE", loc, "日期必须为有效的YYYY-MM-DD")

    duplicates = sorted({item for item in ids if item and ids.count(item) > 1})
    for activity_id in duplicates:
        error("DUPLICATE_ACTIVITY_ID", "Activities", f"activity_id重复：{activity_id}")

    errors = sum(issue.severity == "ERROR" for issue in issues)
    return ValidationResult(errors == 0, errors, 0, len(data.activities), issues)
