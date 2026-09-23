from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from .models import CalculationError


CRM_ALIASES = {
    "event_time": ("event_time", "事件发生时间"),
    "district": ("district", "街区名称"),
    "store_name": ("store_name", "店铺名称(当前)", "店铺名称（当前）"),
    "store_id": ("store_id", "Lease No.", "lease_no"),
    "member_id": ("member_id", "会员卡号(当前)", "会员卡号（当前）"),
    "card_level": ("card_level", "会员卡名称(当前)", "会员卡名称（当前）"),
    "registration_time": ("registration_time", "开卡时间"),
    "paid_amount": ("paid_amount", "crm会员消费的实付金额的总和"),
    "points_balance": ("points_balance", "剩余积分", "当前剩余积分", "用户总积分"),
}

MALL_ALIASES = {
    "district": ("district", "街区名称"),
    "store_name": ("store_name", "Tenants", "Shop", "店铺名称"),
    "store_id": ("store_id", "Lease No.", "lease_no"),
    "business_date": ("business_date", "日期", "销售日期"),
    "mall_sales": ("mall_sales", "Mall Sales", "销售额"),
}


def _rename_aliases(frame: pd.DataFrame, aliases: Mapping[str, tuple[str, ...]]) -> pd.DataFrame:
    lower_to_actual = {str(column).strip().casefold(): column for column in frame.columns}
    rename: dict[Any, str] = {}
    for canonical, candidates in aliases.items():
        for candidate in candidates:
            actual = lower_to_actual.get(candidate.casefold())
            if actual is not None:
                rename[actual] = canonical
                break
    return frame.rename(columns=rename).copy()


def normalize_crm(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename documented CRM headers to the canonical calculation contract."""
    return _rename_aliases(frame, CRM_ALIASES)


def normalize_mall(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename an already-long Mall Sales table to canonical headers."""
    return _rename_aliases(frame, MALL_ALIASES)


def mall_wide_to_long(
    frame: pd.DataFrame,
    district: str,
    *,
    store_column: str,
    store_id_column: str | None = None,
) -> pd.DataFrame:
    """Convert a district sheet with one sales column per date into canonical rows."""
    id_columns = [store_column] + ([store_id_column] if store_id_column else [])
    date_columns = [column for column in frame.columns if column not in id_columns]
    parsed_dates = pd.to_datetime(pd.Index(date_columns), errors="coerce", dayfirst=True)
    valid = [column for column, parsed in zip(date_columns, parsed_dates) if not pd.isna(parsed)]
    if not valid:
        raise CalculationError("FIELD_MISSING", "Mall Sales未发现可解析的日期列")

    result = frame.melt(
        id_vars=id_columns,
        value_vars=valid,
        var_name="business_date",
        value_name="mall_sales",
    ).rename(columns={store_column: "store_name"})
    if store_id_column:
        result = result.rename(columns={store_id_column: "store_id"})
    result["district"] = district
    return result
