from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import DetectionInput, HistoryPoint, MetricRecord


def detection_input_from_metric_bundle(bundle: Any) -> DetectionInput:
    """Convert MetricBundle 1.1 (or its dict form) into the detector contract."""
    payload = bundle.as_dict() if hasattr(bundle, "as_dict") else bundle
    if not isinstance(payload, Mapping):
        raise TypeError("metric bundle must be a mapping or expose as_dict()")
    if str(payload.get("schema_version", "")) != "1.1":
        raise ValueError("MetricBundle schema_version 1.1 is required")

    records = []
    for raw in payload.get("anomaly_records", ()):
        data = dict(raw)
        data.pop("coverage_status", None)
        data["history"] = tuple(HistoryPoint(**point) for point in data.get("history", ()))
        data["large_spend_signal_bases"] = tuple(data.get("large_spend_signal_bases", ()))
        records.append(MetricRecord(**data))

    summary = payload.get("run_summary", {})
    return DetectionInput(
        records=tuple(records),
        mall_sales_total_cells=summary.get("mall_sales_total_cells"),
        mall_sales_missing_cells=summary.get("mall_sales_missing_cells"),
        upstream_data_quality_status=tuple(payload.get("data_quality_status", ())),
    )
