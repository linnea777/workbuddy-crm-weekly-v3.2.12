from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .adapters import normalize_crm, normalize_mall
from .models import CalculationError, CalculationRequest, MetricBundle


DISTRICTS = ("N", "S", "W")
CARD_LEVELS = ("银卡", "金卡", "黑卡", "黑钻卡", "星钻卡")
CARD_KEYS = {"银卡": "SILVER", "金卡": "GOLD", "黑卡": "BLACK", "黑钻卡": "BLACK_DIAMOND", "星钻卡": "STAR_DIAMOND"}
DISTRICT_ALIASES = {
    "n": "N", "north": "N", "北区": "N",
    "s": "S", "south": "S", "南区": "S",
    "w": "W", "west": "W", "西区": "W",
}
CARD_ALIASES = {
    "银": "银卡", "银卡": "银卡",
    "金": "金卡", "金卡": "金卡",
    "黑": "黑卡", "黑卡": "黑卡",
    "黑钻": "黑钻卡", "黑钻卡": "黑钻卡",
    "星钻": "星钻卡", "星钻卡": "星钻卡",
}
BRANDS = {
    "Hermes": {
        "store": "HERMES 爱马仕",
        "aliases": ("HERMES 爱马仕", "HERMES", "Hermès", "爱马仕"),
        "cuts": (100_000, 300_000, 500_000, 800_000, 1_000_000),
    },
    "LV": {
        "store": "LOUIS VUITTON 路易威登",
        "aliases": ("LOUIS VUITTON 路易威登", "LOUIS VUITTON", "LV", "路易威登"),
        "cuts": (10_000, 20_000, 30_000, 40_000, 50_000),
    },
    "Dior": {
        "store": "DIOR 迪奥",
        "aliases": ("DIOR 迪奥", "DIOR", "迪奥"),
        "cuts": (10_000, 20_000, 30_000, 40_000, 50_000),
    },
}
EXCLUSIONS = {
    "EXCLUDED_APPLE": {"names": ("Apple", "Apple-S07"), "ids": ()},
    "EXCLUDED_EMPEROR_CINEMA": {"names": ("英皇电影城",), "ids": ("VSS0013140",)},
}
REPORTING_POLICY = {
    "main_anomaly_scopes": [
        "TOTAL:CRM_SALES", "REGION:CRM_SALES", "MEMBER_TIER:CRM_SALES",
        "REGION_MEMBER_TIER:CRM_SALES", "TOTAL:CAPTURE_RATIO", "REGION:CAPTURE_RATIO",
    ],
    "brand_min_change_amount": 50000,
    "brand_min_total_base_share": 0.003,
    "directional_coverage_target": 0.8,
    "initial_brands_per_region": 3,
    "initial_brands_total": 5,
    "max_summary_points": 5,
    "insight_review_threshold": 8,
    "report_hard_limit": None,
    "priority_brands": ["HERMES", "LV", "DIOR"],
}


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date


def _key(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _iso_date(value: date) -> str:
    return value.isoformat()


def _windows(week_start: date) -> dict[str, Window]:
    if week_start.weekday() != 0:
        raise CalculationError("PERIOD_INCOMPLETE", "week_start必须是周一", {"week_start": str(week_start)})
    week_end = week_start + timedelta(days=6)
    iso_year, iso_week, _ = week_start.isocalendar()
    previous_year_start = date.fromisocalendar(iso_year - 1, iso_week, 1)
    previous_year_end = previous_year_start + timedelta(days=6)
    return {
        "current": Window(week_start, week_end),
        "previous": Window(week_start - timedelta(days=7), week_end - timedelta(days=7)),
        "yoy": Window(previous_year_start, previous_year_end),
        "ytd": Window(date(week_end.year, 1, 1), week_end),
        "previous_ytd": Window(date(week_end.year - 1, 1, 1), date(week_end.year - 1, week_end.month, week_end.day)),
    }


def _require(frame: pd.DataFrame, columns: tuple[str, ...], source: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise CalculationError(
            "FIELD_MISSING",
            f"{source}缺少必要字段: {', '.join(missing)}",
            {"file": source, "missing_fields": missing},
        )


def _parse_dates(frame: pd.DataFrame, column: str, source: str, *, required: bool = True) -> None:
    original = frame[column]
    parsed = pd.to_datetime(original, errors="coerce")
    invalid = original.notna() & parsed.isna()
    if required and invalid.any():
        samples = original[invalid].astype(str).head(5).tolist()
        raise CalculationError(
            "DATE_PARSE_FAILED",
            f"{source}.{column}存在无法解析的日期",
            {"field": column, "error_rows": int(invalid.sum()), "samples": samples},
        )
    frame[column] = parsed.dt.date


def _parse_amount(frame: pd.DataFrame, column: str, source: str, *, allow_missing: bool) -> None:
    original = frame[column]
    missing_tokens = original.astype(str).str.strip().str.casefold().isin(("", "nan", "none", "n/a")) | original.isna()
    parsed = pd.to_numeric(original.mask(missing_tokens), errors="coerce")
    invalid = ~missing_tokens & parsed.isna()
    if invalid.any():
        raise CalculationError(
            "FIELD_MISSING",
            f"{source}.{column}存在非数值内容",
            {"field": column, "error_rows": int(invalid.sum()), "samples": original[invalid].astype(str).head(5).tolist()},
        )
    if not allow_missing and parsed.isna().any():
        raise CalculationError("FIELD_MISSING", f"{source}.{column}存在空值", {"field": column})
    frame[column] = parsed


def _exclusion_masks(frame: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
    names = frame["store_name"].map(_key)
    ids = frame.get("store_id", pd.Series("", index=frame.index)).map(_key)
    matches: dict[str, pd.Series] = {}
    for canonical_id, rule in EXCLUSIONS.items():
        name_keys = {_key(value) for value in rule["names"]}
        id_keys = {_key(value) for value in rule["ids"]}
        matches[canonical_id] = names.isin(name_keys) | ids.isin(id_keys)
    return pd.concat(matches.values(), axis=1).any(axis=1), matches


def _prepare(request: CalculationRequest) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    crm = normalize_crm(request.crm)
    mall = normalize_mall(request.mall)
    _require(crm, ("event_time", "district", "store_name", "member_id", "card_level", "registration_time", "paid_amount"), "CRM")
    _require(mall, ("business_date", "district", "store_name", "mall_sales"), "Mall Sales")

    _parse_dates(crm, "event_time", "CRM")
    _parse_dates(crm, "registration_time", "CRM")
    _parse_dates(mall, "business_date", "Mall Sales")
    _parse_amount(crm, "paid_amount", "CRM", allow_missing=False)
    _parse_amount(mall, "mall_sales", "Mall Sales", allow_missing=True)
    if "points_balance" in crm:
        _parse_amount(crm, "points_balance", "CRM", allow_missing=True)

    crm["district"] = crm["district"].map(lambda value: DISTRICT_ALIASES.get(_key(value)))
    mall["district"] = mall["district"].map(lambda value: DISTRICT_ALIASES.get(_key(value)))
    if crm["district"].isna().any() or mall["district"].isna().any():
        raise CalculationError("FIELD_MISSING", "街区名称包含未知值，仅支持N/S/W（北/南/西区）")
    crm["card_level"] = crm["card_level"].map(lambda value: CARD_ALIASES.get(_key(value)))
    if crm["card_level"].isna().any():
        raise CalculationError("FIELD_MISSING", "会员卡名称包含未知值")
    crm["store_name"] = crm["store_name"].astype(str).str.strip()
    mall["store_name"] = mall["store_name"].astype(str).str.strip()

    crm_excluded, crm_matches = _exclusion_masks(crm)
    mall_excluded, mall_matches = _exclusion_masks(mall)
    exclusions = []
    for canonical_id in EXCLUSIONS:
        crm_hit = crm_matches[canonical_id]
        mall_hit = mall_matches[canonical_id]
        exclusions.append({
            "canonical_id": canonical_id,
            "crm_rows": int(crm_hit.sum()),
            "crm_amount": float(crm.loc[crm_hit, "paid_amount"].fillna(0).sum()),
            "mall_rows": int(mall_hit.sum()),
            "mall_amount": float(mall.loc[mall_hit, "mall_sales"].fillna(0).sum()),
        })
    crm = crm.loc[~crm_excluded].copy()
    mall = mall.loc[~mall_excluded].copy()

    quality: list[dict[str, Any]] = []
    for district, group in mall.groupby("district", sort=False):
        missing = group["mall_sales"].isna()
        if not missing.any():
            continue
        valid_count = int((~missing).sum())
        missing_count = int(missing.sum())
        status = "MALL_SALES_PARTIAL_MISSING" if valid_count else "MALL_SALES_ALL_MISSING"
        quality.append({
            "status_code": status,
            "level": "WARNING",
            "scope": "mall_sales",
            "dimension": district,
            "affected_dates": sorted({_iso_date(value) for value in group.loc[missing, "business_date"]}),
            "affected_stores": int(group.loc[missing, "store_name"].nunique()),
            "missing_count": missing_count,
            "valid_count": valid_count,
            "coverage_rate": round(valid_count / len(group), 4),
            "message": "Mall Sales缺失单元格已跳过，未按0处理",
        })
    if "points_balance" not in crm:
        quality.append({
            "status_code": "MISSING_REQUIRED_FIELD",
            "level": "WARNING",
            "scope": "M09",
            "field": "points_balance",
            "message": "缺少剩余积分字段，仅M09不可计算",
        })

    summary = {
        "crm_input_rows": int(len(request.crm)),
        "crm_valid_rows": int(len(crm)),
        "mall_input_rows": int(len(request.mall)),
        "mall_valid_rows": int(len(mall)),
        "exclusions": exclusions,
        "mall_sales_total_cells": int(len(mall)),
        "mall_sales_missing_cells": int(mall["mall_sales"].isna().sum()),
    }
    return crm, mall, summary, quality


def _slice(frame: pd.DataFrame, date_column: str, window: Window) -> pd.DataFrame:
    return frame.loc[frame[date_column].between(window.start, window.end, inclusive="both")]


def _positive(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.loc[frame["paid_amount"] > 0]


def _number(value: Any) -> float | int | None:
    if value is None or pd.isna(value):
        return None
    result = float(value)
    return int(result) if result.is_integer() else result


def _change(current: float | int | None, base: float | int | None) -> dict[str, Any]:
    if current is None or base is None:
        return {"current": _number(current), "base": _number(base), "change_amount": None, "change_rate_percent": None}
    amount = current - base
    return {
        "current": _number(current),
        "base": _number(base),
        "change_amount": _number(amount),
        "change_rate_percent": None if base == 0 else float(((Decimal(str(current)) - Decimal(str(base))) / Decimal(str(base)) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)),
    }


def _sales(frame: pd.DataFrame) -> float:
    return float(frame["paid_amount"].sum())


def _crm_sales_by(frame: pd.DataFrame, window: Window, column: str) -> dict[str, float]:
    data = _slice(frame, "event_time", window)
    return data.groupby(column, observed=True)["paid_amount"].sum().astype(float).to_dict()


def _mall_sales_by(frame: pd.DataFrame, window: Window, column: str) -> dict[str, float | None]:
    data = _slice(frame, "business_date", window)
    grouped = data.groupby(column, observed=True)["mall_sales"]
    return {key: (None if values.notna().sum() == 0 else float(values.sum())) for key, values in grouped}


def _district_values(crm: pd.DataFrame, window: Window) -> dict[str, float | None]:
    if _slice(crm, "event_time", window).empty:
        return {dimension: None for dimension in (*DISTRICTS, "Total")}
    values = _crm_sales_by(crm, window, "district")
    result = {district: values.get(district, 0.0) for district in DISTRICTS}
    result["Total"] = sum(result.values())
    return result


def _district_comparison(crm: pd.DataFrame, current: Window, base: Window) -> list[dict[str, Any]]:
    current_values = _district_values(crm, current)
    base_values = _district_values(crm, base)
    return [{"dimension": key, **_change(current_values[key], base_values[key])} for key in (*DISTRICTS, "Total")]


def _m04_card_sales(crm: pd.DataFrame, current: Window, previous: Window) -> list[dict[str, Any]]:
    current_values = _crm_sales_by(crm, current, "card_level")
    previous_values = _crm_sales_by(crm, previous, "card_level")
    current_available = not _slice(crm, "event_time", current).empty
    previous_available = not _slice(crm, "event_time", previous).empty
    current_values["Total"] = sum(current_values.get(level, 0) for level in CARD_LEVELS)
    previous_values["Total"] = sum(previous_values.get(level, 0) for level in CARD_LEVELS)
    return [
        {
            "card_level": level,
            **_change(
                current_values.get(level, 0) if current_available else None,
                previous_values.get(level, 0) if previous_available else None,
            ),
        }
        for level in (*CARD_LEVELS, "Total")
    ]


def _m05_district_card(crm: pd.DataFrame, current: Window, previous: Window) -> list[dict[str, Any]]:
    def grouped(window: Window) -> pd.Series:
        return _slice(crm, "event_time", window).groupby(["district", "card_level"])["paid_amount"].sum()

    now, before = grouped(current), grouped(previous)
    previous_available = not _slice(crm, "event_time", previous).empty
    rows = []
    for (district, card_level), value in now.items():
        base = float(before.get((district, card_level), 0)) if previous_available else None
        rows.append({"district": district, "card_level": card_level, **_change(float(value), base)})
    return sorted(rows, key=lambda row: (-row["current"], row["district"], row["card_level"]))[:5]


def _new_members(crm: pd.DataFrame, window: Window) -> dict[str, int] | None:
    period = _slice(crm, "event_time", window)
    if period.empty:
        return None
    data = _positive(period)
    data = data.loc[data["registration_time"].between(window.start, window.end, inclusive="both")]
    result = data.groupby("card_level")["member_id"].nunique().to_dict()
    values = {level: int(result.get(level, 0)) for level in CARD_LEVELS}
    values["Total"] = int(data["member_id"].nunique())
    return values


def _m06_new_members(crm: pd.DataFrame, current: Window, previous: Window) -> list[dict[str, Any]]:
    now, before = _new_members(crm, current), _new_members(crm, previous)
    return [
        {
            "card_level": level,
            **_change(now[level] if now is not None else None, before[level] if before is not None else None),
        }
        for level in (*CARD_LEVELS, "Total")
    ]


def _m07_store_top10(crm: pd.DataFrame, current: Window, previous: Window) -> list[dict[str, Any]]:
    now = _positive(_slice(crm, "event_time", current)).groupby("store_name")["paid_amount"].agg(["sum", "count"])
    before = _crm_sales_by(crm, previous, "store_name")
    previous_available = not _slice(crm, "event_time", previous).empty
    rows = []
    for store, values in now.iterrows():
        sales, transactions = float(values["sum"]), int(values["count"])
        rows.append({
            "store_name": store,
            "transactions": transactions,
            "average_ticket": _number(sales / transactions),
            **_change(sales, before.get(store, 0) if previous_available else None),
        })
    return sorted(rows, key=lambda row: (-row["current"], -row["transactions"], row["store_name"]))[:10]


def _m08_member_top10(crm: pd.DataFrame, current: Window) -> list[dict[str, Any]]:
    data = _positive(_slice(crm, "event_time", current))
    months = {(current.start.year, current.start.month), (current.end.year, current.end.month)}
    rows = []
    for member_id, group in data.groupby("member_id", sort=False):
        stores = group.groupby("store_name")["paid_amount"].sum().sort_values(ascending=False, kind="stable")
        stores = stores.reset_index().sort_values(["paid_amount", "store_name"], ascending=[False, True])
        registration = group["registration_time"].dropna().min() if group["registration_time"].notna().any() else None
        rows.append({
            "member_id": str(member_id),
            "member_type": "new" if registration and (registration.year, registration.month) in months else "existing",
            "card_level": group["card_level"].iloc[-1],
            "stores": stores["store_name"].tolist(),
            "sales": float(group["paid_amount"].sum()),
            "transactions": int(len(group)),
        })
    return sorted(rows, key=lambda row: (-row["sales"], -row["transactions"], row["member_id"]))[:10]


def _m09_points(crm: pd.DataFrame, week_end: date) -> list[dict[str, Any]]:
    if "points_balance" not in crm:
        return []
    eligible = crm.loc[(crm["event_time"] <= week_end) & crm["points_balance"].notna()].copy()
    if eligible.empty:
        return []
    latest_dates = eligible.groupby("member_id")["event_time"].transform("max")
    latest = eligible.loc[eligible["event_time"] == latest_dates]
    member_snapshot = latest.groupby("member_id", as_index=False).agg(points_balance=("points_balance", "max"), card_level=("card_level", "last"))
    by_level = member_snapshot.groupby("card_level")["points_balance"].sum().to_dict()
    rows = [{"card_level": level, "points_balance": _number(by_level.get(level, 0))} for level in CARD_LEVELS]
    rows.append({"card_level": "Total", "points_balance": _number(sum(by_level.values()))})
    return rows


def _brand_mask(frame: pd.DataFrame, aliases: tuple[str, ...]) -> pd.Series:
    expected = {_key(value) for value in aliases}
    return frame["store_name"].map(_key).isin(expected)


def _ratio(crm_sales: float | None, mall_sales: float | None) -> float | None:
    return None if crm_sales is None or mall_sales is None or mall_sales == 0 else crm_sales / mall_sales


def _segments(data: pd.DataFrame, cuts: tuple[int, ...]) -> list[dict[str, Any]]:
    per_member = data.groupby("member_id")["paid_amount"].sum().sort_index()
    transactions_per_member = data.loc[data.paid_amount.gt(0)].groupby("member_id").size().reindex(per_member.index, fill_value=0)
    per_member=per_member.loc[per_member.gt(0)]
    labels = [f"≤{cuts[0]}"] + [f"{low}–{high}" for low, high in zip(cuts, cuts[1:])] + [f">{cuts[-1]}"]
    bins = [-math.inf, *cuts, math.inf]
    categories = pd.cut(per_member, bins=bins, labels=labels, right=True)
    total_members, total_sales = int(len(per_member)), float(per_member.sum())
    rows = []
    for label in labels:
        values = per_member.loc[categories == label]
        sales = float(values.sum())
        rows.append({
            "segment": label,
            "members": int(len(values)),
            "transactions": int(transactions_per_member.loc[values.index].sum()),
            "member_share": None if total_members == 0 else len(values) / total_members,
            "sales": sales,
            "sales_share": None if total_sales == 0 else sales / total_sales,
        })
    return rows


def _brand_metrics(crm: pd.DataFrame, mall: pd.DataFrame, current: Window, previous: Window, name: str, config: dict[str, Any]) -> dict[str, Any]:
    crm_brand = crm.loc[_brand_mask(crm, config["aliases"])]
    mall_brand = mall.loc[_brand_mask(mall, config["aliases"])]
    current_all = _slice(crm_brand, "event_time", current)
    previous_all = _slice(crm_brand, "event_time", previous)
    current_crm = _positive(current_all)
    previous_crm = _positive(previous_all)

    def period_summary(data: pd.DataFrame) -> tuple[float, int, float | None]:
        sales, transactions = float(data["paid_amount"].sum()), int(len(data))
        return sales, transactions, None if transactions == 0 else sales / transactions

    expected_regions=sorted(set(crm_brand.district))
    coverage=crm.attrs.get("coverage",{}).get("crm",{})
    def available(data,window):
        return not data.empty or bool(expected_regions) and all(coverage.get(window.start.isoformat(),{}).get(r)=="COMPLETE" for r in expected_regions)
    current_available = available(current_all,current)
    previous_available = available(previous_all,previous)
    current_sales, current_tx, current_ticket = period_summary(current_crm)
    previous_sales, previous_tx, previous_ticket = period_summary(previous_crm)
    current_sales = _sales(current_all)
    previous_sales = _sales(previous_all)
    current_ticket=current_sales/current_tx if current_tx else None
    previous_ticket=previous_sales/previous_tx if previous_tx else None
    if not current_available:
        current_sales = current_tx = current_ticket = None
    if not previous_available:
        previous_sales = previous_tx = previous_ticket = None
    current_mall_series = _slice(mall_brand, "business_date", current)["mall_sales"]
    previous_mall_series = _slice(mall_brand, "business_date", previous)["mall_sales"]
    current_mall = None if current_mall_series.notna().sum() == 0 else float(current_mall_series.sum())
    previous_mall = None if previous_mall_series.notna().sum() == 0 else float(previous_mall_series.sum())
    current_ratio, previous_ratio = _ratio(current_sales, current_mall), _ratio(previous_sales, previous_mall)
    return {
        "brand": name,
        "store_name": config["store"],
        "crm_sales": _change(current_sales, previous_sales),
        "transactions": _change(current_tx, previous_tx),
        "average_ticket": _change(current_ticket, previous_ticket),
        "mall_sales": _change(current_mall, previous_mall),
        "capture_ratio": {
            "current": current_ratio,
            "base": previous_ratio,
            "change_pp": None if current_ratio is None or previous_ratio is None else round((current_ratio - previous_ratio) * 100, 2),
        },
        "segments": _segments(current_all, config["cuts"]),
        "segment_definition": "全场该品牌会员自然周净消费；仅正净额会员分段，份额分母为正净额会员销售总额",
        "nonpositive_adjustment": _number(current_all.groupby("member_id").paid_amount.sum().loc[lambda x:x.le(0)].sum()),
    }


def _m12_active(crm: pd.DataFrame, current: Window, previous: Window) -> list[dict[str, Any]]:
    def counts(window: Window) -> dict[str, int] | None:
        period = _slice(crm, "event_time", window)
        if period.empty:
            return None
        data = _positive(period)
        result = data.groupby("card_level")["member_id"].nunique().to_dict()
        result["Total"] = int(data["member_id"].nunique())
        return {key: int(value) for key, value in result.items()}

    now, before = counts(current), counts(previous)
    return [
        {
            "card_level": level,
            **_change(now.get(level, 0) if now is not None else None, before.get(level, 0) if before is not None else None),
        }
        for level in (*CARD_LEVELS, "Total")
    ]


def _m13_capture(crm: pd.DataFrame, mall: pd.DataFrame, current: Window, previous: Window, quality: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_crm, previous_crm = _district_values(crm, current), _district_values(crm, previous)
    current_mall = _mall_sales_by(mall, current, "district")
    previous_mall = _mall_sales_by(mall, previous, "district")
    current_valid = [current_mall.get(key) for key in DISTRICTS if current_mall.get(key) is not None]
    previous_valid = [previous_mall.get(key) for key in DISTRICTS if previous_mall.get(key) is not None]
    current_mall["Total"] = None if not current_valid else sum(current_valid)
    previous_mall["Total"] = None if not previous_valid else sum(previous_valid)

    rows = []
    for dimension in (*DISTRICTS, "Total"):
        current_ratio = _ratio(current_crm[dimension], current_mall.get(dimension))
        previous_ratio = _ratio(previous_crm[dimension], previous_mall.get(dimension))
        for label, denominator in (("current", current_mall.get(dimension)), ("previous", previous_mall.get(dimension))):
            if denominator == 0:
                quality.append({
                    "status_code": "CAPTURE_RATIO_DENOMINATOR_ZERO",
                    "level": "WARNING",
                    "scope": "M13",
                    "dimension": dimension,
                    "window": label,
                    "message": "Mall Sales分母为0，Capture Ratio为N/A",
                })
        rows.append({
            "dimension": dimension,
            "current_crm_sales": _number(current_crm[dimension]),
            "current_mall_sales": _number(current_mall.get(dimension)),
            "previous_crm_sales": _number(previous_crm[dimension]),
            "previous_mall_sales": _number(previous_mall.get(dimension)),
            "current": current_ratio,
            "base": previous_ratio,
            "change_pp": None if current_ratio is None or previous_ratio is None else round((current_ratio - previous_ratio) * 100, 2),
            "change_rate_percent": None if previous_ratio in (None, 0) or current_ratio is None else round((current_ratio - previous_ratio) / previous_ratio * 100, 1),
        })
    return rows


def _mall_period_values(mall: pd.DataFrame, window: Window) -> dict[str, float | None]:
    period = _slice(mall, "business_date", window)
    values: dict[str, float | None] = {}
    for district in DISTRICTS:
        series = period.loc[period["district"] == district, "mall_sales"]
        values[district] = None if series.notna().sum() == 0 else float(series.sum())
    valid = [values[district] for district in DISTRICTS if values[district] is not None]
    values["Total"] = None if not valid else sum(float(value) for value in valid)
    return values


def _m14_mall_sales(mall: pd.DataFrame, current: Window, previous: Window, yoy: Window) -> list[dict[str, Any]]:
    current_values = _mall_period_values(mall, current)
    previous_values = _mall_period_values(mall, previous)
    yoy_values = _mall_period_values(mall, yoy)
    rows = []
    for dimension in (*DISTRICTS, "Total"):
        wow = _change(current_values[dimension], previous_values[dimension])
        yoy_change = _change(current_values[dimension], yoy_values[dimension])
        rows.append({
            "dimension": dimension,
            "current": wow["current"],
            "previous": wow["base"],
            "wow_change_amount": wow["change_amount"],
            "wow_change_rate_percent": wow["change_rate_percent"],
            "yoy_base": yoy_change["base"],
            "yoy_change_amount": yoy_change["change_amount"],
            "yoy_change_rate_percent": yoy_change["change_rate_percent"],
        })
    return rows


def _week_id(value: date) -> str:
    iso_year, iso_week, _ = value.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _rate_decimal(percent: Any) -> float | None:
    return None if percent is None else float(percent) / 100


def _brand_key(name: Any) -> str:
    # Brand identity is intentionally strict in v3: trim surrounding whitespace
    # but preserve the exact source spelling/case.  A visually similar Mall name
    # must not be silently merged with the CRM entity.
    return str(name or "").strip()


def _weekly_history(crm: pd.DataFrame, current: Window, mask: pd.Series) -> list[dict[str, Any]]:
    return []  # Populated from the shared weekly index by apply_contract.


def _change_status(current: float | None, previous: float | None) -> str:
    if current is None or previous is None or current == previous:
        return "STABLE"
    return "UP" if current > previous else "DOWN"


def _operating_context(
    crm_current: float | None,
    crm_previous: float | None,
    mall_current: float | None,
    mall_previous: float | None,
) -> dict[str, Any]:
    crm_rate = _change(crm_current, crm_previous)["change_rate_percent"]
    mall_rate = _change(mall_current, mall_previous)["change_rate_percent"]
    crm_status = _change_status(crm_current, crm_previous)
    mall_status = _change_status(mall_current, mall_previous)
    if crm_status == "STABLE":
        relation = "CRM_STABLE"
    elif mall_status == "STABLE":
        relation = "MALL_STABLE"
    else:
        relation = "SAME" if crm_status == mall_status else "OPPOSITE"
    if crm_rate is None or mall_rate is None:
        magnitude = "NOT_APPLICABLE"
        gap = None
    else:
        gap = round(crm_rate - mall_rate, 2)
        magnitude = "ALIGNED" if abs(gap) <= 1 else ("CRM_STRONGER" if abs(crm_rate) > abs(mall_rate) else "MALL_STRONGER")
    current_ratio = _ratio(crm_current, mall_current)
    previous_ratio = _ratio(crm_previous, mall_previous)
    return {
        "mall_sales": {"current": _number(mall_current), "previous": _number(mall_previous)},
        "capture_ratio": {
            "current": current_ratio,
            "previous": previous_ratio,
            "change_pp": None if current_ratio is None or previous_ratio is None else round((current_ratio - previous_ratio) * 100, 2),
        },
        "crm_change_rate": crm_rate,
        "mall_change_rate": mall_rate,
        "crm_status": crm_status,
        "mall_status": mall_status,
        "direction_relation": relation,
        "change_rate_gap_pp": gap,
        "magnitude_relation": magnitude,
        "capture_status": _change_status(current_ratio, previous_ratio),
    }


def _large_spend_bases(crm_brand: pd.DataFrame, windows: dict[str, Window]) -> list[str]:
    def has_large(window: Window) -> bool:
        period = _slice(crm_brand, "event_time", window)
        return bool((period.groupby("member_id")["paid_amount"].sum() >= 100_000).any())

    bases = []
    if has_large(windows["current"]) or has_large(windows["previous"]):
        bases.append("WOW")
    if has_large(windows["current"]) or has_large(windows["yoy"]):
        bases.append("YOY")
    recent = [
        Window(windows["current"].start - timedelta(days=7 * offset), windows["current"].end - timedelta(days=7 * offset))
        for offset in range(1, 9)
    ]
    if has_large(windows["current"]) or any(has_large(window) for window in recent):
        bases.extend(("CONTINUOUS_PERIOD", "EIGHT_WEEK_BASELINE"))
    return bases


def _amount_band_comparison(crm_brand: pd.DataFrame, crm_all: pd.DataFrame, windows: dict[str, Window], cuts: tuple[int, ...]) -> list[dict[str, Any]]:
    current_available = not _slice(crm_all, "event_time", windows["current"]).empty
    previous_available = not _slice(crm_all, "event_time", windows["previous"]).empty
    yoy_available = not _slice(crm_all, "event_time", windows["yoy"]).empty
    current = {row["segment"]: row for row in _segments(_slice(crm_brand, "event_time", windows["current"]), cuts)}
    previous = {row["segment"]: row for row in _segments(_slice(crm_brand, "event_time", windows["previous"]), cuts)}
    yoy = {row["segment"]: row for row in _segments(_slice(crm_brand, "event_time", windows["yoy"]), cuts)}
    history = []
    for offset in range(8, 0, -1):
        week = Window(windows["current"].start - timedelta(days=7 * offset), windows["current"].end - timedelta(days=7 * offset))
        if _slice(crm_all, "event_time", week).empty:
            continue
        history.append({row["segment"]: row for row in _segments(_slice(crm_brand, "event_time", week), cuts)})
    rows = []
    for segment in current:
        now, before, last_year = current[segment], previous[segment], yoy[segment]
        rows.append({
            "segment": segment,
            "current_members": now["members"] if current_available else None,
            "previous_members": before["members"] if previous_available else None,
            "member_change": now["members"] - before["members"] if current_available and previous_available else None,
            "current_transactions": now["transactions"] if current_available else None,
            "previous_transactions": before["transactions"] if previous_available else None,
            "transaction_change": now["transactions"] - before["transactions"] if current_available and previous_available else None,
            "current_crm_sales": _number(now["sales"]) if current_available else None,
            "previous_crm_sales": _number(before["sales"]) if previous_available else None,
            "wow_change_amount": _number(now["sales"] - before["sales"]) if current_available and previous_available else None,
            "yoy_crm_sales": _number(last_year["sales"]) if yoy_available else None,
            "yoy_change_amount": _number(now["sales"] - last_year["sales"]) if current_available and yoy_available else None,
            "eight_week_median_members": _number(pd.Series([week[segment]["members"] for week in history]).median()) if len(history) == 8 else None,
            "eight_week_median_transactions": _number(pd.Series([week[segment]["transactions"] for week in history]).median()) if len(history) == 8 else None,
            "eight_week_median_crm_sales": _number(pd.Series([week[segment]["sales"] for week in history]).median()) if len(history) == 8 else None,
        })
    return rows


def _controlled_agent_records(
    crm: pd.DataFrame,
    mall: pd.DataFrame,
    windows: dict[str, Window],
    metrics: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current, previous = windows["current"], windows["previous"]
    current_week_id = _week_id(current.start)
    records: list[dict[str, Any]] = []

    def add_record(**record: Any) -> None:
        record.setdefault("week_id", current_week_id)
        record.setdefault("history", [])
        record.setdefault("attribution_context", {})
        record.setdefault("has_large_spend_signal", False)
        record.setdefault("large_spend_signal_bases", [])
        records.append(record)

    wow_by_dimension = {row["dimension"]: row for row in metrics["M01"]}
    yoy_by_dimension = {row["dimension"]: row for row in metrics["M02"]}
    ytd_by_dimension = {row["dimension"]: row for row in metrics["M03"]}
    mall_by_dimension = {row["dimension"]: row for row in metrics["M14"]}
    for dimension in (*DISTRICTS, "Total"):
        wow, yoy, ytd, mall_row = wow_by_dimension[dimension], yoy_by_dimension[dimension], ytd_by_dimension[dimension], mall_by_dimension[dimension]
        mask = pd.Series(True, index=crm.index) if dimension == "Total" else crm["district"].eq(dimension)
        context = _operating_context(wow["current"], wow["base"], mall_row["current"], mall_row["previous"])
        add_record(
            dimension_type="TOTAL" if dimension == "Total" else "REGION",
            dimension_key="TOTAL" if dimension == "Total" else dimension,
            metric_id="CRM_SALES",
            current_value=wow["current"],
            previous_value=wow["base"],
            wow_rate=_rate_decimal(wow["change_rate_percent"]),
            yoy_reference_value=yoy["base"],
            yoy_rate=_rate_decimal(yoy["change_rate_percent"]),
            ytd_current_value=ytd["current"],
            ytd_reference_value=ytd["base"],
            ytd_yoy_rate=_rate_decimal(ytd["change_rate_percent"]),
            history=_weekly_history(crm, current, mask),
            source_record_id=f"metric:{current_week_id}:{dimension}:CRM_SALES",
            attribution_context=context,
        )

    for metric_id, rows in (("NEW_REGISTRATIONS", metrics["M06"]), ("ACTIVE_MEMBERS", metrics["M12"])):
        for row in rows:
            if row["card_level"] not in CARD_KEYS:
                continue
            add_record(
                dimension_type="MEMBER_TIER", dimension_key=CARD_KEYS[row["card_level"]], metric_id=metric_id,
                current_value=row["current"], previous_value=row["base"], wow_rate=_rate_decimal(row["change_rate_percent"]),
                source_record_id=f"metric:{current_week_id}:{CARD_KEYS[row['card_level']]}:{metric_id}",
            )

    current_rt = _slice(crm, "event_time", current).groupby(["district", "card_level"])["paid_amount"].sum()
    previous_rt = _slice(crm, "event_time", previous).groupby(["district", "card_level"])["paid_amount"].sum()
    for district in DISTRICTS:
        for level in CARD_LEVELS:
            now, before = float(current_rt.get((district, level), 0)), float(previous_rt.get((district, level), 0))
            add_record(
                dimension_type="REGION_MEMBER_TIER", dimension_key=f"{district}|{CARD_KEYS[level]}", metric_id="CRM_SALES",
                current_value=_number(now), previous_value=_number(before),
                wow_rate=None if before == 0 else (now - before) / before,
                source_record_id=f"metric:{current_week_id}:{district}:{CARD_KEYS[level]}:CRM_SALES",
            )

    for row in metrics["M13"]:
        dimension = row["dimension"]
        add_record(
            dimension_type="TOTAL" if dimension == "Total" else "REGION", dimension_key="TOTAL" if dimension == "Total" else dimension,
            metric_id="CAPTURE_RATIO", current_value=row["current"], previous_value=row["base"],
            capture_ratio_denominator_zero=row["current_mall_sales"] == 0,
            source_record_id=f"metric:{current_week_id}:{dimension}:CAPTURE_RATIO",
            attribution_context={"mall_sales": {"current": row["current_mall_sales"], "previous": row["previous_mall_sales"]}, "capture_ratio": {"current": row["current"], "previous": row["base"], "change_pp": row["change_pp"]}},
        )

    crm = crm.copy()
    mall = mall.copy()
    crm["brand_key"] = crm["store_name"].map(_brand_key)
    mall["brand_key"] = mall["store_name"].map(_brand_key)
    luxury_context: list[dict[str, Any]] = []
    brand_keys = sorted(set(crm.loc[crm["event_time"].between(current.start - timedelta(days=7 * 26), current.end), "brand_key"]))
    for brand in brand_keys:
        crm_mask = crm["brand_key"].eq(brand)
        mall_mask = mall["brand_key"].eq(brand)
        crm_brand, mall_brand = crm.loc[crm_mask], mall.loc[mall_mask]
        now = _sales(_slice(crm_brand, "event_time", current))
        before = _sales(_slice(crm_brand, "event_time", previous))
        yoy_value = _sales(_slice(crm_brand, "event_time", windows["yoy"]))
        current_mall_series = _slice(mall_brand, "business_date", current)["mall_sales"]
        previous_mall_series = _slice(mall_brand, "business_date", previous)["mall_sales"]
        current_mall = None if current_mall_series.notna().sum() == 0 else float(current_mall_series.sum())
        previous_mall = None if previous_mall_series.notna().sum() == 0 else float(previous_mall_series.sum())
        context = _operating_context(now, before, current_mall, previous_mall)
        config = next((cfg for cfg in BRANDS.values() if _key(brand) in {_key(alias) for alias in cfg["aliases"]}), None)
        if config:
            amount_context = {"brand": brand, "bands": _amount_band_comparison(crm_brand, crm, windows, config["cuts"])}
            context["high_luxury_amount_band_context"] = amount_context
            luxury_context.append(amount_context)
        bases = _large_spend_bases(crm_brand, windows)
        add_record(
            dimension_type="BRAND", dimension_key=brand, metric_id="CRM_SALES",
            current_value=_number(now), previous_value=_number(before),
            wow_rate=None if before == 0 else (now - before) / before,
            yoy_reference_value=_number(yoy_value), yoy_rate=None if yoy_value == 0 else (now - yoy_value) / yoy_value,
            history=_weekly_history(crm, current, crm_mask),
            source_record_id=f"metric:{current_week_id}:BRAND:{brand}:CRM_SALES",
            attribution_context=context,
            has_large_spend_signal=bool(bases), large_spend_signal_bases=bases,
        )
    return records, luxury_context


def calculate_metrics(request: CalculationRequest, *, prepared=None) -> MetricBundle:
    """Validate inputs once and deterministically calculate M01-M14."""
    windows = _windows(request.week_start)
    crm, mall, summary, quality = prepared if prepared is not None else _prepare(request)
    current, previous = windows["current"], windows["previous"]

    for source, frame, date_column in (("CRM", crm, "event_time"), ("Mall Sales", mall, "business_date")):
        for window_name, window in windows.items():
            if _slice(frame, date_column, window).empty:
                quality.append({
                    "status_code": "PERIOD_INCOMPLETE",
                    "level": "WARNING",
                    "scope": source,
                    "window": window_name,
                    "required_start": _iso_date(window.start),
                    "required_end": _iso_date(window.end),
                    "message": "该比较窗口没有记录，相关指标返回N/A",
                })

    metrics = {
        "M01": _district_comparison(crm, current, previous),
        "M02": _district_comparison(crm, current, windows["yoy"]),
        "M03": _district_comparison(crm, windows["ytd"], windows["previous_ytd"]),
        "M04": _m04_card_sales(crm, current, previous),
        "M05": _m05_district_card(crm, current, previous),
        "M06": _m06_new_members(crm, current, previous),
        "M07": _m07_store_top10(crm, current, previous),
        "M08": _m08_member_top10(crm, current),
        "M09": _m09_points(crm, current.end),
        "M10": _brand_metrics(crm, mall, current, previous, "Hermes", BRANDS["Hermes"]),
        "M11": [_brand_metrics(crm, mall, current, previous, name, BRANDS[name]) for name in ("LV", "Dior")],
        "M12": _m12_active(crm, current, previous),
    }
    metrics["M13"] = _m13_capture(crm, mall, current, previous, quality)
    metrics["M14"] = _m14_mall_sales(mall, current, previous, windows["yoy"])
    from .coverage import apply_contract
    anomaly_records, luxury_context = _controlled_agent_records(crm, mall, windows, metrics)
    anomaly_records = apply_contract(crm, mall, windows, metrics, anomaly_records, request.coverage, quality)
    serialized_windows = {name: {"start": _iso_date(window.start), "end": _iso_date(window.end)} for name, window in windows.items()}
    return MetricBundle(
        week_id=f"{current.start.isoformat()}_{current.end.isoformat()}",
        windows=serialized_windows,
        metrics=metrics,
        data_quality_status=quality,
        run_summary=summary,
        anomaly_records=anomaly_records,
        high_luxury_amount_band_context=luxury_context,
        reporting_policy=dict(REPORTING_POLICY),
    )
