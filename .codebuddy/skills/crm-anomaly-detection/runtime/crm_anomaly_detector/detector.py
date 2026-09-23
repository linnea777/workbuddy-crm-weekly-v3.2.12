from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median
from typing import Any, Iterable, Mapping, Optional, Sequence

from .config import DetectorConfig, load_default_config
from .models import (
    AnomalyBundle,
    AnomalyGroup,
    AnomalySignal,
    AttributionContextRecord,
    DataQualityStatus,
    DetectionInput,
    MetricRecord,
)


SEVERITY_RANK = {"YELLOW": 1, "RED": 2}
SUPPORTED_DIMENSIONS = {"TOTAL", "REGION", "MEMBER_TIER", "REGION_MEMBER_TIER", "BRAND"}
SUPPORTED_METRICS = {"CRM_SALES", "NEW_REGISTRATIONS", "ACTIVE_MEMBERS", "CAPTURE_RATIO"}
SALES_METRIC = "CRM_SALES"
MEMBER_COUNT_METRICS = {"NEW_REGISTRATIONS", "ACTIVE_MEMBERS"}
PII_EXACT_KEYS = {"member_list", "raw_row", "raw_transaction", "transaction_rows"}
PII_KEY_TOKENS = {"card", "age", "address", "phone", "mobile", "email"}
PII_KEY_TEXT = {"卡号", "年龄", "地区"}
SAFE_CONTEXT_KEYS = {
    "mall_sales", "capture_ratio", "brand_metrics", "record_refs", "coverage_rate", "source_refs",
    "crm_change_rate", "mall_change_rate", "crm_status", "mall_status", "direction_relation",
    "change_rate_gap_pp", "magnitude_relation", "capture_status", "high_luxury_amount_band_context",
}
EXCLUDED_BRAND_KEYS = {"APPLE", "APPLE-S07", "英皇电影城", "VSS0013140"}
AVAILABLE_DIMENSIONS = {
    ("TOTAL", "CRM_SALES"): ("REGION",),
    ("REGION", "CRM_SALES"): ("BRAND",),
    ("MEMBER_TIER", "CRM_SALES"): ("REGION_MEMBER_TIER", "BRAND"),
    ("REGION_MEMBER_TIER", "CRM_SALES"): ("BRAND",),
    ("BRAND", "CRM_SALES"): (),
    ("MEMBER_TIER", "NEW_REGISTRATIONS"): ("REGION_MEMBER_TIER", "BRAND"),
    ("MEMBER_TIER", "ACTIVE_MEMBERS"): ("REGION_MEMBER_TIER", "BRAND"),
    ("TOTAL", "CAPTURE_RATIO"): ("REGION",),
    ("REGION", "CAPTURE_RATIO"): ("BRAND",),
}


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def _fmt(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _stable_id(prefix: str, *parts: str) -> str:
    body = "\x1f".join(parts)
    return f"{prefix}_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:20]}"


def _canonical(value: str, aliases: Mapping[str, Any]) -> str:
    cleaned = str(value).strip()
    return str(aliases.get(cleaned, cleaned)).upper()


def _history_position(week_id: str) -> Optional[tuple[str, int, int]]:
    text = str(week_id).strip().upper()
    iso_match = re.fullmatch(r"(\d{4})-W(\d{2})", text)
    if iso_match:
        try:
            ordinal = date.fromisocalendar(int(iso_match.group(1)), int(iso_match.group(2)), 1).toordinal()
        except ValueError:
            return None
        return ("iso", ordinal, 7)
    week_match = re.fullmatch(r"W(\d+)", text)
    if week_match:
        return ("week", int(week_match.group(1)), 1)
    if text.isdigit():
        return ("index", int(text), 1)
    return None


def _is_pii_key(key: str) -> bool:
    lowered = str(key).strip().lower()
    if lowered in PII_EXACT_KEYS or any(text in lowered for text in PII_KEY_TEXT):
        return True
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    return bool(tokens & PII_KEY_TOKENS)


@dataclass
class _Evaluation:
    record: MetricRecord
    dimension_type: str
    dimension_key: str
    metric_id: str
    group_id: str
    current: Decimal
    previous: Optional[Decimal]
    signals: list[AnomalySignal]


class AnomalyDetector:
    """Deterministic detector implementing controlled-agent contract v0.5.0."""

    def __init__(self, config: Optional[DetectorConfig] = None):
        self.config = config or load_default_config()
        self._dq: list[DataQualityStatus] = []

    def detect(self, bundle: DetectionInput) -> AnomalyBundle:
        self._dq = []
        self._bundle_quality(bundle)
        self._check_region_tier_completeness(bundle.records)
        duplicate_keys = self._check_duplicate_business_keys(bundle.records)

        evaluations: list[_Evaluation] = []
        for record in sorted(bundle.records, key=self._record_sort_key):
            if self._business_key(record) in duplicate_keys:
                continue
            evaluation = self._evaluate_record(record)
            if evaluation and evaluation.signals:
                self._append_robust_z(evaluation)
                evaluations.append(evaluation)

        groups: list[AnomalyGroup] = []
        signals: list[AnomalySignal] = []
        contexts: list[AttributionContextRecord] = []
        for evaluation in evaluations:
            ordered = sorted(evaluation.signals, key=lambda s: (s.signal_type, s.signal_id))
            signals.extend(ordered)
            severity = max((s.severity for s in ordered), key=lambda value: SEVERITY_RANK[value])
            change = evaluation.current - evaluation.previous if evaluation.previous is not None else None
            directions = {s.direction for s in ordered if s.signal_role == "BUSINESS_TRIGGER"}
            direction = "MIXED" if len(directions) > 1 else next(iter(directions))
            signal_bases = {signal.comparison_basis for signal in ordered}
            large_spend_bases = tuple(basis for basis in evaluation.record.large_spend_signal_bases if basis in signal_bases)
            groups.append(
                AnomalyGroup(
                    anomaly_group_id=evaluation.group_id,
                    region_id=evaluation.record.region_id,
                    week_id=evaluation.record.week_id,
                    dimension_type=evaluation.dimension_type,
                    dimension_key=evaluation.dimension_key,
                    metric_id=evaluation.metric_id,
                    current_value=_fmt(evaluation.current) or "0",
                    change_amount=_fmt(change),
                    group_severity=severity,
                    direction=direction,
                    available_dimensions=AVAILABLE_DIMENSIONS.get((evaluation.dimension_type, evaluation.metric_id), ()),
                    signal_ids=tuple(s.signal_id for s in ordered),
                    display_value=self._display_value(evaluation.metric_id, evaluation.current),
                    display_change=self._display_change(evaluation.metric_id, change),
                    display_unit=self._display_unit(evaluation.metric_id),
                    has_large_spend_signal=bool(evaluation.record.has_large_spend_signal and large_spend_bases),
                    large_spend_signal_bases=large_spend_bases,
                )
            )
            clean_context = self._sanitize_context(evaluation.record.attribution_context)
            contexts.append(AttributionContextRecord(evaluation.group_id, clean_context, (self._safe_ref(evaluation.record.source_record_id),)))

        groups.sort(key=lambda g: (g.week_id, g.dimension_type, g.dimension_key, g.metric_id, g.anomaly_group_id))
        signal_order = {signal_id: index for index, group in enumerate(groups) for signal_id in group.signal_ids}
        signals.sort(key=lambda s: (signal_order.get(s.signal_id, 10**9), s.signal_type, s.signal_id))
        dq = sorted(self._dq, key=lambda item: (item.code, item.scope, item.affected_rule or "", item.field or "", item.message))
        return AnomalyBundle(
            schema_version=str(self.config.data.get("schema_version", "1.1")),
            anomaly_groups=tuple(groups),
            anomaly_signals=tuple(signals),
            attribution_context=tuple(contexts),
            data_quality_status=tuple(dq),
        )

    def _record_sort_key(self, record: MetricRecord) -> tuple[str, str, str, str]:
        return (str(record.week_id), str(record.dimension_type), str(record.dimension_key), str(record.metric_id))

    def _business_key(self, record: MetricRecord) -> tuple[str, str, str, str]:
        dim, key, metric = self._normalise(record)
        return (str(record.week_id).strip(), dim, key + ("@" + record.region_id if record.region_id else ""), metric)

    def _check_duplicate_business_keys(self, records: Iterable[MetricRecord]) -> set[tuple[str, str, str, str]]:
        counts: dict[tuple[str, str, str, str], int] = {}
        for record in records:
            business_key = self._business_key(record)
            counts[business_key] = counts.get(business_key, 0) + 1
        duplicates = {key for key, count in counts.items() if count > 1}
        for week_id, dim, key, metric in sorted(duplicates):
            scope = f"{week_id}/{dim}/{key}/{metric}"
            self._status(
                "DUPLICATE_BUSINESS_KEY",
                scope,
                "Multiple input records share the same normalized business key; the group is skipped",
                details={"record_count": counts[(week_id, dim, key, metric)]},
            )
        return duplicates

    def _normalise(self, record: MetricRecord) -> tuple[str, str, str]:
        aliases = self.config.section("aliases")
        dim = _canonical(record.dimension_type, aliases.get("dimension_type", {}))
        if dim == "STORE":
            dim = "BRAND"
        metric = str(record.metric_id).strip().upper()
        key = str(record.dimension_key).strip()
        if dim == "TOTAL":
            key = "TOTAL"
        elif dim == "REGION":
            key = _canonical(key, aliases.get("region", {}))
        elif dim == "BRAND":
            # Brand names are business identifiers in v3.  Do not apply aliases,
            # case folding or Unicode normalization here.
            key = str(key).strip()
        elif dim == "REGION_MEMBER_TIER" and "|" in key:
            region, tier = (part.strip() for part in key.split("|", 1))
            key = f"{_canonical(region, aliases.get('region', {}))}|{tier}"
        return dim, key, metric

    def _evaluate_record(self, record: MetricRecord) -> Optional[_Evaluation]:
        dim, key, metric = self._normalise(record)
        scope = f"{record.week_id}/{dim}/{key}/{metric}"
        if dim not in SUPPORTED_DIMENSIONS:
            self._status("UNSUPPORTED_DIMENSION", scope, f"Unsupported dimension_type: {record.dimension_type}")
            return None
        if dim == "BRAND" and key.upper() in EXCLUDED_BRAND_KEYS:
            self._status("EXCLUDED_ENTITY_PRESENT", scope, "Excluded brand/store reached anomaly detection; the record is skipped")
            return None
        if metric not in SUPPORTED_METRICS:
            self._status("UNSUPPORTED_METRIC", scope, f"Unsupported metric_id: {record.metric_id}")
            return None
        if not record.week_id or not key:
            self._status("MISSING_REQUIRED_FIELD", scope, "week_id and dimension_key are required", field="week_id/dimension_key", aliases=("week", "dimension"))
            return None
        if metric == "CAPTURE_RATIO" and record.capture_ratio_denominator_zero:
            self._status("CAPTURE_RATIO_DENOMINATOR_ZERO", scope, "Capture Ratio denominator is zero; ratio is N/A and its rule is skipped", "CAPTURE_RATIO_PP_CHANGE")
            return None
        current = _decimal(record.current_value)
        if current is None:
            code = "MISSING_REQUIRED_FIELD" if record.current_value is None else "INVALID_CURRENT_VALUE"
            self._status(code, scope, "current_value is missing or invalid", field="current_value", aliases=("current", "this_week_value"))
            return None
        previous = _decimal(record.previous_value)
        group_id = _stable_id("ag", str(record.week_id), dim, key + ("@" + record.region_id if record.region_id else ""), metric)
        evaluation = _Evaluation(record, dim, key, metric, group_id, current, previous, [])

        if metric == SALES_METRIC:
            self._sales_rules(evaluation)
        elif metric in MEMBER_COUNT_METRICS:
            self._member_count_rules(evaluation)
        elif metric == "CAPTURE_RATIO":
            self._capture_ratio_rule(evaluation)
        return evaluation

    def _sales_rules(self, ev: _Evaluation) -> None:
        if ev.dimension_type in {"TOTAL", "REGION"}:
            key = "TOTAL" if ev.dimension_type == "TOTAL" else ev.dimension_key
            cfg = self.config.section("total_region").get(key)
            if not cfg:
                self._status("UNSUPPORTED_DIMENSION", self._scope(ev), f"Unsupported region: {key}")
                return
            self._simple_wow(ev, cfg["wow"])
            self._yoy_ytd(ev, cfg["yoy_ytd"])
            self._continuous_decline(ev, cfg["decline"])
        elif ev.dimension_type == "MEMBER_TIER":
            cfg = self.config.section("member_tier").get(ev.dimension_key)
            if not cfg:
                self._status("UNSUPPORTED_DIMENSION", self._scope(ev), f"Unsupported member tier: {ev.dimension_key}")
                return
            self._simple_wow(ev, cfg)
            self._continuous_decline(ev, {"yellow_weeks": 2, "yellow": cfg["yellow"], "red_weeks": 3, "red": cfg["red"]})
        elif ev.dimension_type == "REGION_MEMBER_TIER":
            cfg = self.config.section("region_member_tier")
            self._dual_threshold_wow(ev, cfg["wow"])
            self._continuous_decline(ev, cfg["decline"])
        elif ev.dimension_type == "BRAND":
            brand_cfg = self.config.section("brands").get(ev.dimension_key)
            if brand_cfg:
                self._simple_wow(ev, brand_cfg["wow"])
                self._rolling_median_deviation(ev, brand_cfg["median"])
                self._continuous_decline(ev, {"yellow_weeks": 2, "yellow": brand_cfg["median"]["yellow"], "red_weeks": 3, "red": brand_cfg["median"]["red"]})
                return
            brand_tier = self._brand_tier(ev)
            if brand_tier:
                self._dual_threshold_wow(ev, {
                    "yellow_rate": brand_tier["yellow_rate"], "yellow_amount": brand_tier["yellow_amount"],
                    "red_rate": brand_tier["red_rate"], "red_amount": brand_tier["red_amount"],
                }, extra={"brand_tier": brand_tier["name"], "store_tier": brand_tier["name"]})
                self._continuous_decline(ev, {
                    "yellow_weeks": 2, "yellow": brand_tier["yellow_rate"], "yellow_amount": brand_tier["yellow_amount"],
                    "red_weeks": 3, "red": brand_tier["red_rate"], "red_amount": brand_tier["red_amount"],
                }, extra={"brand_tier": brand_tier["name"], "store_tier": brand_tier["name"]})

    def _member_count_rules(self, ev: _Evaluation) -> None:
        if ev.dimension_type != "MEMBER_TIER":
            self._status("UNSUPPORTED_DIMENSION", self._scope(ev), "Member-count rules only support MEMBER_TIER")
            return
        self._simple_wow(ev, self.config.section("member_count"))

    def _simple_wow(self, ev: _Evaluation, thresholds: Mapping[str, Any]) -> None:
        rate = self._rate(ev.record.wow_rate, ev, "WOW_THRESHOLD", "wow_rate")
        if rate is None:
            return
        severity = self._severity(abs(rate), Decimal(str(thresholds["yellow"])), Decimal(str(thresholds["red"])))
        if severity:
            threshold = thresholds["red"] if severity == "RED" else thresholds["yellow"]
            self._add_signal(ev, "WOW_THRESHOLD", "BUSINESS_TRIGGER", severity, self._direction(rate), rate, Decimal(str(threshold)), {"unit": "percent"})

    def _dual_threshold_wow(self, ev: _Evaluation, thresholds: Mapping[str, Any], extra: Optional[Mapping[str, Any]] = None) -> None:
        rate = self._rate(ev.record.wow_rate, ev, "WOW_THRESHOLD", "wow_rate")
        if rate is None:
            return
        if ev.previous is None:
            self._status("INVALID_PREVIOUS_VALUE", self._scope(ev), "previous_value is required for amount threshold", "WOW_THRESHOLD", "previous_value")
            return
        amount = abs(ev.current - ev.previous)
        yellow = abs(rate) >= Decimal(str(thresholds["yellow_rate"])) and amount >= Decimal(str(thresholds["yellow_amount"]))
        red = abs(rate) >= Decimal(str(thresholds["red_rate"])) and amount >= Decimal(str(thresholds["red_amount"]))
        if yellow:
            severity = "RED" if red else "YELLOW"
            prefix = "red" if red else "yellow"
            details = {"unit": "percent", "absolute_change_amount": _fmt(amount), **(extra or {})}
            self._add_signal(ev, "WOW_THRESHOLD", "BUSINESS_TRIGGER", severity, self._direction(rate), rate, Decimal(str(thresholds[f"{prefix}_rate"])), details, threshold_text=f"rate>={thresholds[f'{prefix}_rate']}%,amount>={thresholds[f'{prefix}_amount']}")

    def _yoy_ytd(self, ev: _Evaluation, thresholds: Mapping[str, Any]) -> None:
        for signal_type, raw, field_name in (
            ("YOY_THRESHOLD", ev.record.yoy_rate, "yoy_rate"),
            ("YTD_YOY_THRESHOLD", ev.record.ytd_yoy_rate, "ytd_yoy_rate"),
        ):
            rate = self._rate(raw, ev, signal_type, field_name, required=False)
            if rate is None or rate >= 0:
                continue
            severity = self._severity(abs(rate), Decimal(str(thresholds["yellow"])), Decimal(str(thresholds["red"])))
            if severity:
                threshold = thresholds["red"] if severity == "RED" else thresholds["yellow"]
                self._add_signal(ev, signal_type, "BUSINESS_TRIGGER", severity, "DOWN", rate, Decimal(str(threshold)), {"unit": "percent", "decline_only": True})

    def _capture_ratio_rule(self, ev: _Evaluation) -> None:
        if ev.dimension_type not in {"TOTAL", "REGION"}:
            self._status("UNSUPPORTED_DIMENSION", self._scope(ev), "Capture Ratio only independently detects Total/N/S/W")
            return
        if ev.previous is None:
            self._status("INVALID_PREVIOUS_VALUE", self._scope(ev), "previous_value is required for Capture Ratio pp change", "CAPTURE_RATIO_PP_CHANGE", "previous_value")
            return
        delta_pp = (ev.current - ev.previous) * Decimal("100")
        cfg = self.config.section("capture_ratio")
        severity = self._severity(abs(delta_pp), Decimal(str(cfg["yellow_pp"])), Decimal(str(cfg["red_pp"])))
        if severity:
            threshold = cfg["red_pp"] if severity == "RED" else cfg["yellow_pp"]
            self._add_signal(ev, "CAPTURE_RATIO_PP_CHANGE", "BUSINESS_TRIGGER", severity, self._direction(delta_pp), delta_pp, Decimal(str(threshold)), {"unit": "percentage_point"})

    def _brand_tier(self, ev: _Evaluation) -> Optional[Mapping[str, Any]]:
        values = self._history_values(ev, 8, "BRAND_8W_MEDIAN_TIER")
        if values is None:
            return None
        med = Decimal(str(median(values)))
        for tier in self.config.data.get("brand_tiers", self.config.data.get("store_tiers", [])):
            minimum = _decimal(tier.get("median_min"))
            maximum = _decimal(tier.get("median_max"))
            if (minimum is None or med >= minimum) and (maximum is None or med < maximum):
                return {**tier, "median": _fmt(med)}
        return None

    def _rolling_median_deviation(self, ev: _Evaluation, thresholds: Mapping[str, Any]) -> None:
        values = self._history_values(ev, 8, "ROLLING_MEDIAN_DEVIATION")
        if values is None:
            return
        med = Decimal(str(median(values)))
        if med == 0:
            self._status("INVALID_PREVIOUS_VALUE", self._scope(ev), "8-week median is zero; deviation is N/A", "ROLLING_MEDIAN_DEVIATION")
            return
        rate = self._quantize_percent((ev.current / med - Decimal("1")) * Decimal("100"))
        severity = self._severity(abs(rate), Decimal(str(thresholds["yellow"])), Decimal(str(thresholds["red"])))
        if severity:
            threshold = thresholds["red"] if severity == "RED" else thresholds["yellow"]
            self._add_signal(ev, "ROLLING_MEDIAN_DEVIATION", "BUSINESS_TRIGGER", severity, self._direction(rate), rate, Decimal(str(threshold)), {"unit": "percent", "history_median": _fmt(med), "window_weeks": 8})

    def _continuous_decline(self, ev: _Evaluation, thresholds: Mapping[str, Any], extra: Optional[Mapping[str, Any]] = None) -> None:
        points = self._contiguous_suffix(ev)
        parsed = [_decimal(point.value) for point in points]
        if not parsed:
            self._status("INSUFFICIENT_HISTORY", self._scope(ev), "No valid adjacent history", "CONTINUOUS_DECLINE")
            return
        sequence = [value for value in parsed if value is not None] + [ev.current]
        decline_steps = 0
        index = len(sequence) - 1
        while index > 0 and sequence[index] < sequence[index - 1]:
            decline_steps += 1
            index -= 1
        if decline_steps < int(thresholds["yellow_weeks"]):
            return
        base = sequence[-1 - decline_steps]
        if base == 0:
            self._status("INVALID_PREVIOUS_VALUE", self._scope(ev), "Value before continuous decline is zero", "CONTINUOUS_DECLINE")
            return
        cumulative = self._quantize_percent((ev.current / base - Decimal("1")) * Decimal("100"))
        amount = abs(ev.current - base)
        yellow = abs(cumulative) >= Decimal(str(thresholds["yellow"]))
        red = decline_steps >= int(thresholds["red_weeks"]) and abs(cumulative) >= Decimal(str(thresholds["red"]))
        if "yellow_amount" in thresholds:
            yellow = yellow and amount >= Decimal(str(thresholds["yellow_amount"]))
            red = red and amount >= Decimal(str(thresholds["red_amount"]))
        if yellow:
            severity = "RED" if red else "YELLOW"
            threshold = thresholds["red"] if red else thresholds["yellow"]
            details = {"unit": "percent", "continuous_decline_weeks": decline_steps, "cumulative_decline_rate": _fmt(cumulative), "cumulative_change_amount": _fmt(ev.current - base), **(extra or {})}
            self._add_signal(ev, "CONTINUOUS_DECLINE", "BUSINESS_TRIGGER", severity, "DOWN", cumulative, Decimal(str(threshold)), details)

    def _append_robust_z(self, ev: _Evaluation) -> None:
        if not self.config.section("robust_z").get("enabled", True):
            return
        if ev.metric_id not in {SALES_METRIC, "CAPTURE_RATIO"}:
            return
        values = [_decimal(point.value) for point in ev.record.history]
        positions = [_history_position(str(point.week_id)) for point in ev.record.history]
        min_changes = int(self.config.section("windows").get("robust_z_min_history_changes", 12))
        max_changes = int(self.config.section("windows").get("robust_z_history_changes", 26))
        changes: list[Decimal] = []
        for i, (previous, current) in enumerate(zip(values, values[1:])):
            left, right = positions[i:i+2]
            if previous is None or current is None or left is None or right is None or left[0] != right[0] or right[1]-left[1] != left[2]:
                continue
            if ev.metric_id == SALES_METRIC:
                if previous <= 0 or current <= 0:
                    continue
                changes.append(Decimal(str(math.log(float(current / previous)))))
            else:
                changes.append(current - previous)
        changes = changes[-max_changes:]
        if len(changes) < min_changes:
            self._status("INSUFFICIENT_HISTORY", self._scope(ev), f"Robust Z-score requires at least {min_changes} historical changes", "ROBUST_ZSCORE", details={"valid_history_changes": len(changes)})
            return
        invalid_previous = ev.previous is None or (ev.metric_id == SALES_METRIC and ev.previous <= 0)
        invalid_current = ev.metric_id == SALES_METRIC and ev.current <= 0
        if invalid_previous or invalid_current:
            code = "INVALID_PREVIOUS_VALUE" if invalid_previous else "INVALID_CURRENT_VALUE"
            self._status(code, self._scope(ev), "Current/previous value cannot produce Robust Z-score", "ROBUST_ZSCORE")
            return
        current_change = Decimal(str(math.log(float(ev.current / ev.previous)))) if ev.metric_id == SALES_METRIC else ev.current - ev.previous
        hist_med = Decimal(str(median(changes)))
        mad = Decimal(str(median([abs(value - hist_med) for value in changes])))
        if mad == 0:
            self._status("ZERO_MAD", self._scope(ev), "MAD is zero; Robust Z-score omitted", "ROBUST_ZSCORE")
            return
        z = Decimal("0.6745") * (current_change - hist_med) / mad
        cfg = self.config.section("robust_z")
        severity = self._severity(abs(z), Decimal(str(cfg["yellow"])), Decimal(str(cfg["red"])))
        if severity:
            threshold = cfg["red"] if severity == "RED" else cfg["yellow"]
            self._add_signal(ev, "ROBUST_ZSCORE", "SUPPLEMENTAL", severity, self._direction(z), z.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), Decimal(str(threshold)), {"history_median": _fmt(hist_med), "mad": _fmt(mad), "history_change_count": len(changes)})

    @staticmethod
    def _contiguous_suffix(ev):
        expected = _history_position(str(ev.record.week_id))
        result = []
        for point in reversed(ev.record.history):
            position = _history_position(str(point.week_id))
            if (_decimal(point.value) is None or position is None or expected is None
                    or position[0] != expected[0] or expected[1] - position[1] != position[2]):
                break
            result.append(point)
            expected = position
        return list(reversed(result))

    def _history_values(self, ev: _Evaluation, count: int, rule: str) -> Optional[list[Decimal]]:
        recent = self._contiguous_suffix(ev)[-count:]
        values = [_decimal(point.value) for point in recent]
        if len(recent) < count or any(value is None for value in values):
            self._status("INSUFFICIENT_HISTORY", self._scope(ev), f"{rule} requires {count} valid historical weeks", rule, details={"valid_weeks": sum(value is not None for value in values)})
            return None
        return [value for value in values if value is not None]

    def _rate(self, raw: Any, ev: _Evaluation, rule: str, field_name: str, required: bool = True) -> Optional[Decimal]:
        value = _decimal(raw)
        if value is None:
            if raw is not None:
                self._status("INVALID_NUMERIC_VALUE", self._scope(ev), f"{field_name} is not a finite number", rule, field_name)
            elif required:
                self._status("MISSING_REQUIRED_FIELD", self._scope(ev), f"{field_name} is required for {rule}", rule, field_name, (field_name.replace("_display", ""),))
            return None
        return self._quantize_percent(value * Decimal("100"))

    def _quantize_percent(self, value: Decimal) -> Decimal:
        places = int(self.config.section("rounding").get("percentage_decimal_places", 1))
        quantum = Decimal("1").scaleb(-places)
        return value.quantize(quantum, rounding=ROUND_HALF_UP)

    @staticmethod
    def _severity(observed: Decimal, yellow: Decimal, red: Decimal) -> Optional[str]:
        if observed >= red:
            return "RED"
        if observed >= yellow:
            return "YELLOW"
        return None

    @staticmethod
    def _direction(value: Decimal) -> str:
        return "UP" if value > 0 else "DOWN" if value < 0 else "FLAT"

    def _add_signal(self, ev: _Evaluation, signal_type: str, role: str, severity: str, direction: str, observed: Decimal, threshold: Decimal, details: Mapping[str, Any], threshold_text: Optional[str] = None) -> None:
        signal_id = _stable_id("as", ev.group_id, signal_type)
        basis, current, baseline = self._signal_values(ev, signal_type, details)
        change = current - baseline if current is not None and baseline is not None else None
        ev.signals.append(AnomalySignal(
            signal_id=signal_id,
            anomaly_group_id=ev.group_id,
            signal_type=signal_type,
            signal_role=role,
            severity=severity,
            direction=direction,
            comparison_basis=basis,
            observed_value=_fmt(observed) or "0",
            threshold_value=threshold_text or (_fmt(threshold) or "0"),
            current_value=_fmt(current),
            baseline_value=_fmt(baseline),
            change_amount=_fmt(change),
            display_value=self._display_value(ev.metric_id, current),
            display_change=self._display_change(ev.metric_id, change, signal_type),
            display_unit=self._display_unit(ev.metric_id),
            details=dict(details),
        ))

    def _signal_values(self, ev: _Evaluation, signal_type: str, details: Mapping[str, Any]) -> tuple[str, Optional[Decimal], Optional[Decimal]]:
        if signal_type == "YOY_THRESHOLD":
            return "YOY", ev.current, _decimal(ev.record.yoy_reference_value)
        if signal_type == "YTD_YOY_THRESHOLD":
            return "YTD", _decimal(ev.record.ytd_current_value), _decimal(ev.record.ytd_reference_value)
        if signal_type == "CONTINUOUS_DECLINE":
            base = ev.current - (_decimal(details.get("cumulative_change_amount")) or Decimal("0"))
            return "CONTINUOUS_PERIOD", ev.current, base
        if signal_type == "ROLLING_MEDIAN_DEVIATION":
            return "EIGHT_WEEK_BASELINE", ev.current, _decimal(details.get("history_median"))
        return "WOW", ev.current, ev.previous

    @staticmethod
    def _display_unit(metric_id: str) -> str:
        if metric_id == "CRM_SALES":
            return "万元"
        if metric_id == "CAPTURE_RATIO":
            return "百分点"
        return "人"

    @staticmethod
    def _display_value(metric_id: str, value: Optional[Decimal]) -> Optional[str]:
        if value is None:
            return None
        if metric_id == "CRM_SALES":
            return f"{_fmt((value / Decimal('10000')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))}万"
        if metric_id == "CAPTURE_RATIO":
            return f"{_fmt((value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))}%"
        return _fmt(value)

    @staticmethod
    def _display_change(metric_id: str, value: Optional[Decimal], signal_type: str = "") -> Optional[str]:
        if value is None:
            return None
        sign = "+" if value > 0 else ""
        if metric_id == "CRM_SALES":
            display = (value / Decimal("10000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return f"{sign}{_fmt(display)}万"
        if metric_id == "CAPTURE_RATIO":
            display = (value * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return f"{sign}{_fmt(display)}个百分点"
        return f"{sign}{_fmt(value)}"

    def _scope(self, ev: _Evaluation) -> str:
        return f"{ev.record.week_id}/{ev.dimension_type}/{ev.dimension_key}/{ev.metric_id}"

    def _status(self, code: str, scope: str, message: str, affected_rule: Optional[str] = None, field: Optional[str] = None, aliases: Sequence[str] = (), details: Optional[Mapping[str, Any]] = None) -> None:
        item = DataQualityStatus(code, scope, message, affected_rule, field, tuple(aliases), dict(details or {}))
        if item not in self._dq:
            self._dq.append(item)

    def _bundle_quality(self, bundle: DetectionInput) -> None:
        for item in bundle.upstream_data_quality_status:
            code = str(item.get("status_code", item.get("code", "UPSTREAM_DATA_QUALITY")))
            scope = str(item.get("scope", "upstream"))
            message = str(item.get("message", "Upstream data quality status"))
            known = {"status_code", "code", "scope", "message", "field", "affected_rule"}
            details = {key: value for key, value in item.items() if key not in known}
            self._status(code, scope, message, item.get("affected_rule"), item.get("field"), details=details)
        total = bundle.mall_sales_total_cells
        missing = bundle.mall_sales_missing_cells
        if total is not None and missing is not None and total > 0:
            if missing >= total:
                self._status("MALL_SALES_ALL_MISSING", "bundle", "All Mall Sales cells are missing; Capture Ratio is N/A")
            elif missing > 0:
                coverage = Decimal(total - missing) / Decimal(total)
                self._status("MALL_SALES_PARTIAL_MISSING", "bundle", "Some Mall Sales cells are missing; valid cells continue", details={"coverage_rate": _fmt(coverage)})

    def _check_region_tier_completeness(self, records: Iterable[MetricRecord]) -> None:
        if not self.config.data.get("enforce_complete_region_member_tier_set", True):
            return
        expected = set(self.config.data.get("expected_region_member_tier_keys", []))
        present = set()
        for record in records:
            dim, key, metric = self._normalise(record)
            if dim == "REGION_MEMBER_TIER" and metric == SALES_METRIC:
                present.add(key)
        for key in sorted(expected - present):
            self._status("INCOMPLETE_REGION_MEMBER_TIER_SET", f"REGION_MEMBER_TIER/{key}", f"Missing required region×member-tier combination: {key}", details={"missing_key": key})

    def _sanitize_context(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        def safe(value: Any, key: str = "") -> Any:
            if _is_pii_key(key):
                return None
            if isinstance(value, Mapping):
                return {str(k): cleaned for k, v in sorted(value.items(), key=lambda item: str(item[0])) if (cleaned := safe(v, str(k))) is not None}
            if isinstance(value, (list, tuple)):
                return [cleaned for item in value if (cleaned := safe(item, key)) is not None][:100]
            if isinstance(value, str):
                return re.sub(r"(?<!\d)\d{8,}(?!\d)", "[REDACTED]", value)
            if isinstance(value, (int, float, Decimal, bool)) or value is None:
                return value
            return str(value)

        return {key: cleaned for key, value in sorted(context.items()) if key in SAFE_CONTEXT_KEYS and (cleaned := safe(value, key)) is not None}

    @staticmethod
    def _safe_ref(value: str) -> str:
        return re.sub(r"(?<!\d)\d{8,}(?!\d)", "[REDACTED]", str(value))


def detect_anomalies(bundle: DetectionInput, config: Optional[DetectorConfig] = None) -> AnomalyBundle:
    return AnomalyDetector(config).detect(bundle)
