"""Deterministic authoring scaffolds. Business prose always comes from the host model."""
from copy import deepcopy
from pathlib import Path

from common import ContractError, read_json, require_text, write_json

ACTION_KINDS = {"SERVICE", "CONDITIONAL", "VERIFY", "MONITOR", "CAMPAIGN"}
IDENTITY = ("schema_version", "week_id", "input_signature", "implementation_version", "kb_version")


def validate_action_delivery(item, path="insight"):
    action = require_text(item.get("action_text"), path + ".action_text")
    kind = item.get("action_kind")
    if kind not in ACTION_KINDS:
        raise ContractError("ACTION_KIND_INVALID", "行动类型必须为SERVICE/CONDITIONAL/VERIFY/MONITOR/CAMPAIGN", {"path": path})
    body = require_text(item.get("narrative"), path + ".narrative")
    if not body.endswith("\n\n" + action) or not body[:-len(action)].strip():
        raise ContractError("ACTION_NOT_DELIVERED", "正文必须以完整行动段结尾；使用--draft由程序连接两段", {"path": path, "field": "action_text"})
    rec = item.get("recommendation", {})
    if rec.get("applicable") is True and rec.get("action", "").strip() != action:
        raise ContractError("ACTION_TEXT_MISMATCH", "内部建议action与交付行动段必须使用同一文本", {"path": path})
    if kind in {"CONDITIONAL", "MONITOR"}:
        condition = require_text(item.get("action_condition"), path + ".action_condition")
        if condition not in action:
            raise ContractError("ACTION_CONDITION_NOT_DELIVERED", "合作条件或复查触发条件须出现在行动段", {"path": path})
    if kind in {"SERVICE", "CAMPAIGN"} and rec.get("applicable") is not True:
        raise ContractError("ACTION_DECISION_INVALID", "日常服务或正式营销刺激必须声明适用性并通过证据校验", {"path": path})
    return action


def writing_template(plan, evidence):
    result = {k: plan.get(k) for k in IDENTITY}
    result.update(report_notes=[], data_quality_items=[], monitor_items=[],
                  run_status={"data_quality": "", "attribution": "", "knowledge_base": ""}, insights=[])
    for c in plan.get("selected_cases", []):
        result["insights"].append({
            "insight_case_id": c["insight_case_id"], "title": "", "analysis_text": "",
            "action_text": "", "action_kind": "", "action_condition": "",
            "recommendation": {"applicable": None, "not_applicable_reason": "", "target": "", "channel": "", "timing": "", "fact_ids": []},
            "primary_driver": {"statement": "", "evidence_strength": "", "evidence_ids": []},
            "impact_assessment": {"type": "", "statement": ""},
            "confidence": "", "analysis_note": {"basis": "", "limitations": ""},
            "verification_metrics": [], "follow_up_items": [], "fact_ids": [], "signal_ids": [],
            "activity_ids": [], "operating_assessment": {},
        })
    return result


def write_workspace(output_dir, context, plan, evidence):
    """Index first; load complete formal facts only for the current case. No fact cap."""
    output_dir = Path(output_dir)
    root = output_dir / "writing"
    root.mkdir(exist_ok=True)
    index = {k: context.get(k) for k in ("week_id", "input_signature", "implementation_version", "run_context")}
    index.update(instructions=[
        "先看本索引及四个few-shot；逐主题读取facts_file，按完整事实选择数字与行动。",
        "填写../writing_draft_template.json的副本：analysis_text写分析，action_text写完整自然建议段。",
        "只引用实际使用的事实；保留重要抵消与缺口。需要补查时使用调查入口，不读会员审计文件。",
        "SERVICE日常服务；CONDITIONAL待核实的合作；VERIFY核查；MONITOR暂不营销；CAMPAIGN正式营销刺激。",
        "品牌/卡级及数字仅为当周证据；参考样例的判断和建议方式，不复制其数值。",
        "finalize --draft自动组装固定字段与两段正文；不需要临时编写组装脚本。",
    ], cases=[])
    plans = {c["insight_case_id"]: c for c in plan.get("selected_cases", [])}
    total_bytes = 0
    for n, ledger in enumerate(context.get("evidence_ledgers", []), 1):
        # Use ordinal names, never untrusted case IDs as paths.
        path = root / f"case_{n:02d}.json"
        facts = ledger.get("facts", [])
        compact = {k: v for k, v in ledger.items() if k not in {"facts", "fact_ids", "signal_ids", "activity_ids"}}
        # Table representation removes repeated JSON field names, not evidence.
        columns = sorted({k for fact in facts for k in fact})
        compact["fact_columns"] = columns
        compact["fact_rows"] = [[fact.get(k) for k in columns] for fact in facts]
        import json
        path.write_text(json.dumps(compact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        total_bytes += path.stat().st_size
        c = plans[ledger["insight_case_id"]]
        index["cases"].append({"insight_case_id": c["insight_case_id"], "scope": c.get("primary_scope"),
                               "reason": c.get("independent_issue_reason"), "facts_file": "writing/" + path.name,
                               "fact_count": len(facts), "coverage": ledger.get("coverage"),
                               "unavailable_evidence": ledger.get("unavailable_evidence", [])})
    template = output_dir / "writing_draft_template.json"
    # Regeneration must not overwrite the model's separately named working draft.
    write_json(template, writing_template(plan, evidence))
    index_path = write_json(output_dir / "writing_context.json", index)
    return {"writing_context": str(index_path), "draft_template": str(template),
            "index_bytes": index_path.stat().st_size, "case_context_bytes": total_bytes}


def assemble_draft(draft, plan, evidence):
    for k in IDENTITY:
        if draft.get(k) != plan.get(k):
            raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "写作草稿与当前计划身份不一致", {"field": k})
    result = deepcopy(draft)
    cases = {c["insight_case_id"]: c for c in plan.get("selected_cases", [])}
    for n, item in enumerate(result.get("insights", [])):
        path = f"insights[{n}]"
        cid = item.get("insight_case_id")
        if cid not in cases:
            raise ContractError("CASE_DECISION_INVALID", "草稿引用未知主题", {"path": path, "case": cid})
        case = cases[cid]
        for k in ("issue_id", "issue_type", "primary_scope", "comparison_basis", "independent_issue_reason"):
            if k in item and item[k] != case.get(k):
                raise ContractError("ISSUE_IDENTITY_CHANGED", "不能覆盖主题身份", {"path": path, "field": k})
            item[k] = deepcopy(case.get(k))
        supporting = item.setdefault("supporting_case_ids", [])
        if any(c not in cases for c in supporting):
            raise ContractError("CASE_DECISION_INVALID", "未知支撑主题", {"path": path})
        item["anomaly_group_ids"] = list(dict.fromkeys(g for c in [cid, *supporting] for g in cases[c]["anomaly_group_ids"]))
        analysis = require_text(item.pop("analysis_text", None), path + ".analysis_text")
        action = require_text(item.get("action_text"), path + ".action_text")
        item["narrative"] = analysis + "\n\n" + action
        rec = item.get("recommendation", {})
        if rec.get("applicable") is True:
            if rec.get("action") and rec["action"].strip() != action:
                raise ContractError("ACTION_TEXT_MISMATCH", "草稿中重复的action不一致；仅填写action_text", {"path": path})
            rec["action"] = action
        item.setdefault("summary", item.get("primary_driver", {}).get("statement", ""))
        item.setdefault("confidence_reason", item.get("analysis_note", {}).get("limitations") or item.get("analysis_note", {}).get("basis", ""))
        item.setdefault("supporting_factors", [])
        item.setdefault("related_insight_ids", [])
        item.setdefault("claims", [
            {"claim_id": case["issue_id"] + "-finding", "claim_role": "TREND" if case["issue_type"] == "BRAND_TREND" else "CONTRIBUTION",
             "statement": item.get("primary_driver", {}).get("statement", ""), "primary_insight_id": case["issue_id"]},
            {"claim_id": case["issue_id"] + "-action", "claim_role": "ACTION", "statement": action, "primary_insight_id": case["issue_id"]},
        ])
        validate_action_delivery(item, path)
    return result
