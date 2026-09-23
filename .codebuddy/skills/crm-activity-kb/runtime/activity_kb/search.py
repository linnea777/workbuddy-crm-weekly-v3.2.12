from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


ALIASES = {
    "brand": {},
    "region": {
        "south": "南区", "南区": "南区",
        "north": "北区", "北区": "北区",
        "west": "西区", "西区": "西区",
    },
    "card_tier": {
        "silver": "银卡", "银卡": "银卡",
        "gold": "金卡", "金卡": "金卡",
        "black": "黑卡", "黑卡": "黑卡",
        "black_diamond": "黑钻卡", "黑钻卡": "黑钻卡",
        "star_diamond": "星钻卡", "星钻卡": "星钻卡",
    },
}
EXCLUDED_BRANDS = {"apple", "apple-s07", "英皇电影城", "vss0013140"}
MAX_TOP_K = 20
def _as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _canon(entity_type: str, values: list[str]) -> tuple[list[str], list[str]]:
    mapping = ALIASES.get(entity_type, {})
    canonical: list[str] = []
    excluded: list[str] = []
    for value in values:
        normalized = value.casefold()
        if entity_type == "brand" and normalized in EXCLUDED_BRANDS:
            excluded.append("Apple")
        else:
            canonical.append(mapping.get(normalized, value))
    return sorted(set(canonical)), sorted(set(excluded))


def _relation(activity_start: date, activity_end: date, anomaly_start: date, anomaly_end: date) -> tuple[str, int]:
    if activity_end < anomaly_start:
        return "recently_ended", (anomaly_start - activity_end).days
    if activity_start > anomaly_end:
        return "upcoming_within_7d", (activity_start - anomaly_end).days
    return "overlapping", 0


def _time_score(relation: str, distance: int, pre_days: int, post_days: int) -> float:
    if relation == "overlapping":
        return 1.0
    if relation == "recently_ended":
        return max(0.0, 1.0 - 0.4 * distance / max(pre_days, 1))
    return max(0.0, 1.0 - 0.5 * distance / max(post_days, 1))


def _scope_items(entity_type: str, value: str) -> list[str]:
    items = [item.strip() for item in value.replace("，", ",").replace("；", ",").replace(";", ",").split(",") if item.strip()]
    canonical, _ = _canon(entity_type, items)
    return canonical


def _dimension_score(
    entity_type: str,
    scope: str,
    requested: list[str],
    exact_reason: str,
    all_reason: str,
    *,
    unknown_values: set[str] | None = None,
) -> tuple[float, list[str], bool]:
    if not requested:
        return 0.0, [], True
    if scope.strip() == "全部":
        return (0.7 if entity_type == "card_tier" else 0.65), [all_reason], True
    if unknown_values and scope.strip() in unknown_values:
        return 0.35, ["卡级范围待核实"], True
    scope_values = _scope_items(entity_type, scope)
    if entity_type == "brand":
        compatible = any(item in set(scope_values) for item in requested)
    else:
        values = {item.casefold() for item in scope_values}
        compatible = any(item.casefold() in values for item in requested)
    if compatible:
        return 1.0, [exact_reason], True
    return 0.0, [], False


def _scope_score(
    brand_scope: str,
    region_scope: str,
    card_tier_scope: str,
    brands: list[str],
    regions: list[str],
    card_tiers: list[str],
) -> tuple[float, list[str], bool]:
    reasons: list[str] = []
    components: list[float] = []
    checks = (
        _dimension_score("brand", brand_scope, brands, "品牌完全匹配", "活动覆盖全部品牌"),
        _dimension_score("region", region_scope, regions, "区域范围匹配", "活动覆盖全部区域"),
        _dimension_score(
            "card_tier", card_tier_scope, card_tiers, "卡级范围匹配", "活动覆盖全部卡级",
            unknown_values={"未提供", "指定等级以上"},
        ),
    )
    for score, match_reasons, compatible in checks:
        if not compatible:
            return 0.0, reasons, False
        if match_reasons:
            components.append(score)
            reasons.extend(match_reasons)
    return (sum(components) / len(components) if components else 0.5), reasons, True


def _time_reasons(relation: str, direction: str) -> list[str]:
    if relation == "overlapping":
        return ["活动与异常周时间重合"]
    if relation == "recently_ended":
        return ["活动在异常周前14天窗口内结束"]
    reasons = ["活动将在异常周结束后7天内开始"]
    if direction == "DOWN":
        reasons.append("消费下降可能与活动前需求延后有关")
    return reasons


def _type_score(activity_type: str, metric_id: str, requested_types: list[str], keywords: list[str], text: str) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if requested_types:
        score = 1.0 if activity_type in requested_types else 0.0
        return score, (["活动类型完全匹配"] if score else [])
    mapping = {
        "capture": {"multiple_points", "points_redemption", "gift_with_purchase", "prize_draw", "cashback"},
        "crm": {"multiple_points", "points_redemption", "gift_with_purchase", "prize_draw", "brand_event"},
        "mall": {"brand_event", "popup", "celebrity_event", "competitor_promotion"},
        "traffic": {"popup", "celebrity_event", "competitor_promotion"},
    }
    score = 0.5
    for key, types in mapping.items():
        if key in metric_id.casefold() and activity_type in types:
            score = 1.0
            reasons.append("活动类型与异常指标匹配")
            break
    if keywords and any(keyword.casefold() in text.casefold() for keyword in keywords):
        score = max(score, 0.9)
        reasons.append("关键词匹配")
    return score, reasons


def search_activities(
    query: dict[str, Any],
    date_range: dict[str, str],
    top_k: int | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    db = Path(db_path or (Path(__file__).resolve().parents[1] / "data" / "activity_kb.sqlite")).expanduser().resolve()
    if not db.exists():
        return {"knowledge_base_status": "unavailable", "kb_version": None, "query_window": None, "retrieved_evidence": [], "error": f"知识库索引不存在：{db}"}
    try:
        anomaly_start = date.fromisoformat(date_range["start_date"])
        anomaly_end = date.fromisoformat(date_range["end_date"])
        if anomaly_start > anomaly_end:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("date_range必须包含有效的start_date和end_date（YYYY-MM-DD）") from exc

    try:
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            metadata = dict(conn.execute("SELECT key, value FROM metadata"))
            config = dict(conn.execute("SELECT config_key, config_value FROM config"))
            pre_days = int(config.get("pre_window_days", 14))
            post_days = int(config.get("post_window_days", 7))
            requested_top_k = int(top_k or config.get("default_top_k", 5))
            if not 1 <= requested_top_k <= MAX_TOP_K:
                raise ValueError(f"top_k必须在1到{MAX_TOP_K}之间")
            weights = {"time": 0.40, "scope": 0.30, "type": 0.30}
            window_start = anomaly_start - timedelta(days=pre_days)
            window_end = anomaly_end + timedelta(days=post_days)

            dimension_type = str(query.get("dimension_type", "")).casefold()
            brands, excluded_brands = _canon("brand", _as_list(query.get("brands") or (query.get("dimension_key") if dimension_type == "brand" else None)))
            regions, _ = _canon("region", _as_list(query.get("regions") or (query.get("dimension_key") if dimension_type == "region" else None)))
            card_tiers, _ = _canon("card_tier", _as_list(query.get("card_tiers") or (query.get("dimension_key") if dimension_type == "card_tier" else None)))
            party_types = [item.casefold() for item in _as_list(query.get("party_types"))]
            activity_types = [item.casefold() for item in _as_list(query.get("activity_types"))]
            keywords = _as_list(query.get("keywords"))
            metric_id = str(query.get("metric_id", ""))
            direction = str(query.get("direction", "")).upper()

            if excluded_brands:
                return {"knowledge_base_status": "available", "kb_version": metadata.get("kb_version"), "query_window": {"start_date": window_start.isoformat(), "end_date": window_end.isoformat()}, "retrieved_evidence": [], "excluded_entities": excluded_brands}

            activities = conn.execute("SELECT * FROM activities WHERE start_date <= ? AND end_date >= ?", (window_end.isoformat(), window_start.isoformat())).fetchall()
            results: list[dict[str, Any]] = []
            for activity in activities:
                if party_types and activity["party_type"].casefold() not in party_types:
                    continue
                if activity_types and activity["activity_type"].casefold() not in activity_types:
                    continue
                scope_score, scope_reasons, compatible = _scope_score(
                    activity["brand_scope"], activity["region_scope"], activity["card_tier_scope"],
                    brands, regions, card_tiers,
                )
                if not compatible:
                    continue
                text = " ".join(
                    str(activity[key])
                    for key in ("activity_name", "mechanism_summary", "brand_scope", "region_scope", "card_tier_scope")
                )
                type_score, type_reasons = _type_score(activity["activity_type"], metric_id, activity_types, keywords, text)
                if activity_types and type_score == 0:
                    continue
                start = date.fromisoformat(activity["start_date"])
                end = date.fromisoformat(activity["end_date"])
                relation, distance = _relation(start, end, anomaly_start, anomaly_end)
                time_score = _time_score(relation, distance, pre_days, post_days)
                score = weights["time"] * time_score + weights["scope"] * scope_score + weights["type"] * type_score
                attribution_limit = "没有历史响应数据时不能确定归因"
                if relation == "upcoming_within_7d":
                    attribution_limit = (
                        "可能存在活动前消费需求延后；需结合活动开始后的消费回升验证"
                        if direction == "DOWN"
                        else "即将开始的活动仅作背景，不能直接解释本次异常"
                    )
                if activity["party_type"] == "competitor":
                    attribution_limit = "竞品活动仅作辅助证据；" + attribution_limit
                results.append({
                    "activity_id": activity["activity_id"], "activity_name": activity["activity_name"],
                    "party_type": activity["party_type"], "activity_type": activity["activity_type"],
                    "start_date": activity["start_date"], "end_date": activity["end_date"],
                    "relation_to_anomaly": relation, "distance_days": distance,
                    "brand_scope": activity["brand_scope"],
                    "region_scope": activity["region_scope"],
                    "card_tier_scope": activity["card_tier_scope"],
                    "mechanism_summary": activity["mechanism_summary"],
                    "match_reasons": _time_reasons(relation, direction) + scope_reasons + type_reasons,
                    "attribution_limit": attribution_limit,
                    "retrieval_score": round(score, 4),
                })
            results.sort(key=lambda item: (-item["retrieval_score"], item["activity_id"]))
            return {
                "knowledge_base_status": "available", "kb_version": metadata.get("kb_version"),
                "anomaly_group_id": query.get("anomaly_group_id"),
                "query_window": {"anomaly_start_date": anomaly_start.isoformat(), "anomaly_end_date": anomaly_end.isoformat(), "search_start_date": window_start.isoformat(), "search_end_date": window_end.isoformat()},
                "normalized_query": {"brands": brands, "regions": regions, "card_tiers": card_tiers, "party_types": party_types, "activity_types": activity_types, "metric_id": metric_id, "keywords": keywords, "direction": direction},
                "retrieved_evidence": results[:requested_top_k], "total_candidates": len(results),
            }
    except sqlite3.Error as exc:
        return {"knowledge_base_status": "unavailable", "kb_version": None, "query_window": None, "retrieved_evidence": [], "error": f"知识库索引不可用：{exc}"}


def search_activities_batch(
    queries: list[dict[str, Any]],
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    kb_version: str | None = None
    overall_status = "available"
    for index, item in enumerate(queries):
        query = item.get("query", item)
        date_range = item.get("date_range")
        if date_range is None and item.get("anomaly_start_date") and item.get("anomaly_end_date"):
            date_range = {"start_date": item["anomaly_start_date"], "end_date": item["anomaly_end_date"]}
        try:
            if date_range is None:
                raise ValueError("每个批量查询必须提供date_range")
            result = search_activities(query, date_range, item.get("top_k"), db_path=db_path)
            kb_version = kb_version or result.get("kb_version")
            if result.get("knowledge_base_status") == "unavailable":
                overall_status = "unavailable"
            results.append(result)
        except (TypeError, ValueError) as exc:
            errors.append({
                "index": index,
                "anomaly_group_id": query.get("anomaly_group_id") if isinstance(query, dict) else None,
                "message": str(exc),
            })
    return {
        "knowledge_base_status": overall_status,
        "kb_version": kb_version,
        "results": results,
        "errors": errors,
    }
