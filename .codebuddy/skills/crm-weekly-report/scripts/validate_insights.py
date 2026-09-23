#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from decimal import Decimal
from typing import Any, Iterable

from issue_contract import validate_tree, validate_disclosure
from common import ContractError, read_json, require_list, require_mapping, require_text, write_json
from writing_delivery import validate_action_delivery


CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}
IMPACT_TYPES = {"REAL_OPERATION", "POINTS_PARTICIPATION", "MIXED", "UNRESOLVED"}
CHANNELS = {"积分活动", "会员触达", "VIC 邀约", "VIC邀约", "线下核查", "会员体验活动", "品牌联合活动"}
STRONG_CAUSAL_TERMS = ("直接导致", "已证实由", "直接造成", "证明了因果")
UNITS = r"(?:个百分点|百分点|万元|万|元|人|笔|个|%)"
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?(?:" + UNITS + r")?")
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


def _text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)


def _ids(value: Any, path: str) -> list[str]:
    values = [require_text(item, path) for item in require_list(value, path)]
    if len(values) != len(set(values)):
        raise ContractError("REFERENCE_DUPLICATE", f"{path}不能包含重复ID")
    return values


def _official_number_tokens(ledger: dict[str, Any], groups: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    sources: list[Any] = []
    for group in groups:
        sources.extend((group.get("display_fields", {}), group.get("display_value"), group.get("display_change")))
    sources.extend(s.get("display_fields", {k:s.get(k) for k in ("display_value","display_change","display_observed_value","display_text")}) for s in ledger.get("signals", []))
    sources.extend(f.get("display_fields", {}) for f in ledger.get("facts", []))
    for source in sources:
        for text in _text_values(source):
            tokens.update(NUMBER_RE.findall(text))
    # Count display fields historically omit units; allow their actual semantic unit.
    for fact in ledger.get('facts', []):
        metric = fact.get('metric_id', '')
        for field, value in fact.get('display_fields', {}).items():
            unit = '人' if field in {'member_count', 'previous_member_count'} else ''
            if field in {'current', 'baseline', 'change'}:
                if metric == 'ACTIVE_MEMBERS':
                    unit = '人'
                elif metric == 'TRANSACTIONS':
                    unit = '笔'
            if unit and re.fullmatch(r'[-+]?\d+(?:\.\d+)?', str(value)):
                tokens.add(str(value) + unit)
    return tokens


def _entity_labels(facts: list[dict[str, Any]], groups: list[dict[str, Any]]) -> set[str]:
    """Source entity names are identifiers, not quantitative claims."""
    scopes = [f.get("scope_details", {}) for f in facts] + groups
    labels = {str(s["dimension_key"]) for s in scopes
              if s.get("dimension_type") == "BRAND" and s.get("dimension_key")}
    labels.update(str(f.get("display_fields", {}).get("amount_band")) for f in facts
                  if f.get("display_fields", {}).get("amount_band"))
    return labels


def _normalize_number_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("−", "-")
    text = re.sub(r"(?<=\d)\s+(?=" + UNITS + r")", "", text)
    return text


def _canonical_number(token: str) -> tuple[str, str]:
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)(.*)", _normalize_number_text(token))
    if not match:
        return token, ""
    unit = {"万元": "万", "个百分点": "百分点"}.get(match[2], match[2])
    return str(Decimal(match[1]).normalize()), unit


def _validate_narrative_numbers(record: dict[str, Any], allowed: set[str], path: str,
                                entity_labels: set[str] | None = None) -> None:
    def narrative_only(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: narrative_only(item)
                for key, item in value.items()
                if key not in {"signal_ids", "fact_ids", "activity_ids", "anomaly_group_ids", "insight_case_id", "supporting_case_ids", "evidence_ids", "issue_id", "issue_type", "claim_id", "primary_insight_id", "related_insight_ids", "nodes", "edges", "root_node_id", "comparison_basis", "primary_scope"}
            }
        if isinstance(value, list):
            return [narrative_only(item) for item in value]
        return value

    fields = narrative_only(record)
    unsupported: set[str] = set()
    allowed_values = {_canonical_number(token) for token in allowed}
    for text in _text_values(fields):
        text = _normalize_number_text(text)
        for label in sorted(entity_labels or (), key=len, reverse=True):
            text = text.replace(_normalize_number_text(label), "[实体]")
        if re.search(r'(增长|增加|提高|上升|提升|上涨)(?:约|为|达)?\s*-\d', text):
            raise ContractError("NUMBER_DIRECTION_CONFLICT", "增长方向词不能搭配负数", {"path": path})
        # A natural directional phrase preserves the sign: “下降24.5%” is -24.5%.
        # An unsigned figure without an explicit decrease remains unsigned.
        text = re.sub(r'(下降|减少|降低|下滑|缩减|降幅)(?:约|为|达|至多|至少)?\s*(\d+(?:\.\d+)?(?:%|万元|万|元|人|笔|个百分点|百分点))', r'\1-\2', text)
        for token in NUMBER_RE.findall(text):
            if _canonical_number(token) not in allowed_values:
                unsupported.add(token)
    if unsupported:
        raise ContractError("NUMBER_NOT_OFFICIAL", "叙事中的正式数字必须逐值对应上游展示字段", {"path": path, "unsupported": sorted(unsupported)})


def _merge_story_ledgers(case_ids: list[str], ledgers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        field: [item for case_id in case_ids for item in ledgers[case_id].get(field, [])]
        for field in ("signal_ids", "fact_ids", "activity_ids", "signals", "facts", "activities", "data_quality")
    }


def validate_activity_assessment(insight, facts):
    """An explicit business attribution needs relevant comparison evidence."""
    assessment = insight.get('activity_assessment')
    if assessment is None:
        for sentence in re.split(r'[。；;]', insight.get('narrative', '')):
            if re.search(r'(活动|倍积分).{0,30}(带动|刺激|促进|推动|提高)', sentence) and not any(t in sentence for t in ('可能', '尚未', '未见', '不能', '未能', '假设', '无法', '没有')):
                raise ContractError('ACTIVITY_ASSESSMENT_REQUIRED', '明确活动归因需声明并验证活动判断依据')
        return
    if not isinstance(assessment, dict) or assessment.get('strength') not in {'ASSOCIATED', 'SUPPORTED'}:
        raise ContractError('ACTIVITY_ASSESSMENT_INVALID', '活动判断必须声明ASSOCIATED或SUPPORTED')
    if assessment['strength'] == 'ASSOCIATED':
        validate_activity_assessment({**insight, 'activity_assessment': None}, facts)
        return
    outcome = assessment.get('outcome')
    if outcome not in {'CRM', 'CONSUMPTION', 'POINTS', 'BOTH'}:
        raise ContractError('ACTIVITY_ASSESSMENT_INVALID', '需分别声明CRM、消费或积分参与结果')
    if not assessment.get('confound_review') or insight.get('confidence') == 'LOW':
        raise ContractError('ACTIVITY_ATTRIBUTION_UNSUPPORTED', '明确归因需核查其他解释，不能为低置信度')
    ids = assessment.get('fact_ids', [])
    cited = {f['fact_id']: f for f in facts if f['fact_id'] in insight.get('fact_ids', [])}
    if not ids or not set(ids).issubset(cited):
        raise ContractError('EVIDENCE_REFERENCE_INVALID', '活动判断只能使用正文已引用的事实')
    for fid in ids:
        f = cited[fid]
        x = f.get('extra', {})
        if f.get('metric_id') != 'ACTIVITY_RESPONSE':
            continue
        if x.get('activity_id') not in insight.get('activity_ids', []):
            continue
        if x.get('baseline_count', 0) < 2 or x.get('baseline_event_check') != 'VERIFIED' or not x.get('baseline_context_sources'):
            continue
        a, b, n, nb = (x.get(k) or {} for k in ('activity', 'baseline', 'non_activity', 'non_activity_baseline'))
        if not all(isinstance(v, (int, float)) for v in (a.get('sales'), b.get('sales'), n.get('sales'), nb.get('sales'))):
            continue
        if b['sales'] <= 0 or nb['sales'] <= 0 or a['sales'] <= b['sales']:
            continue
        if a['sales']/b['sales'] <= n['sales']/nb['sales']:
            continue
        operating = x.get('operating', {})
        if outcome in {'CONSUMPTION', 'BOTH'} and not (
            operating.get('available') and operating.get('current_mall') is not None and
            operating.get('baseline_mall') is not None and operating['current_mall'] > operating['baseline_mall']):
            continue
        if outcome in {'POINTS', 'BOTH'} and not (
            operating.get('available') and operating.get('current_capture') is not None and
            operating.get('baseline_capture') is not None and operating['current_capture'] > operating['baseline_capture']):
            continue
        return
    raise ContractError('ACTIVITY_ATTRIBUTION_UNSUPPORTED', '活动比较尚不足以支持所声明的明确归因，请补充依据或保留关联判断')


def _validate_total_region_disclosure(insight, ledger):
    if insight.get('primary_scope', {}).get('dimension_type') != 'TOTAL':
        return
    body = insight.get('narrative', '')
    cited = set(insight.get('fact_ids', []))
    missing = []
    for key, name in [('N', '北区'), ('S', '南区'), ('W', '西区')]:
        facts = [f for f in ledger.get('facts', []) if f.get('scope') == 'REGION:' + key
                 and f.get('metric_id') == 'CRM_SALES'
                 and f.get('display_fields', {}).get('impact_amount') is not None]
        if facts and (name not in body or not any(f['fact_id'] in cited for f in facts)):
            missing.append(name)
    if missing:
        raise ContractError('TOTAL_REGION_DISCLOSURE_REQUIRED', '全场销售正文须披露可用的区域贡献并引用对应事实', {'missing_regions': missing})


def _validate_operating_disclosure(insight, ledger):
    scope = insight.get('primary_scope', {})
    if scope.get('dimension_type') not in {'TOTAL', 'REGION'}:
        return
    label = 'TOTAL' if scope['dimension_type'] == 'TOTAL' else 'REGION:' + scope['dimension_key']
    basis = insight.get('comparison_basis', 'WOW')
    metrics = {'CRM_SALES', 'MALL_SALES', 'CAPTURE_RATIO'}
    facts = [f for f in ledger.get('facts', []) if f.get('scope') == label and f.get('comparison_basis', 'WOW') == basis
             and f.get('metric_id') in metrics and all(f.get('display_fields', {}).get(k) not in (None, '', 'N/A') for k in ('current', 'baseline'))]
    assessment = insight.get('operating_assessment', {})
    if not isinstance(assessment, dict):
        assessment = {}
    body = insight.get('narrative', '')
    available = {f['metric_id'] for f in facts}
    if available != metrics:
        reason = assessment.get('unavailable_reason', '')
        if not reason or reason not in body:
            raise ContractError('OPERATING_COMPARISON_UNAVAILABLE', '缺少同范围经营比较时须在正文说明具体缺口', {'scope':label,'missing_metrics':sorted(metrics-available)})
        return
    cited = set(insight.get('fact_ids', [])) & set(assessment.get('fact_ids', []))
    if {f['metric_id'] for f in facts if f['fact_id'] in cited} != metrics:
        raise ContractError('OPERATING_COMPARISON_REQUIRED', '全场/区域判断须引用同范围同基准CRM、Mall与Capture三项事实', {'scope':label})
    statement = assessment.get('statement', '')
    has_mall = bool(re.search(r'Mall\s*(?:Sales|销售)?|商场销售|经营销售', body, re.I))
    has_capture = bool(re.search(r'Capture\s*(?:Ratio)?|积分记录占比|积分参与率', body, re.I))
    if not has_mall or not has_capture or not statement or statement not in body:
        raise ContractError('OPERATING_JUDGMENT_REQUIRED', '正文须展示Mall/Capture比较并写出经营销售与积分记录占比判断')


def _validate_unreported_region_operations(insight, ledger, reported_regions):
    if insight.get('primary_scope', {}).get('dimension_type') != 'TOTAL':
        return
    for region in sorted({'N', 'S', 'W'} - set(reported_regions)):
        if not any(f.get('scope') == 'REGION:' + region for f in ledger.get('facts', [])):
            continue
        assessment = insight.get('operating_assessment', {}).get('regions', {}).get(region, {})
        proxy = dict(insight, primary_scope={'dimension_type':'REGION','dimension_key':region}, operating_assessment=assessment)
        _validate_operating_disclosure(proxy, ledger)


def validate_insight_bundle(
    anomaly_bundle: dict[str, Any],
    plan: dict[str, Any],
    evidence_bundle: dict[str, Any],
    insight_bundle: dict[str, Any],
) -> dict[str, Any]:
    _validate_identity(plan, anomaly_bundle, "InvestigationPlan")
    _validate_identity(evidence_bundle, anomaly_bundle, "EvidenceLedger")
    _validate_identity(insight_bundle, anomaly_bundle, "InsightBundle")
    if "kb_version" not in insight_bundle or insight_bundle.get("kb_version") != evidence_bundle.get("kb_version"):
        raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "InsightBundle必须携带EvidenceLedger使用的kb_version")
    groups = {str(item["anomaly_group_id"]): item for item in anomaly_bundle.get("anomaly_groups", [])}
    selected = {str(case["insight_case_id"]): [str(item) for item in case.get("anomaly_group_ids", [])] for case in plan.get("selected_cases", [])}
    ledgers = {str(item["insight_case_id"]): item for item in evidence_bundle.get("cases", [])}
    if set(selected) != set(ledgers):
        raise ContractError("LEDGER_CASE_MISMATCH", "EvidenceLedger必须覆盖所有selected case")
    insights = [require_mapping(item, "insight") for item in require_list(insight_bundle.get("insights"), "insights")]
    monitors = [require_mapping(item, "monitor_item") for item in require_list(insight_bundle.get("monitor_items"), "monitor_items")]
    validate_disclosure(insight_bundle,plan,anomaly_bundle)
    decision_owner: dict[str, str] = {}
    reported_regions = {i.get('primary_scope', {}).get('dimension_key') for i in insights if i.get('primary_scope', {}).get('dimension_type') == 'REGION'}

    for index, insight in enumerate(insights):
        path = f"insights[{index}]"
        case_id = require_text(insight.get("insight_case_id"), f"{path}.insight_case_id")
        supporting_case_ids = _ids(insight.get("supporting_case_ids", []), f"{path}.supporting_case_ids")
        story_case_ids = [case_id, *supporting_case_ids]
        if len(story_case_ids) != len(set(story_case_ids)):
            raise ContractError("CASE_DECISION_INVALID", "主case不得在supporting_case_ids中重复", {"insight_case_id": case_id})
        unknown_cases = sorted(set(story_case_ids) - set(selected))
        repeated_cases = []  # Shared cases are legal across distinct issues; claims have unique owners.
        if unknown_cases or repeated_cases:
            raise ContractError("CASE_DECISION_INVALID", "主case或支撑case不存在、重复，或已被其他洞察使用", {"unknown_cases": unknown_cases, "repeated_cases": repeated_cases})
        if insight.get("issue_type") not in {"REGION_CONTRIBUTION","BRAND_TREND","INDEPENDENT_RISK"}:raise ContractError("ISSUE_TYPE_INVALID","主题类型不受支持")
        scope=require_mapping(insight.get("primary_scope"),f"{path}.primary_scope")
        primary_plan=next(c for c in plan["selected_cases"] if c["insight_case_id"]==case_id)
        if insight.get("issue_id")!=primary_plan.get("issue_id") or scope!=primary_plan.get("primary_scope"):
            raise ContractError("ISSUE_IDENTITY_CHANGED","主题身份与主范围必须与计划一致")
        require_text(insight.get("issue_type"), f"{path}.issue_type")
        require_text(insight.get("comparison_basis"), f"{path}.comparison_basis")
        require_text(insight.get("independent_issue_reason"), f"{path}.independent_issue_reason")
        for story_case_id in story_case_ids:
            decision_owner[story_case_id] = "REPORT"
        anomaly_ids = _ids(insight.get("anomaly_group_ids"), f"{path}.anomaly_group_ids")
        story_anomaly_ids = [group_id for story_case_id in story_case_ids for group_id in selected[story_case_id]]
        if set(anomaly_ids) != set(story_anomaly_ids):
            raise ContractError("CASE_MEMBERS_CHANGED", "综合洞察的anomaly_group_ids必须等于主case与支撑case成员并集", {"insight_case_id": case_id, "story_case_ids": story_case_ids})
        require_text(insight.get("summary"), f"{path}.summary")
        require_text(insight.get("title"), f"{path}.title")
        body = require_text(insight.get("narrative"), f"{path}.narrative")
        validate_action_delivery(insight, path)
        if any(term in body for term in ("进一步下钻", "结合现有正式证据", "贡献查询已返回", "insight_case_id", "fact_id", "ACTIVITY_RESPONSE")):
            raise ContractError("BUSINESS_NARRATIVE_REQUIRED", "正文应直接描述业务发现，不包含工具过程或内部字段")
        for field in ("basis", "limitations"):
            if not isinstance(insight.get("analysis_note", {}).get(field), str):
                raise ContractError("ANALYSIS_NOTE_REQUIRED", "analysis_note需要简短basis与limitations文本")
        impact_assessment = require_mapping(insight.get("impact_assessment"), f"{path}.impact_assessment")
        impact_type = str(impact_assessment.get("type", "")).upper()
        if impact_type not in IMPACT_TYPES:
            raise ContractError("IMPACT_ASSESSMENT_INVALID", "impact_assessment.type必须是REAL_OPERATION/POINTS_PARTICIPATION/MIXED/UNRESOLVED", {"path": path})
        require_text(impact_assessment.get("statement"), f"{path}.impact_assessment.statement")
        primary_driver = insight.get("primary_driver")
        if not isinstance(primary_driver, (dict, str)) or not any(_text_values(primary_driver)):
            raise ContractError("FIELD_INVALID", "primary_driver不能为空", {"path": path})
        require_list(insight.get("supporting_factors"), f"{path}.supporting_factors")
        confidence = str(insight.get("confidence", "")).upper()
        if confidence not in CONFIDENCES:
            raise ContractError("CONFIDENCE_INVALID", "confidence必须是HIGH/MEDIUM/LOW", {"path": path})
        require_text(insight.get("confidence_reason"), f"{path}.confidence_reason")
        if not require_list(insight.get("verification_metrics"), f"{path}.verification_metrics"):
            raise ContractError("VERIFICATION_METRICS_REQUIRED", "每条正式洞察必须提供验证指标", {"insight_case_id": case_id})
        follow_up_items = require_list(insight.get("follow_up_items"), f"{path}.follow_up_items")
        if confidence == "LOW" and not follow_up_items:
            raise ContractError("FOLLOW_UP_REQUIRED", "低置信洞察必须列出具体待核查项", {"insight_case_id": case_id})
        ledger = _merge_story_ledgers(story_case_ids, ledgers)
        signal_ids = _ids(insight.get("signal_ids"), f"{path}.signal_ids")
        fact_ids = _ids(insight.get("fact_ids"), f"{path}.fact_ids")
        activity_ids = _ids(insight.get("activity_ids"), f"{path}.activity_ids")
        invalid = {
            "signal_ids": sorted(set(signal_ids) - set(ledger.get("signal_ids", []))),
            "fact_ids": sorted(set(fact_ids) - set(ledger.get("fact_ids", []))),
            "activity_ids": sorted(set(activity_ids) - set(ledger.get("activity_ids", []))),
        }
        if any(invalid.values()):
            raise ContractError("EVIDENCE_REFERENCE_INVALID", "证据ID必须属于当前洞察声明的主case或支撑case", {"insight_case_id": case_id, "story_case_ids": story_case_ids, **invalid})
        _validate_total_region_disclosure(insight, ledger)
        _validate_operating_disclosure(insight, ledger)
        _validate_unreported_region_operations(insight, ledger, reported_regions)
        if len(story_case_ids) > 1:
            referenced_ids = set(signal_ids) | set(fact_ids) | set(activity_ids)
            unreferenced_cases = [
                story_case_id
                for story_case_id in story_case_ids
                if not referenced_ids.intersection(
                    set(ledgers[story_case_id].get("signal_ids", []))
                    | set(ledgers[story_case_id].get("fact_ids", []))
                    | set(ledgers[story_case_id].get("activity_ids", []))
                )
            ]
            if unreferenced_cases:
                raise ContractError("STORY_SUPPORT_UNREFERENCED", "综合洞察中的每个主case和支撑case必须至少贡献一条被引用证据", {"insight_case_id": case_id, "unreferenced_cases": unreferenced_cases})
        if evidence_bundle.get("knowledge_base_status") == "unavailable" and activity_ids:
            raise ContractError("ACTIVITY_REFERENCE_INVALID", "知识库不可用时不得引用活动证据")
        narrative = " ".join(_text_values({key: insight.get(key) for key in ("title", "narrative", "summary", "impact_assessment", "primary_driver", "supporting_factors", "confidence_reason")}))
        if any(term in narrative for term in STRONG_CAUSAL_TERMS):
            direct_ids = primary_driver.get("evidence_ids", []) if isinstance(primary_driver, dict) else []
            strength = str(primary_driver.get("evidence_strength", "")).upper() if isinstance(primary_driver, dict) else ""
            if confidence != "HIGH" or strength != "DIRECT" or not direct_ids:
                raise ContractError("STRONG_CAUSAL_UNSUPPORTED", "强因果措辞必须由高置信直接证据支持", {"insight_case_id": case_id})
            if not set(direct_ids).issubset(set(fact_ids) | set(activity_ids) | set(signal_ids)):
                raise ContractError("EVIDENCE_REFERENCE_INVALID", "primary_driver.evidence_ids必须已在当前洞察引用")

        recommendation = require_mapping(insight.get("recommendation"), f"{path}.recommendation")
        if not isinstance(recommendation.get("applicable"), bool):
            raise ContractError("RECOMMENDATION_INVALID", "recommendation.applicable必须是布尔值", {"path": path})
        if recommendation["applicable"]:
            action_kind = insight["action_kind"]
            if confidence == "LOW" and action_kind == "CAMPAIGN":
                raise ContractError("RECOMMENDATION_UNSUPPORTED", "低置信不得生成营销刺激建议", {"insight_case_id": case_id})
            for field in ("target", "action", "channel", "timing"):
                require_text(recommendation.get(field), f"{path}.recommendation.{field}")
            if str(recommendation["channel"]).strip() not in CHANNELS:
                raise ContractError("RECOMMENDATION_CHANNEL_INVALID", "建议渠道不在允许范围内", {"channel": recommendation["channel"]})
            supporting = _ids(recommendation.get("fact_ids"), f"{path}.recommendation.fact_ids")
            if not supporting or not set(supporting).issubset(set(fact_ids)):
                raise ContractError("RECOMMENDATION_UNSUPPORTED", "可执行建议必须引用当前case的调查事实")
            if not isinstance(primary_driver, dict):
                raise ContractError("RECOMMENDATION_UNSUPPORTED", "可执行建议必须先定位结构化主因")
            driver_strength = str(primary_driver.get("evidence_strength", "")).upper()
            driver_ids = _ids(primary_driver.get("evidence_ids", []), f"{path}.primary_driver.evidence_ids")
            if not set(driver_ids).issubset(set(signal_ids) | set(fact_ids) | set(activity_ids)):
                raise ContractError("EVIDENCE_REFERENCE_INVALID", "主因证据必须已在本主题声明", {"insight_case_id": case_id})
            if action_kind == "CAMPAIGN" and (driver_strength not in {"DIRECT", "AGGREGATE"} or not driver_ids):
                raise ContractError("RECOMMENDATION_UNSUPPORTED", "可执行建议必须有当前case证据支持的已定位主因")
            severe_quality = [item for item in ledger.get("data_quality", []) if isinstance(item, dict) and str(item.get("level", item.get("severity", ""))).upper() in {"ERROR", "CRITICAL", "FATAL"}]
            if severe_quality and action_kind in {"CAMPAIGN", "SERVICE"}:
                raise ContractError("RECOMMENDATION_UNSUPPORTED", "严重数据质量case不得生成营销刺激建议")
        else:
            require_text(recommendation.get("not_applicable_reason"), f"{path}.recommendation.not_applicable_reason")
        cited = dict(ledger, facts=[f for f in ledger.get("facts",[]) if f["fact_id"] in fact_ids], signals=[s for s in ledger.get("signals",[]) if s["signal_id"] in signal_ids])
        validate_activity_assessment(insight, cited['facts'])
        validate_tree(dict(insight,week_id=anomaly_bundle["week_id"]),{f["fact_id"]:f for f in cited["facts"]},anomaly_bundle.get("brand_regions",{}))
        story_groups = [groups[group_id] for group_id in story_anomaly_ids]
        _validate_narrative_numbers(insight, _official_number_tokens(cited, story_groups), path,
                                    _entity_labels(cited['facts'], story_groups))

    for index, monitor in enumerate(monitors):
        path = f"monitor_items[{index}]"
        case_id = require_text(monitor.get("insight_case_id"), f"{path}.insight_case_id")
        if case_id not in selected or case_id in decision_owner:
            raise ContractError("CASE_DECISION_INVALID", "case不存在或出现多次", {"insight_case_id": case_id})
        if any(groups[group_id].get("material") for group_id in selected[case_id]) and not any(set(q.get("anomaly_group_ids",[])) & set(selected[case_id]) for q in insight_bundle.get("data_quality_items",[])):
            raise ContractError("TOTAL_RED_MONITOR_ONLY", "含Total红色异常的case必须优先REPORT", {"insight_case_id": case_id})
        decision_owner[case_id] = "MONITOR_ONLY"
        if set(_ids(monitor.get("anomaly_group_ids"), f"{path}.anomaly_group_ids")) != set(selected[case_id]):
            raise ContractError("CASE_MEMBERS_CHANGED", "monitor item不得改变case成员", {"insight_case_id": case_id})
        require_text(monitor.get("reason"), f"{path}.reason")
        if not require_list(monitor.get("verification_metrics"), f"{path}.verification_metrics"):
            raise ContractError("MONITOR_INVALID", "MONITOR_ONLY必须提供验证指标", {"insight_case_id": case_id})
        ledger = ledgers[case_id]
        if not set(_ids(monitor.get("signal_ids"), f"{path}.signal_ids")).issubset(set(ledger.get("signal_ids", []))) or not set(_ids(monitor.get("fact_ids"), f"{path}.fact_ids")).issubset(set(ledger.get("fact_ids", []))):
            raise ContractError("EVIDENCE_REFERENCE_INVALID", "MONITOR_ONLY证据必须属于当前case", {"insight_case_id": case_id})
        monitor_groups = [groups[group_id] for group_id in selected[case_id]]
        _validate_narrative_numbers(monitor, _official_number_tokens(ledger, monitor_groups), path,
                                    _entity_labels(ledger.get('facts', []), monitor_groups))

    for index,summary in enumerate(insight_bundle.get("summary_items",[])):
        linked=[i for i in insights if i.get("issue_id") in summary.get("related_insight_ids",[])]
        fids={f for i in linked for f in i.get("fact_ids",[])};sids={sid for i in linked for sid in i.get("signal_ids",[])}
        allowed=_official_number_tokens({"facts":[f for ledger in ledgers.values() for f in ledger.get("facts",[]) if f["fact_id"] in fids],"signals":[sig for ledger in ledgers.values() for sig in ledger.get("signals",[]) if sig["signal_id"] in sids]},[g for g in groups.values() if any(g["anomaly_group_id"] in i.get("anomaly_group_ids",[]) for i in linked)])
        summary_facts = [f for ledger in ledgers.values() for f in ledger.get('facts', []) if f['fact_id'] in fids]
        summary_groups = [g for g in groups.values() if any(g['anomaly_group_id'] in i.get('anomaly_group_ids', []) for i in linked)]
        _validate_narrative_numbers({"text":summary.get("text")},allowed,f"summary_items[{index}]",
                                    _entity_labels(summary_facts, summary_groups))
    missing_cases = sorted(set(selected) - set(decision_owner))
    if missing_cases:
        raise ContractError("CASE_DECISION_MISSING", "所有selected case必须进入REPORT或MONITOR_ONLY", {"missing": missing_cases})
    run_status = require_mapping(insight_bundle.get("run_status"), "run_status")
    for field in ("data_quality", "attribution", "knowledge_base"):
        require_text(run_status.get(field), f"run_status.{field}")
    if str(evidence_bundle.get("knowledge_base_status", "")).lower() == "unavailable" and str(run_status.get("knowledge_base", "")).upper() not in {"UNAVAILABLE", "DEGRADED"}:
        raise ContractError("RUN_STATUS_INVALID", "知识库不可用时run_status必须标记UNAVAILABLE或DEGRADED")
    incomplete=any(ledger.get("unavailable_evidence") for ledger in ledgers.values())
    if incomplete and str(run_status.get("attribution","")).upper() in {"COMPLETE","SUCCESS","FULL","OK"}:
        raise ContractError("RUN_STATUS_INVALID","存在未完成证据项时必须披露PARTIAL/DEGRADED，不能声明完整")
    covered_report_case_count = sum(status == "REPORT" for status in decision_owner.values())
    return {"ok": True, "schema_version": "3.0", "report_case_count": len(insights), "covered_report_case_count": covered_report_case_count, "monitor_case_count": len(monitors), "selected_case_count": len(selected)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anomalies", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--insights", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        result = validate_insight_bundle(require_mapping(read_json(args.anomalies), "anomaly_bundle"), require_mapping(read_json(args.plan), "plan"), require_mapping(read_json(args.evidence), "evidence"), require_mapping(read_json(args.insights), "insight_bundle"))
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContractError, OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        error = exc.as_dict() if isinstance(exc, ContractError) else {"code": "INSIGHT_BUNDLE_INVALID", "message": str(exc), "details": {}}
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
