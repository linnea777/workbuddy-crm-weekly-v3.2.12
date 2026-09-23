from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd


class CalculationError(ValueError):
    """Blocking, user-fixable input error."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True, slots=True)
class CalculationRequest:
    week_start: date
    crm: pd.DataFrame
    mall: pd.DataFrame
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MetricBundle:
    week_id: str
    windows: dict[str, dict[str, str]]
    metrics: dict[str, Any]
    data_quality_status: list[dict[str, Any]] = field(default_factory=list)
    run_summary: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.1"
    anomaly_records: list[dict[str, Any]] = field(default_factory=list)
    high_luxury_amount_band_context: list[dict[str, Any]] = field(default_factory=list)
    reporting_policy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "week_id": self.week_id,
            "windows": self.windows,
            "metrics": self.metrics,
            "data_quality_status": self.data_quality_status,
            "run_summary": self.run_summary,
            "schema_version": self.schema_version,
            "anomaly_records": self.anomaly_records,
            "high_luxury_amount_band_context": self.high_luxury_amount_band_context,
            "reporting_policy": self.reporting_policy,
        }
