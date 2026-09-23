from __future__ import annotations

from dataclasses import asdict, dataclass, field as dc_field
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence


NumberLike = Decimal | int | float | str


@dataclass(frozen=True)
class HistoryPoint:
    week_id: str
    value: NumberLike


@dataclass(frozen=True)
class MetricRecord:
    """One upstream standard metric. Rates use decimals: -0.0995 means -9.95%."""

    week_id: str
    dimension_type: str
    dimension_key: str
    metric_id: str
    current_value: Optional[NumberLike]
    source_record_id: str
    previous_value: Optional[NumberLike] = None
    wow_rate: Optional[NumberLike] = None
    yoy_reference_value: Optional[NumberLike] = None
    yoy_rate: Optional[NumberLike] = None
    ytd_current_value: Optional[NumberLike] = None
    ytd_reference_value: Optional[NumberLike] = None
    ytd_yoy_rate: Optional[NumberLike] = None
    history: Sequence[HistoryPoint] = dc_field(default_factory=tuple)
    attribution_context: Mapping[str, Any] = dc_field(default_factory=dict)
    capture_ratio_denominator_zero: bool = False
    has_large_spend_signal: bool = False
    large_spend_signal_bases: Sequence[str] = dc_field(default_factory=tuple)
    region_id: Optional[str] = None


@dataclass(frozen=True)
class DetectionInput:
    records: Sequence[MetricRecord]
    mall_sales_total_cells: Optional[int] = None
    mall_sales_missing_cells: Optional[int] = None
    upstream_data_quality_status: Sequence[Mapping[str, Any]] = dc_field(default_factory=tuple)


@dataclass(frozen=True)
class AnomalySignal:
    signal_id: str
    anomaly_group_id: str
    signal_type: str
    signal_role: str
    severity: str
    direction: str
    comparison_basis: str
    observed_value: str
    threshold_value: str
    current_value: Optional[str] = None
    baseline_value: Optional[str] = None
    change_amount: Optional[str] = None
    display_value: Optional[str] = None
    display_change: Optional[str] = None
    display_unit: Optional[str] = None
    details: Mapping[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class AnomalyGroup:
    anomaly_group_id: str
    week_id: str
    dimension_type: str
    dimension_key: str
    metric_id: str
    current_value: str
    change_amount: Optional[str]
    group_severity: str
    direction: str
    available_dimensions: Sequence[str]
    signal_ids: Sequence[str]
    display_value: Optional[str] = None
    display_change: Optional[str] = None
    display_unit: Optional[str] = None
    has_large_spend_signal: bool = False
    large_spend_signal_bases: Sequence[str] = dc_field(default_factory=tuple)
    region_id: Optional[str] = None


@dataclass(frozen=True)
class AttributionContextRecord:
    anomaly_group_id: str
    context: Mapping[str, Any]
    source_record_refs: Sequence[str]


@dataclass(frozen=True)
class DataQualityStatus:
    code: str
    scope: str
    message: str
    affected_rule: Optional[str] = None
    field: Optional[str] = None
    acceptable_aliases: Sequence[str] = dc_field(default_factory=tuple)
    details: Mapping[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class AnomalyBundle:
    schema_version: str
    anomaly_groups: Sequence[AnomalyGroup]
    anomaly_signals: Sequence[AnomalySignal]
    attribution_context: Sequence[AttributionContextRecord]
    data_quality_status: Sequence[DataQualityStatus]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
