from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

import pandas as pd

from crm_weekly_metrics.calculator import CARD_KEYS, Window, BRANDS, _key
from crm_weekly_metrics.coverage import scope_value


IMPLEMENTATION_VERSION = "3.2.2"
DISTRICTS = ("N", "S", "W")


def _brand_key(value: Any) -> str:
    """Preserve the exact source name; only surrounding whitespace is ignored."""
    return str(value or "").strip()


def align_known_mall_names(crm, mall):
    """Reuse declared luxury aliases only for an unambiguous regional store pair."""
    mall = mall.copy()
    mappings = []
    for region in DISTRICTS:
        crm_names = set(crm.loc[crm.district.eq(region), 'store_name'])
        mall_names = set(mall.loc[mall.district.eq(region), 'store_name'])
        for config in BRANDS.values():
            keys = {_key(n) for n in config['aliases']}
            left = [n for n in crm_names if _key(n) in keys]
            right = [n for n in mall_names if _key(n) in keys]
            if len(left) == len(right) == 1 and left[0] != right[0]:
                mask = mall.district.eq(region) & mall.store_name.eq(right[0])
                mall.loc[mask, 'store_name'] = left[0]
                mappings.append({'region':region, 'source_name':right[0], 'crm_name':left[0], 'basis':'DECLARED_BRAND_ALIAS'})
    return mall, mappings


def _window(metric_bundle: dict[str, Any], name: str) -> tuple[Any, Any]:
    raw = metric_bundle["windows"][name]
    return pd.Timestamp(raw["start"]).date(), pd.Timestamp(raw["end"]).date()


def _slice(frame: pd.DataFrame, date_column: str, bounds: tuple[Any, Any]) -> pd.DataFrame:
    return frame.loc[frame[date_column].between(bounds[0], bounds[1], inclusive="both")]


def _number(value: Any) -> int | float | None:
    if value is None or pd.isna(value):
        return None
    result = float(value)
    return int(result) if result.is_integer() else result


def _record_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20].upper()
    return f"{prefix}-{digest}"


def _member_type(registration_time: Any, bounds: tuple[Any, Any]) -> str:
    if registration_time is None or pd.isna(registration_time):
        return "UNKNOWN"
    registration_date = pd.Timestamp(registration_time).date()
    if bounds[0] <= registration_date <= bounds[1]:
        return "NEW"
    return "EXISTING" if registration_date < bounds[0] else "UNKNOWN"


def _member_period_facts(
    crm: pd.DataFrame,
    windows: dict[str, tuple[Any, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    status: dict[str, dict[str, Any]] = {}
    group_columns = ["member_id", "brand_key", "district", "card_key"]
    for period, bounds in windows.items():
        data = _slice(crm, "event_time", bounds).copy()
        status[period] = {
            "available": not data.empty,
            "start_date": bounds[0].isoformat(),
            "end_date": bounds[1].isoformat(),
        }
        if data.empty:
            continue
        for key, group in data.groupby(group_columns, sort=True, dropna=False):
            member_id, brand, district, card = (str(item or "").strip() for item in key)
            registration = group["registration_time"].dropna()
            registration_time = registration.min() if not registration.empty else None
            payload = {
                "period": period,
                "member_id": member_id,
                "brand": brand,
                "region": district,
                "card_tier": card,
            }
            records.append(
                {
                    "member_record_id": _record_id("MW", payload),
                    **payload,
                    "period_start": bounds[0].isoformat(),
                    "period_end": bounds[1].isoformat(),
                    "member_type": _member_type(registration_time, bounds),
                    "registration_date": None if registration_time is None else pd.Timestamp(registration_time).date().isoformat(),
                    "crm_sales": _number(group["paid_amount"].sum()),
                    "transactions": int((group["paid_amount"] > 0).sum()),
                    "refund_amount": _number(group.loc[group["paid_amount"] < 0, "paid_amount"].sum()),
                }
            )
    records.sort(key=lambda item: (item["period"], item["member_id"], item["brand"], item["region"], item["card_tier"]))
    return records, status


def _member_day_facts(
    crm: pd.DataFrame,
    windows: dict[str, tuple[Any, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Activity queries compare at most eight preceding natural weeks.
    from datetime import timedelta
    start = windows["current"][0] - timedelta(weeks=8)
    end = windows["current"][1]
    data = _slice(crm, "event_time", (start, end)).copy()
    if data.empty:
        return [], {"start_date": start.isoformat(), "end_date": end.isoformat(), "available": False}
    data["date"] = data["event_time"].map(lambda v: pd.Timestamp(v).date().isoformat())
    data["transactions"] = (data["paid_amount"] > 0).astype(int)
    data["refund_amount"] = data["paid_amount"].where(data["paid_amount"] < 0, 0)
    grouped = data.groupby(["date", "member_id", "brand_key", "district", "card_key"], dropna=False, sort=True).agg(
        crm_sales=("paid_amount", "sum"), transactions=("transactions", "sum"), refund_amount=("refund_amount", "sum")).reset_index()
    grouped = grouped.rename(columns={"brand_key": "brand", "district": "region", "card_key": "card_tier"})
    records = grouped.to_dict("records")
    for row in records:
        row["member_record_id"] = _record_id("MD", {k: str(row[k]) for k in ("date", "member_id", "brand", "region", "card_tier")})
    return records, {"start_date": start.isoformat(), "end_date": end.isoformat(), "available": True}


def activity_daily_payload(crm, mall, metric_bundle):
    crm, mall = crm.copy(), mall.copy()
    mall, _ = align_known_mall_names(crm, mall)
    crm['brand_key'] = crm['store_name'].map(_brand_key)
    crm['card_key'] = crm['card_level'].map(CARD_KEYS)
    mall['brand_key'] = mall['store_name'].map(_brand_key)
    windows = {"current": _window(metric_bundle, "current")}
    records, coverage = _member_day_facts(crm, windows)
    start, end = pd.Timestamp(coverage["start_date"]).date(), windows["current"][1]
    data = _slice(mall, "business_date", (start, end)).copy()
    data["date"] = data["business_date"].map(lambda v: pd.Timestamp(v).date().isoformat())
    # Keep a missing store/day missing, even if a duplicated source row has a value.
    grouped = data.groupby(["date", "district", "brand_key"], sort=True, dropna=False)["mall_sales"].agg(
        sales="sum", count="count", rows="size").reset_index()
    day_facts = [{"date": r.date, "region": r.district, "brand": r.brand_key,
                  "mall_sales": _number(r.sales) if r.count == r.rows else None}
                 for r in grouped.itertuples()]
    return records, coverage, day_facts


def _period_record(
    frame: pd.DataFrame,
    *,
    date_column: str,
    amount_column: str,
    windows: dict[str, tuple[Any, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, field in (("current", "current"), ("previous", "previous"), ("yoy", "yoy"), ("ytd", "ytd"), ("previous_ytd", "prior_ytd")):
        series = _slice(frame, date_column, windows[name])[amount_column]
        values[field] = None if series.notna().sum() == 0 else _number(series.sum())
    return values


def _group_records(
    frame: pd.DataFrame,
    group_columns: list[str],
    *,
    date_column: str,
    amount_column: str,
    windows: dict[str, tuple[Any, Any]],
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    grouped = {}
    for period, field in (("current", "current"), ("previous", "previous"), ("yoy", "yoy"), ("ytd", "ytd"), ("previous_ytd", "prior_ytd")):
        data = _slice(frame, date_column, windows[period])
        grouped[field] = data.groupby(group_columns, dropna=False)[amount_column].sum(min_count=1).to_dict()
    output=[]
    for raw in frame[group_columns].drop_duplicates().itertuples(index=False, name=None):
        key=tuple(raw); lookup=key[0] if len(key)==1 else key
        values={field:_number(mapping.get(lookup)) for field,mapping in grouped.items()}
        output.append((key,values))
    return sorted(output,key=lambda item:tuple(str(x) for x in item[0]))


def _profile(crm: pd.DataFrame, bounds: tuple[Any, Any], district: str | None = None) -> dict[str, Any]:
    data = _slice(crm, "event_time", bounds)
    if district is not None:
        data = data.loc[data["district"].eq(district)]
    positive = data.loc[data["paid_amount"] > 0]
    return {"transactions": int(len(positive)), "active_members": int(positive["member_id"].nunique())}


def _member_counts(crm: pd.DataFrame, bounds: tuple[Any, Any], card: str) -> int:
    data = _slice(crm, "event_time", bounds)
    data = data.loc[(data["card_level"].eq(card)) & (data["paid_amount"] > 0)]
    return int(data["member_id"].nunique())


def _transaction_counts(crm: pd.DataFrame, bounds: tuple[Any, Any], mask: pd.Series) -> int:
    data = crm.loc[mask & crm["event_time"].between(bounds[0], bounds[1], inclusive="both")]
    return int((data["paid_amount"] > 0).sum())


def _refund_amounts(crm: pd.DataFrame, windows: dict[str, tuple[Any, Any]], mask: pd.Series) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, field in (("current", "refund_current"), ("previous", "refund_previous"), ("yoy", "refund_yoy")):
        data = crm.loc[mask & crm["event_time"].between(windows[name][0], windows[name][1], inclusive="both")]
        result[field] = _number(data.loc[data["paid_amount"] < 0, "paid_amount"].sum())
    return result


def _add_totals(records: dict[str, dict[str, Any]], keys: Iterable[str]) -> None:
    fields = ("current", "previous", "yoy", "ytd", "prior_ytd")
    records["Total"] = {
        field: None if any(records.get(key, {}).get(field) is None for key in keys) else _number(sum(float(records[key][field]) for key in keys))
        for field in fields
    }


def build_rich_metrics(
    crm: pd.DataFrame,
    mall: pd.DataFrame,
    metric_bundle: dict[str, Any],
    *,
    input_signature: str,
    include_member_days: bool = False,
    include_extended_members: bool = False,
) -> dict[str, Any]:
    """Build deterministic investigation aggregates without exposing transaction rows."""
    windows = {name: _window(metric_bundle, name) for name in ("current", "previous", "yoy", "ytd", "previous_ytd")}
    crm = crm.copy()
    mall = mall.copy()
    mall, brand_name_mappings = align_known_mall_names(crm, mall)
    crm["brand_key"] = crm["store_name"].map(_brand_key)
    mall["brand_key"] = mall["store_name"].map(_brand_key)
    crm["card_key"] = crm["card_level"].map(CARD_KEYS)

    crm_district = {key[0]: values for key, values in _group_records(crm, ["district"], date_column="event_time", amount_column="paid_amount", windows=windows)}
    mall_district = {key[0]: values for key, values in _group_records(mall, ["district"], date_column="business_date", amount_column="mall_sales", windows=windows)}
    _add_totals(crm_district, DISTRICTS)
    _add_totals(mall_district, DISTRICTS)

    cards: dict[str, dict[str, Any]] = {}
    for (card,), values in _group_records(crm, ["card_key"], date_column="event_time", amount_column="paid_amount", windows=windows):
        mask = crm["card_key"].eq(card)
        values.update(
            {
                "transactions": _transaction_counts(crm, windows["current"], mask),
                "previous_transactions": _transaction_counts(crm, windows["previous"], mask),
            }
        )
        cards[str(card)] = values
    _add_totals(cards, sorted(cards))
    cards["Total"]["transactions"] = int(sum(int(cards[key]["transactions"]) for key in cards if key != "Total"))
    cards["Total"]["previous_transactions"] = int(sum(int(cards[key]["previous_transactions"]) for key in cards if key != "Total"))

    district_card = []
    for (district, card), values in _group_records(crm, ["district", "card_key"], date_column="event_time", amount_column="paid_amount", windows=windows):
        mask = crm["district"].eq(district) & crm["card_key"].eq(card)
        values.update(
            {
                "district": district,
                "card": card,
                "transactions": _transaction_counts(crm, windows["current"], mask),
                "previous_transactions": _transaction_counts(crm, windows["previous"], mask),
                "active_members": int(_slice(crm.loc[mask & (crm["paid_amount"] > 0)], "event_time", windows["current"])["member_id"].nunique()),
                "previous_active_members": int(_slice(crm.loc[mask & (crm["paid_amount"] > 0)], "event_time", windows["previous"])["member_id"].nunique()),
            }
        )
        district_card.append(values)

    brands: dict[str, dict[str, Any]] = {}
    for (brand,), values in _group_records(crm, ["brand_key"], date_column="event_time", amount_column="paid_amount", windows=windows):
        mask = crm["brand_key"].eq(brand)
        values.update(
            {
                "transactions": _transaction_counts(crm, windows["current"], mask),
                "previous_transactions": _transaction_counts(crm, windows["previous"], mask),
                **_refund_amounts(crm, windows, mask),
                "mall_data_status": "AVAILABLE" if brand in set(mall["brand_key"]) else "DATA_UNAVAILABLE",
            }
        )
        brands[str(brand)] = values

    stores = [{"store": brand, **values} for brand, values in sorted(brands.items())]
    district_brands = [
        {"district": key[0], "brand": key[1], **values}
        for key, values in _group_records(crm, ["district", "brand_key"], date_column="event_time", amount_column="paid_amount", windows=windows)
    ]
    district_stores = [{"district": item["district"], "store": item["brand"], **{key: value for key, value in item.items() if key not in {"district", "brand"}}} for item in district_brands]

    mall_brand_periods = {name: {} for name in ("current", "previous", "yoy", "ytd", "prior_ytd")}
    for (brand,), values in _group_records(mall, ["brand_key"], date_column="business_date", amount_column="mall_sales", windows=windows):
        for name in mall_brand_periods:
            mall_brand_periods[name][str(brand)] = values.get(name)
    mall_store_periods = {name: {district: {} for district in DISTRICTS} for name in mall_brand_periods}
    mall_district_brand = {name: {district: {} for district in DISTRICTS} for name in mall_brand_periods}
    for (district, brand), values in _group_records(mall, ["district", "brand_key"], date_column="business_date", amount_column="mall_sales", windows=windows):
        for name in mall_brand_periods:
            mall_store_periods[name][district][str(brand)] = values.get(name)
            mall_district_brand[name][district][str(brand)] = values.get(name)

    period_profile = {
        period: {**{district: _profile(crm, bounds, district) for district in DISTRICTS}, "Total": _profile(crm, bounds)}
        for period, bounds in (("current", windows["current"]), ("previous", windows["previous"]))
    }
    active = {
        CARD_KEYS[level]: {
            "current": _member_counts(crm, windows["current"], level),
            "previous": _member_counts(crm, windows["previous"], level),
        }
        for level in CARD_KEYS
    }
    new_recruited = {}
    for level, card in CARD_KEYS.items():
        values = {}
        for name in ("current", "previous"):
            data = _slice(crm, "event_time", windows[name])
            data = data.loc[(data["card_level"].eq(level)) & (data["paid_amount"] > 0)]
            data = data.loc[data["registration_time"].between(windows[name][0], windows[name][1], inclusive="both")]
            values[name] = int(data["member_id"].nunique())
        new_recruited[card] = values

    member_windows = dict(windows) if include_extended_members else {k:v for k,v in windows.items() if k in ("current","previous","yoy")}
    if "previous_ytd" in member_windows: member_windows["prior_ytd"] = member_windows.pop("previous_ytd")
    member_week_facts, member_period_status = _member_period_facts(crm, member_windows)
    for period, bounds in member_windows.items():
        member_period_status[period]["regions"] = {r: scope_value(crm,"event_time","paid_amount",Window(*bounds),region=r) is not None for r in DISTRICTS}
    member_day_facts, member_day_coverage, mall_day_facts = activity_daily_payload(crm, mall, metric_bundle) if include_member_days else ([], {"available":False,"status":"NOT_MATERIALIZED","reason":"Activity window not requested"}, [])
    # Keep operating aggregates aligned with the coverage contract.
    for source, records, frame, col, amount in [("crm",crm_district,crm,"event_time","paid_amount"),("mall",mall_district,mall,"business_date","mall_sales")]:
        for reg in (*DISTRICTS,"Total"):
            records.setdefault(reg,{})
            for period,field in (("current","current"),("previous","previous"),("yoy","yoy"),("ytd","ytd"),("previous_ytd","prior_ytd")):
                records[reg][field]=scope_value(frame,col,amount,Window(*windows[period]),source=source,region=None if reg=="Total" else reg)
    for row in district_brands:
        for period,field in (("current","current"),("previous","previous"),("yoy","yoy"),("ytd","ytd"),("previous_ytd","prior_ytd")):
            row[field]=scope_value(crm,"event_time","paid_amount",Window(*windows[period]),region=row["district"],brand=row["brand"])


    # Common-store WOW operating context; partial Mall never divides full CRM.
    comparable={}
    scopes=[("TOTAL",None,None)]+[("REGION:"+r,r,None) for r in DISTRICTS]
    scopes += [("BRAND:"+b,None,b) for b in brands]
    scopes += [("BRAND:"+row["brand"]+"@"+row["district"],row["district"],row["brand"]) for row in district_brands]
    for label,reg,brand in scopes:
        universe=[row for row in district_brands if (reg is None or row["district"]==reg) and (brand is None or row["brand"]==brand)]
        matched=[]
        for row in universe:
            mc=scope_value(mall,"business_date","mall_sales",Window(*windows["current"]),source="mall",region=row["district"],brand=row["brand"],require_complete=True)
            mp=scope_value(mall,"business_date","mall_sales",Window(*windows["previous"]),source="mall",region=row["district"],brand=row["brand"],require_complete=True)
            if all(v is not None for v in (row.get("current"),row.get("previous"),mc,mp)):
                matched.append({"region":row["district"],"brand":row["brand"],"crm_current":row["current"],"crm_previous":row["previous"],"mall_current":mc,"mall_previous":mp})
        comparable[label]={"scope":"COMMON_STORES_TWO_PERIODS","full_coverage":bool(universe) and len(matched)==len(universe),"store_count":len(matched),"expected_store_count":len(universe),"stores":[{k:x[k] for k in ("region","brand")} for x in matched],**{field:sum(x[field] for x in matched) if matched else None for field in ("crm_current","crm_previous","mall_current","mall_previous")}}

    metric_total = next(item for item in metric_bundle["metrics"]["M01"] if item["dimension"] == "Total")["current"]
    rich_total = crm_district["Total"]["current"]
    if (metric_total is None) != (rich_total is None) or (metric_total is not None and abs(float(metric_total) - float(rich_total)) > 0.01):
        raise ValueError("MetricBundle与RichMetricsBundle的Total CRM Sales不一致")

    return {
        "schema_version": "3.0",
        "week_id": metric_bundle["week_id"],
        "input_signature": input_signature,
        "implementation_version": IMPLEMENTATION_VERSION,
        "report": {
            "current": [value.isoformat() for value in windows["current"]],
            "previous": [value.isoformat() for value in windows["previous"]],
            "yoy": [value.isoformat() for value in windows["yoy"]],
            "ytd": [value.isoformat() for value in windows["ytd"]],
            "prior_ytd": [value.isoformat() for value in windows["previous_ytd"]],
            "timezone": "Asia/Shanghai",
        },
        "crm": {
            "district": crm_district,
            "cards": cards,
            "district_card": district_card,
            "brands": brands,
            "stores": stores,
            "district_stores": district_stores,
            "district_brands": district_brands,
            "active": active,
            "new_recruited": new_recruited,
            "period_profile": period_profile,
            "member_week_facts": member_week_facts,
            "member_period_status": member_period_status,
            "member_day_facts": member_day_facts,
            "member_day_coverage": member_day_coverage,
        },
        "mall": {
            "brand_name_mappings": brand_name_mappings,
            "day_facts": mall_day_facts,
            "district": mall_district,
            "brand": mall_brand_periods,
            "store": mall_store_periods,
            "district_brand": mall_district_brand,
            "quality": metric_bundle.get("data_quality_status", []),
        },
        "capture": {str(item["dimension"]): dict(item) for item in metric_bundle["metrics"]["M13"]},
        "reporting_policy": metric_bundle.get("reporting_policy", {}),
        "coverage_manifest": crm.attrs.get("coverage", {}),
        "matched_operating": comparable,
        "anomaly_records": metric_bundle.get("anomaly_records", []),
        "brand_regions": {str(b): sorted(set(g.district)) for b,g in crm.groupby("brand_key")},
        "band_config": {"HERMES": [100000,300000,500000,800000,1000000], "LV": [10000,20000,30000,40000,50000], "DIOR": [10000,20000,30000,40000,50000]},

        "high_luxury_amount_band_context": metric_bundle.get("high_luxury_amount_band_context", []),
        "privacy": {
            "contains_member_identifiers": True,
            "contains_transaction_rows": False,
            "model_can_read_member_records": False,
            "member_record_visibility": "DETERMINISTIC_CODE_AND_REPORT_ONLY",
        },
    }
