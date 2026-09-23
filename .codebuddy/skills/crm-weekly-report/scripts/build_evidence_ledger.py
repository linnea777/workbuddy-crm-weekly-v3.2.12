#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from common import ContractError, read_json, require_mapping, write_json


STATUS_RANK = {"NOT_APPLICABLE": 0, "DATA_UNAVAILABLE": 1, "UPSTREAM_SUFFICIENT": 2, "INVESTIGATED": 3}
VALID_COVERAGE = set(STATUS_RANK)


def build_ledgers(
    anomaly_bundle: dict[str, Any],
    frozen_plan: dict[str, Any],
    investigation_rounds: list[dict[str, Any]],
    activity_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    groups = {str(item["anomaly_group_id"]): item for item in anomaly_bundle.get("anomaly_groups", [])}
    signals = {str(item["signal_id"]): item for item in anomaly_bundle.get("anomaly_signals", [])}
    ledgers: dict[str, dict[str, Any]] = {}
    group_owner: dict[str, str] = {}
    for case in frozen_plan.get("selected_cases", []):
        case_id = str(case["insight_case_id"])
        member_ids = [str(item) for item in case.get("anomaly_group_ids", [])]
        for group_id in member_ids:
            group_owner.setdefault(group_id, set()).add(case_id)
        signal_ids = [signal_id for group_id in member_ids for signal_id in groups[group_id].get("signal_ids", [])]
        missing_signals = sorted(set(signal_ids) - set(signals))
        if missing_signals:
            raise ContractError("SIGNAL_REFERENCE_INVALID", "异常组引用了不存在的signal_id", {"signal_ids": missing_signals})
        ledgers[case_id] = {
            "insight_case_id": case_id,
            "anomaly_group_ids": member_ids,
            "signal_ids": signal_ids,
            "fact_ids": [],
            "audit_record_ids": [],
            "activity_ids": [],
            "signals": [signals[signal_id] for signal_id in signal_ids if signal_id in signals],
            "facts": [],
            "audit_records": [],
            "activities": [],
            "data_quality": [item for group_id in member_ids for item in groups[group_id].get("data_quality", [])],
            "unavailable_evidence": [],
            "coverage": [],
        }

    coverage_by_group: dict[str, str] = {}
    seen_fact_ids: dict[str, str] = {}
    seen_audit_ids: dict[str, str] = {}
    if not investigation_rounds:
        raise ContractError("COVERAGE_MISSING", "至少需要第一轮调查结果")
    first_round_ids = [str(item.get("anomaly_group_id", "")) for item in investigation_rounds[0].get("coverage", [])]
    first_round_counts = Counter(first_round_ids)
    duplicates = sorted(group_id for group_id, count in first_round_counts.items() if count != 1)
    if duplicates:
        raise ContractError("COVERAGE_DUPLICATE", "第一轮每个入选异常必须且只能有一个覆盖状态", {"anomaly_group_ids": duplicates})
    missing_first = sorted(set(group_owner) - set(first_round_counts))
    unknown_first = sorted(set(first_round_counts) - set(group_owner))
    if missing_first or unknown_first:
        raise ContractError("COVERAGE_MISSING", "第一轮覆盖必须与全部入选异常完全一致", {"missing": missing_first, "unknown": unknown_first})

    for round_index, round_payload in enumerate(investigation_rounds, 1):
        for field in ("week_id", "input_signature", "implementation_version"):
            if round_payload.get(field) != anomaly_bundle.get(field):
                raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "调查结果与候选池身份不一致", {"round": round_index, "field": field})
        for coverage in round_payload.get("coverage", []):
            group_id = str(coverage.get("anomaly_group_id", ""))
            status = str(coverage.get("status", ""))
            if status not in VALID_COVERAGE:
                raise ContractError("COVERAGE_STATUS_INVALID", "调查覆盖状态不受支持", {"anomaly_group_id": group_id, "status": status})
            if group_id in group_owner and STATUS_RANK.get(status, -1) > STATUS_RANK.get(coverage_by_group.get(group_id, ""), -1):
                coverage_by_group[group_id] = status
        for result in round_payload.get("results", []):
            case_id = str(result.get("insight_case_id", ""))
            group_id = str(result.get("anomaly_group_id", ""))
            if case_id not in ledgers or case_id not in group_owner.get(group_id,set()):
                raise ContractError("EVIDENCE_CASE_INVALID", "调查事实必须属于查询的case", {"insight_case_id": case_id, "anomaly_group_id": group_id})
            ledger = ledgers[case_id]
            ledger.setdefault("analysis_item_statuses", []).extend(result.get("analysis_item_statuses",[]))
            facts = [fact for fact in result.get("facts", []) if isinstance(fact, dict)]
            result_fact_ids: set[str] = set()
            for fact in facts:
                fact_id = str(fact.get("fact_id", ""))
                if not fact_id:
                    raise ContractError("FACT_ID_MISSING", "正式调查事实必须包含fact_id")
                canonical = json.dumps(fact,sort_keys=True,ensure_ascii=False)
                if fact_id in seen_fact_ids and seen_fact_ids[fact_id] != canonical:
                    raise ContractError("FACT_ID_COLLISION", "相同事实编号内容不一致", {"fact_id": fact_id})
                seen_fact_ids[fact_id] = canonical
                result_fact_ids.add(fact_id)
                if fact_id not in ledger["fact_ids"]:
                    ledger["fact_ids"].append(fact_id)
                    ledger["facts"].append(fact)
            for raw_audit in result.get("audit_records", []):
                if not isinstance(raw_audit, dict):
                    continue
                audit_id = str(raw_audit.get("audit_record_id", ""))
                if not audit_id:
                    raise ContractError("AUDIT_ID_MISSING", "会员审计记录必须包含audit_record_id")
                audit_fact_ids = {
                    str(item)
                    for item in raw_audit.get("fact_ids", [raw_audit.get("fact_id")])
                    if str(item or "").strip()
                }
                unknown_fact_ids = sorted(audit_fact_ids - result_fact_ids)
                if not audit_fact_ids or unknown_fact_ids:
                    raise ContractError(
                        "AUDIT_FACT_INVALID",
                        "会员审计记录只能引用同一查询返回的正式fact_id",
                        {"audit_record_id": audit_id, "unknown_fact_ids": unknown_fact_ids},
                    )
                # Shared audit records remain private; fact references are validated above.
                seen_audit_ids[audit_id] = case_id
                if audit_id not in ledger["audit_record_ids"]:
                    ledger["audit_record_ids"].append(audit_id)
                    ledger["audit_records"].append(raw_audit)
            for item in result.get("data_quality_status", []):
                if isinstance(item, dict) and item not in ledger["data_quality"]:
                    ledger["data_quality"].append(item)
            unavailable_reason = result.get("unavailable_reason") or result.get("error", {}).get("message")
            if unavailable_reason or not facts:
                ledger["unavailable_evidence"].append(
                    {
                        "anomaly_group_id": group_id,
                        "query_id": result.get("query_id"),
                        "status": result.get("status"),
                        "missing_metrics": result.get("missing_metrics", []),
                        "reason": unavailable_reason,
                    }
                )

    kb_status = "unavailable"
    kb_version = None
    if activity_result:
        kb_status = str(activity_result.get("knowledge_base_status", "unavailable"))
        kb_version = activity_result.get("kb_version")
        activity_results = activity_result.get("results", []) if kb_status != "unavailable" else []
        for result in activity_results:
            case_id = str(result.get("insight_case_id", ""))
            group_id = str(result.get("anomaly_group_id", ""))
            if case_id not in ledgers or case_id not in group_owner.get(group_id,set()):
                continue
            ledger = ledgers[case_id]
            for activity in result.get("retrieved_evidence", []):
                activity_id = str(activity.get("activity_id", ""))
                if activity_id and activity_id not in ledger["activity_ids"]:
                    ledger["activity_ids"].append(activity_id)
                    ledger["activities"].append(activity)

    for case_id, ledger in ledgers.items():
        for group_id in ledger["anomaly_group_ids"]:
            if group_id not in coverage_by_group:
                raise ContractError("COVERAGE_MISSING", "第一轮每个入选异常必须有且只有一个覆盖状态", {"anomaly_group_id": group_id})
            ledger["coverage"].append({"anomaly_group_id": group_id, "status": coverage_by_group[group_id]})
        ledger["fact_ids"].sort()
        ledger["audit_record_ids"].sort()
        ledger["activity_ids"].sort()

    return {
        "schema_version": "3.0",
        "week_id": anomaly_bundle.get("week_id"),
        "input_signature": anomaly_bundle.get("input_signature"),
        "implementation_version": anomaly_bundle.get("implementation_version", "3.2.2"),
        "kb_version": kb_version,
        "knowledge_base_status": kb_status,
        "access_control": {
            "contains_member_identifiers": any(item["audit_record_ids"] for item in ledgers.values()),
            "contains_transaction_rows": False,
            "model_can_read_audit_records": False,
            "model_projection": "FORMAL_FACT_DISPLAY_FIELDS_ONLY",
        },
        "issue_fact_links": [{"issue_id":case_id,"fact_id":fid} for case_id,ledger in ledgers.items() for fid in ledger["fact_ids"]],
        "shared_fact_count": len(seen_fact_ids),
        "cases": [ledgers[case_id] for case_id in sorted(ledgers)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anomalies", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--investigation", action="append", required=True)
    parser.add_argument("--activities")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        payload = build_ledgers(
            require_mapping(read_json(args.anomalies), "anomaly_bundle"),
            require_mapping(read_json(args.plan), "frozen_plan"),
            [require_mapping(read_json(path), "investigation") for path in args.investigation],
            require_mapping(read_json(args.activities), "activity_result") if args.activities else None,
        )
        output = write_json(args.output, payload)
        print(json.dumps({"ok": True, "output": str(output), "case_count": len(payload["cases"])}, ensure_ascii=False))
        return 0
    except (ContractError, OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        error = exc.as_dict() if isinstance(exc, ContractError) else {"code": "EVIDENCE_LEDGER_FAILED", "message": str(exc), "details": {}}
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
