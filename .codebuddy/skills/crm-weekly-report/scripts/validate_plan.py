#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from issue_contract import reject_quota_reason, validate_query_scope
from common import ContractError, read_json, require_list, require_mapping, require_text, write_json


FOCUSES = {"CONTRIBUTION", "MEMBER_BEHAVIOR", "BRAND_OPERATION"}
COMPARISON_BASES = {"WOW", "YOY", "YTD", "CONTINUOUS_PERIOD", "EIGHT_WEEK_BASELINE"}
ANALYSIS_ITEMS = {
    "CORE_METRICS", "CONTRIBUTORS", "MEMBER_COUNT", "TRANSACTIONS", "TICKET_SIZE",
    "CARD_TIER", "NEW_EXISTING", "LARGE_SPEND", "ACTIVITY_RESPONSE", "REFUND",
    "OPERATING_STATUS", "AMOUNT_BAND", "WEEKLY_HISTORY",
}
PRIORITY_BRANDS = {"HERMES", "HERMES 爱马仕", "LV", "LOUIS VUITTON 路易威登", "DIOR", "DIOR 迪奥"}
IDENTITY_FIELDS = ("week_id", "input_signature", "implementation_version")


def _validate_identity(payload: dict[str, Any], expected: dict[str, Any], path: str) -> None:
    if str(payload.get("schema_version")) != "3.0":
        raise ContractError("SCHEMA_VERSION_UNSUPPORTED", f"{path}必须为3.0")
    mismatches = {
        field: {"expected": expected.get(field), "actual": payload.get(field)}
        for field in IDENTITY_FIELDS
        if not str(payload.get(field, "")).strip() or payload.get(field) != expected.get(field)
    }
    if mismatches:
        raise ContractError("ARTIFACT_IDENTITY_MISMATCH", f"{path}与上游输入、周或实现版本不一致", mismatches)


def _groups(anomaly_bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = {}
    for raw in require_list(anomaly_bundle.get("anomaly_groups"), "anomaly_bundle.anomaly_groups"):
        group = require_mapping(raw, "anomaly_group")
        group_id = require_text(group.get("anomaly_group_id"), "anomaly_group_id")
        if group_id in groups:
            raise ContractError("ANOMALY_DUPLICATE", "候选异常编号重复", {"anomaly_group_id": group_id})
        groups[group_id] = group
    return groups


def _validate_query(query: dict[str, Any], case_id: str, member_ids: set[str], seen_query_ids: set[str], path: str) -> None:
    query_id = require_text(query.get("query_id"), f"{path}.query_id")
    if query_id in seen_query_ids:
        raise ContractError("QUERY_DUPLICATE", "query_id不能重复", {"query_id": query_id})
    seen_query_ids.add(query_id)
    if require_text(query.get("insight_case_id"), f"{path}.insight_case_id") != case_id:
        raise ContractError("QUERY_CASE_INVALID", "查询必须引用当前case", {"query_id": query_id, "case_id": case_id})
    group_id = require_text(query.get("anomaly_group_id"), f"{path}.anomaly_group_id")
    if group_id not in member_ids:
        raise ContractError("QUERY_ANOMALY_INVALID", "查询只能引用当前case成员", {"query_id": query_id, "anomaly_group_id": group_id})
    if str(query.get("focus", "")).upper() not in FOCUSES:
        raise ContractError("QUERY_SCHEMA_INVALID", "focus不在注册查询Schema中", {"query_id": query_id})
    if str(query.get("comparison_basis", "WOW")).upper() not in COMPARISON_BASES:
        raise ContractError("QUERY_SCHEMA_INVALID", "comparison_basis不受支持", {"query_id": query_id})
    analysis_items = query.get("analysis_items", [])
    if not isinstance(analysis_items, list):
        raise ContractError("QUERY_SCHEMA_INVALID", "analysis_items必须是数组", {"query_id": query_id})
    invalid_items = sorted({str(item).upper() for item in analysis_items} - ANALYSIS_ITEMS)
    if invalid_items:
        raise ContractError("QUERY_SCHEMA_INVALID", "analysis_items包含未注册枚举", {"query_id": query_id, "invalid_items": invalid_items})
    if "ACTIVITY_RESPONSE" in {str(item).upper() for item in analysis_items}:
        if str(query.get("focus", "")).upper() != "MEMBER_BEHAVIOR":
            raise ContractError("QUERY_SCHEMA_INVALID", "ACTIVITY_RESPONSE只能用于MEMBER_BEHAVIOR", {"query_id": query_id})
        filters = require_mapping(query.get("filters"), f"{path}.filters")
        for field in ("activity_window", "baseline_window", "post_window"):
            if field != "activity_window" and field not in filters:
                continue
            window = require_mapping(filters.get(field), f"{path}.filters.{field}")
            try:
                start = date.fromisoformat(require_text(window.get("start_date"), f"{path}.filters.{field}.start_date"))
                end = date.fromisoformat(require_text(window.get("end_date"), f"{path}.filters.{field}.end_date"))
            except ValueError as exc:
                raise ContractError("QUERY_SCHEMA_INVALID", f"{field}日期必须为YYYY-MM-DD", {"query_id": query_id}) from exc
            if start > end:
                raise ContractError("QUERY_SCHEMA_INVALID", f"{field}开始日期不得晚于结束日期", {"query_id": query_id})
    scope = query.get("scope")
    if not isinstance(scope, dict) and not str(scope or "").strip():
        raise ContractError("QUERY_SCHEMA_INVALID", "scope必须是非空字符串或对象", {"query_id": query_id})
    if "top_k" in query:
        try:
            top_k = int(query["top_k"])
        except (TypeError, ValueError) as exc:
            raise ContractError("QUERY_SCHEMA_INVALID", "top_k必须是整数", {"query_id": query_id}) from exc
        if not 1 <= top_k <= 20:
            raise ContractError("QUERY_SCHEMA_INVALID", "top_k必须在1到20之间", {"query_id": query_id})


def validate_first_round(anomaly_bundle: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    _validate_identity(plan, anomaly_bundle, "InvestigationPlan")
    if "kb_version" not in plan:
        raise ContractError("FIELD_INVALID", "InvestigationPlan必须携带kb_version（不可用时为null）")
    groups = _groups(anomaly_bundle)
    selected_cases = require_list(plan.get("selected_cases"), "selected_cases")
    omitted = list(require_list(plan.get("omitted_anomaly_groups"), "omitted_anomaly_groups"))
    accounted={g for c in selected_cases for g in c.get("anomaly_group_ids",[])} | {o.get("anomaly_group_id") for o in omitted}
    omitted.extend(o for o in anomaly_bundle.get("observations",[]) if o["anomaly_group_id"] not in accounted)
    plan["omitted_anomaly_groups"]=omitted
    selected_owner: dict[str, str] = {}
    case_ids: set[str] = set()
    query_ids: set[str] = set()
    issue_ids: set[str] = set()
    for case_index, raw_case in enumerate(selected_cases):
        case = require_mapping(raw_case, f"selected_cases[{case_index}]")
        case_id = require_text(case.get("insight_case_id"), f"selected_cases[{case_index}].insight_case_id")
        if case_id in case_ids:
            raise ContractError("CASE_DUPLICATE", "insight_case_id不能重复", {"insight_case_id": case_id})
        case_ids.add(case_id)
        iid=require_text(case.get("issue_id"), "issue_id")
        if iid in issue_ids:raise ContractError("ISSUE_DUPLICATE","同一主题不能重复")
        issue_ids.add(iid)
        if case.get("issue_type") not in {"REGION_CONTRIBUTION","BRAND_TREND","INDEPENDENT_RISK"}:raise ContractError("ISSUE_TYPE_INVALID","主题类型不受支持")
        require_mapping(case.get("primary_scope"),"primary_scope")
        require_text(case.get("independent_issue_reason"), "independent_issue_reason")
        require_text(case.get("selection_reason"), f"selected_cases[{case_index}].selection_reason")
        member_ids = {require_text(item, f"selected_cases[{case_index}].anomaly_group_ids") for item in require_list(case.get("anomaly_group_ids"), f"selected_cases[{case_index}].anomaly_group_ids")}
        if not member_ids:
            raise ContractError("CASE_EMPTY", "case必须包含至少一个异常", {"insight_case_id": case_id})
        unknown = sorted(member_ids - set(groups))
        if unknown:
            raise ContractError("ANOMALY_UNKNOWN", "case引用了不存在的异常", {"ids": unknown})
        for group_id in member_ids:
            if group_id in selected_owner and case.get("issue_id")==selected_owner[group_id]:
                raise ContractError("ISSUE_DUPLICATE", "主题不能重复", {"anomaly_group_id": group_id})
            selected_owner[group_id] = case_id
        identities = {(str(groups[group_id].get("week_id")), str(groups[group_id].get("dimension_type")).upper(), str(groups[group_id].get("dimension_key")),str(groups[group_id].get("region_id") or "")) for group_id in member_ids}
        if len(identities) != 1:
            raise ContractError("CASE_MERGE_INVALID", "case只能合并同周、同维度、同实体异常", {"insight_case_id": case_id, "identities": sorted(identities)})
        for query_index, raw_query in enumerate(require_list(case.get("queries", []), f"selected_cases[{case_index}].queries")):
            _validate_query(require_mapping(raw_query, f"selected_cases[{case_index}].queries[{query_index}]"), case_id, member_ids, query_ids, f"selected_cases[{case_index}].queries[{query_index}]")
        for query in case.get("queries",[]):
            validate_query_scope(query,groups[query["anomaly_group_id"]],anomaly_bundle.get("brand_regions",{}))
        for activity_index, raw_query in enumerate(require_list(case.get("activity_queries", []), f"selected_cases[{case_index}].activity_queries")):
            query = require_mapping(raw_query, f"selected_cases[{case_index}].activity_queries[{activity_index}]")
            if require_text(query.get("insight_case_id"), "activity_query.insight_case_id") != case_id or require_text(query.get("anomaly_group_id"), "activity_query.anomaly_group_id") not in member_ids:
                raise ContractError("ACTIVITY_QUERY_INVALID", "活动查询必须引用当前case与成员异常", {"insight_case_id": case_id})
            require_mapping(query.get("query"), "activity_query.query")
            date_range = require_mapping(query.get("date_range"), "activity_query.date_range")
            try:
                start = date.fromisoformat(require_text(date_range.get("start_date"), "activity_query.date_range.start_date"))
                end = date.fromisoformat(require_text(date_range.get("end_date"), "activity_query.date_range.end_date"))
            except ValueError as exc:
                raise ContractError("ACTIVITY_QUERY_INVALID", "活动查询日期必须为YYYY-MM-DD") from exc
            if start > end:
                raise ContractError("ACTIVITY_QUERY_INVALID", "活动查询开始日期不得晚于结束日期")
            if "top_k" in query:
                try:
                    top_k = int(query["top_k"])
                except (TypeError, ValueError) as exc:
                    raise ContractError("ACTIVITY_QUERY_INVALID", "活动查询top_k必须是整数") from exc
                if not 1 <= top_k <= 20:
                    raise ContractError("ACTIVITY_QUERY_INVALID", "活动查询top_k必须在1到20之间")

    omitted_ids: set[str] = set()
    for index, raw in enumerate(omitted):
        item = require_mapping(raw, f"omitted_anomaly_groups[{index}]")
        group_id = require_text(item.get("anomaly_group_id"), f"omitted_anomaly_groups[{index}].anomaly_group_id")
        reject_quota_reason(item.get("reason"))
        require_text(item.get("reason"), f"omitted_anomaly_groups[{index}].reason")
        if group_id in omitted_ids or group_id in selected_owner or group_id not in groups:
            raise ContractError("OMISSION_INVALID", "省略异常必须存在、唯一且未入选", {"anomaly_group_id": group_id})
        omitted_ids.add(group_id)
    missing = sorted(set(groups) - set(selected_owner) - omitted_ids)
    if missing:
        raise ContractError("CANDIDATE_UNACCOUNTED", "每个候选必须入选或有具体省略理由", {"missing": missing})
    mandatory_missing = sorted(group_id for group_id, group in groups.items() if group.get("must_investigate") is True and group_id not in selected_owner)
    if mandatory_missing:
        raise ContractError("MANDATORY_OMITTED", "业务必查异常不得省略", {"missing": mandatory_missing})
    return {"ok": True, "schema_version": "3.0", "selected_case_count": len(selected_cases), "selected_anomaly_count": len(selected_owner), "omitted_anomaly_count": len(omitted_ids), "query_count": len(query_ids)}


def validate_second_round(plan: dict[str, Any], frozen_plan: dict[str, Any]) -> dict[str, Any]:
    _validate_identity(plan, frozen_plan, "第二轮InvestigationPlan")
    if "kb_version" not in plan or plan.get("kb_version") != frozen_plan.get("kb_version"):
        raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "第二轮不得更换知识库版本")
    frozen = {str(case["insight_case_id"]): set(case["anomaly_group_ids"]) for case in frozen_plan.get("selected_cases", [])}
    query_ids: set[str] = set()
    seen_cases: set[str] = set()
    for index, raw_case in enumerate(require_list(plan.get("selected_cases"), "selected_cases")):
        case = require_mapping(raw_case, f"selected_cases[{index}]")
        case_id = require_text(case.get("insight_case_id"), f"selected_cases[{index}].insight_case_id")
        if case_id in seen_cases:
            raise ContractError("CASE_DUPLICATE", "第二轮insight_case_id不能重复", {"insight_case_id": case_id})
        seen_cases.add(case_id)
        members = set(require_list(case.get("anomaly_group_ids"), f"selected_cases[{index}].anomaly_group_ids"))
        if case_id not in frozen or members != frozen[case_id]:
            raise ContractError("CASE_MEMBERS_FROZEN", "第二轮不得新增case或改变case成员", {"insight_case_id": case_id})
        for query_index, raw_query in enumerate(require_list(case.get("queries", []), f"selected_cases[{index}].queries")):
            _validate_query(require_mapping(raw_query, "query"), case_id, members, query_ids, f"selected_cases[{index}].queries[{query_index}]")
    return {"ok": True, "schema_version": "3.0", "round": 2, "query_count": len(query_ids)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anomalies")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--frozen-plan")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        plan = require_mapping(read_json(args.plan), "plan")
        if args.frozen_plan:
            result = validate_second_round(plan, require_mapping(read_json(args.frozen_plan), "frozen_plan"))
        else:
            if not args.anomalies:
                raise ContractError("INPUT_MISSING", "第一轮计划校验必须提供--anomalies")
            result = validate_first_round(require_mapping(read_json(args.anomalies), "anomaly_bundle"), plan)
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContractError, OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        error = exc.as_dict() if isinstance(exc, ContractError) else {"code": "PLAN_INVALID", "message": str(exc), "details": {}}
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
