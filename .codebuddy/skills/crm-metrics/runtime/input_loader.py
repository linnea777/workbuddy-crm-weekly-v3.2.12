from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from crm_weekly_metrics import CalculationError, mall_wide_to_long


ALLOWED_DISTRICTS = {"n", "s", "w", "north", "south", "west", "北区", "南区", "西区"}
KNOWN_OUT_OF_SCOPE_DISTRICTS = {"线上商城"}


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def parse_header_date(value: Any) -> date | None:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20_000 <= float(value) <= 80_000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})月-(\d{2,4})", text)
    if match:
        day, month, year = map(int, match.groups())
        return date(year + 2000 if year < 100 else year, month, day)
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed.date()


def load_crm(path: str | Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        raise CalculationError("INPUT_READ_FAILED", "CRM必须是存在的.xlsx文件", {"path": str(source)})
    crm = pd.read_excel(source, sheet_name=0)
    district_column = "街区名称" if "街区名称" in crm.columns else "district" if "district" in crm.columns else None
    if district_column is None:
        raise CalculationError("FIELD_MISSING", "CRM缺少街区名称/district字段")
    values = crm[district_column].map(_text)
    allowed = ALLOWED_DISTRICTS | {_text(value) for value in KNOWN_OUT_OF_SCOPE_DISTRICTS}
    invalid = crm.loc[~values.isin(allowed), district_column]
    if not invalid.empty:
        samples = invalid.astype(str).str.strip().drop_duplicates().head(5).tolist()
        raise CalculationError("FIELD_MISSING", "街区名称包含未知值", {"samples": samples})
    out_of_scope = values.isin({_text(value) for value in KNOWN_OUT_OF_SCOPE_DISTRICTS})
    audit = {
        "input_rows": int(len(crm)),
        "in_scope_rows": int((~out_of_scope).sum()),
        "known_out_of_scope_rows": int(out_of_scope.sum()),
        "unknown_district_rows": 0,
        "handling": "仅排除已批准的范围外渠道；未知街区阻断运行",
    }
    return crm.loc[~out_of_scope].copy(), audit


def load_mall(paths: dict[str, str | Path]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    frames: list[pd.DataFrame] = []
    audit: list[dict[str, Any]] = []
    for district in ("N", "S", "W"):
        source = Path(paths[district]).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".xlsx":
            raise CalculationError("INPUT_READ_FAILED", "Mall Sales必须是存在的.xlsx文件", {"district": district, "path": str(source)})
        workbook = pd.read_excel(source, sheet_name=None)
        for sheet_name, raw in workbook.items():
            if raw.empty:
                audit.append({"district": district, "sheet": sheet_name, "status": "SKIPPED", "reason": "empty sheet"})
                continue
            first_column = raw.columns[0]
            store_column = "Shop" if district == "N" else "Tenants"
            if store_column not in raw.columns:
                audit.append({"district": district, "sheet": sheet_name, "status": "SKIPPED", "reason": f"missing {store_column}"})
                continue
            actual_rows = pd.to_numeric(raw[first_column], errors="coerce").notna() & raw[store_column].notna()
            cleaned = raw.loc[actual_rows].copy()
            rename = {column: parsed for column in cleaned.columns if (parsed := parse_header_date(column))}
            if not rename:
                audit.append({"district": district, "sheet": sheet_name, "status": "SKIPPED", "reason": "no date columns"})
                continue
            cleaned = cleaned[[store_column, *rename.keys()]].rename(columns=rename)
            frames.append(mall_wide_to_long(cleaned, district, store_column=store_column))
            audit.append(
                {
                    "district": district,
                    "sheet": sheet_name,
                    "status": "LOADED",
                    "stores": int(cleaned[store_column].nunique()),
                    "date_columns": len(rename),
                    "min_date": min(rename.values()).isoformat(),
                    "max_date": max(rename.values()).isoformat(),
                }
            )
    if not frames:
        raise CalculationError("INPUT_READ_FAILED", "Mall Sales未加载到可用数据")
    return pd.concat(frames, ignore_index=True), audit
