#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import hashlib
import time
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Any


def _runtime_executable(candidate: str) -> str:
    """Return an executable path without dereferencing virtualenv symlinks."""
    expanded = os.path.expanduser(candidate)
    return os.path.abspath(shutil.which(expanded) or expanded)


def _ensure_runtime() -> None:
    candidates = [os.environ.get("AICRM_PYTHON"), sys.executable, shutil.which("python3.13"), shutil.which("python3.12"), shutil.which("python3.11"), shutil.which("python3.10")]
    for candidate in dict.fromkeys(item for item in candidates if item):
        executable = _runtime_executable(candidate)
        try:
            check = subprocess.run([executable, "-c", "import sys; assert sys.version_info >= (3, 10); import pandas, openpyxl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            continue
        if check.returncode:
            continue
        if os.path.normcase(executable) == os.path.normcase(_runtime_executable(sys.executable)):
            return
        environment = dict(os.environ)
        environment["AICRM_PYTHON"] = executable
        os.execve(executable, [executable, os.path.abspath(__file__), *sys.argv[1:]], environment)
    raise RuntimeError("需要Python 3.10+、pandas和openpyxl；可通过AICRM_PYTHON指定虚拟环境解释器。")


_ensure_runtime()

from selection import select_issues, default_plan
from common import ContractError, read_json, require_mapping, write_json  # noqa: E402
from writing_delivery import assemble_draft, write_workspace


WEEKLY_SKILL_ROOT = Path(__file__).absolute().parents[1]
PACKAGE_VERSION = "3.2.12"
STAGE_TIMINGS = []
SKILL_MANIFEST = "aicrm-skill.json"
SKILL_ENTRYPOINTS = {
    "crm-metrics": "scripts/run_metrics.py",
    "crm-anomaly-detection": "scripts/run_anomaly_detection.py",
    "crm-activity-kb": "scripts/run_activity_kb.py",
    "crm-data-investigation": "scripts/run_investigation.py",
    "crm-report-output": "scripts/run_report_output.py",
}
SKILL_ASSETS = {
    "crm-metrics": (),
    "crm-anomaly-detection": (),
    "crm-activity-kb": ("assets/activity_knowledge_base.xlsx",),
    "crm-data-investigation": (),
    "crm-report-output": ("assets/周报模板.xlsx",),
}
SKILL_REQUIREMENTS = {
    name: ("SKILL.md", entrypoint, *SKILL_ASSETS[name])
    for name, entrypoint in SKILL_ENTRYPOINTS.items()
}
VALIDATE_PLAN_SCRIPT = Path(__file__).resolve().parent / "validate_plan.py"
EXECUTE_SCRIPT = Path(__file__).resolve().parent / "execute_investigation.py"
LEDGER_SCRIPT = Path(__file__).resolve().parent / "build_evidence_ledger.py"
VALIDATE_INSIGHTS_SCRIPT = Path(__file__).resolve().parent / "validate_insights.py"


def _normal_path(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.absolute()


def _validate_skill_roots(roots: dict[str, Path]) -> dict[str, Path]:
    missing_skills = sorted(set(SKILL_REQUIREMENTS) - set(roots))
    missing_files: dict[str, list[str]] = {}
    manifest_mismatches: dict[str, str | None] = {}
    for name, requirements in SKILL_REQUIREMENTS.items():
        root = roots.get(name)
        if root is None:
            continue
        if (root / SKILL_MANIFEST).is_file():
            declared_name = _manifest_skill_name(root)
            if declared_name != name:
                manifest_mismatches[name] = declared_name
        absent = [relative for relative in requirements if not (root / relative).is_file()]
        if absent:
            missing_files[name] = absent
    if missing_skills or missing_files or manifest_mismatches:
        raise ContractError("SKILL_DISCOVERY_FAILED", "无法定位完整的CRM技能集合", {"missing_skills": missing_skills, "missing_files": missing_files, "manifest_mismatches": manifest_mismatches})
    return {name: _normal_path(roots[name]) for name in SKILL_REQUIREMENTS}


def _load_skill_map(value: str) -> dict[str, Path]:
    raw = value.strip()
    base = Path.cwd()
    try:
        if raw.startswith("{"):
            payload = json.loads(raw)
        else:
            source = _normal_path(raw)
            if not source.is_file():
                raise ContractError("SKILL_MAP_INVALID", "技能路径映射文件不存在", {"path": str(source)})
            payload = json.loads(source.read_text(encoding="utf-8"))
            base = source.parent
    except json.JSONDecodeError as exc:
        raise ContractError("SKILL_MAP_INVALID", "技能路径映射不是有效JSON", {"message": str(exc)}) from exc
    if isinstance(payload, dict) and isinstance(payload.get("skills"), dict):
        payload = payload["skills"]
    if not isinstance(payload, dict):
        raise ContractError("SKILL_MAP_INVALID", "技能路径映射必须是JSON对象")
    roots = {str(name): _normal_path(str(path), base=base) for name, path in payload.items() if name in SKILL_REQUIREMENTS and isinstance(path, str)}
    return _validate_skill_roots(roots)


def _manifest_skill_name(root: Path) -> str | None:
    manifest = root / SKILL_MANIFEST
    if not manifest.is_file():
        return root.name if root.name in SKILL_REQUIREMENTS else None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("SKILL_MANIFEST_INVALID", "CRM技能清单无法读取", {"path": str(manifest), "message": str(exc)}) from exc
    if not isinstance(payload, dict):
        raise ContractError("SKILL_MANIFEST_INVALID", "CRM技能清单必须是JSON对象", {"path": str(manifest)})
    name = payload.get("name")
    if name not in SKILL_REQUIREMENTS:
        return None
    errors: dict[str, Any] = {}
    if payload.get("schema_version") != "1.0":
        errors["schema_version"] = {"expected": "1.0", "actual": payload.get("schema_version")}
    if payload.get("package_version") != PACKAGE_VERSION:
        errors["package_version"] = {"expected": PACKAGE_VERSION, "actual": payload.get("package_version")}
    expected_entrypoint = SKILL_ENTRYPOINTS[name]
    if payload.get("entrypoint") != expected_entrypoint:
        errors["entrypoint"] = {"expected": expected_entrypoint, "actual": payload.get("entrypoint")}
    assets = payload.get("assets", [])
    expected_assets = list(SKILL_ASSETS[name])
    if not isinstance(assets, list) or any(not isinstance(item, str) for item in assets):
        errors["assets"] = {"expected": expected_assets, "actual": assets}
    elif sorted(assets) != sorted(expected_assets):
        errors["assets"] = {"expected": expected_assets, "actual": assets}
    if errors:
        raise ContractError("SKILL_MANIFEST_INVALID", "CRM技能清单字段不符合当前发布契约", {"path": str(manifest), "errors": errors})
    return name


def _candidate_skill_directories(container: Path) -> list[Path]:
    if not container.is_dir():
        return []
    candidates: list[Path] = []
    try:
        children = sorted((child for child in container.iterdir() if child.is_dir()), key=lambda child: child.name)
    except OSError:
        return []
    candidates.extend(children)
    for child in children:
        nested = child / ".codebuddy" / "skills"
        if nested.is_dir():
            try:
                candidates.extend(sorted((item for item in nested.iterdir() if item.is_dir()), key=lambda item: item.name))
            except OSError:
                continue
    return candidates


def _discover_in_container(container: Path) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for candidate in _candidate_skill_directories(container):
        name = _manifest_skill_name(candidate)
        if name is None:
            continue
        if name in roots and roots[name] != candidate:
            raise ContractError("SKILL_DISCOVERY_AMBIGUOUS", "发现重复的CRM技能资源", {"skill": name, "paths": [str(roots[name]), str(candidate)]})
        roots[name] = candidate.absolute()
    return roots


def resolve_pipeline_resources(*, skill_map: str | None = None, weekly_skill_root: Path = WEEKLY_SKILL_ROOT, project_resources_root: str | Path | None = None) -> dict[str, Path]:
    """Resolve dependencies in named bundles or WorkBuddy UUID resource layouts."""
    if skill_map:
        roots = _load_skill_map(skill_map)
    else:
        containers = [weekly_skill_root.absolute().parent]
        if project_resources_root:
            containers.append(_normal_path(project_resources_root))
        containers.extend(ancestor for ancestor in weekly_skill_root.absolute().parents if ancestor.name == "project-resources")
        roots = {}
        seen: set[Path] = set()
        diagnostics: list[dict[str, Any]] = []
        for container in containers:
            container = container.absolute()
            if container in seen:
                continue
            seen.add(container)
            discovered = _discover_in_container(container)
            diagnostics.append({"container": str(container), "skills": sorted(discovered)})
            if set(SKILL_REQUIREMENTS).issubset(discovered):
                roots = discovered
                break
        if not roots:
            best = max(diagnostics, key=lambda item: len(item["skills"]), default={"skills": []})
            missing_skills = sorted(set(SKILL_REQUIREMENTS) - set(best["skills"]))
            raise ContractError("SKILL_DISCOVERY_FAILED", "无法在整包目录或WorkBuddy UUID资源目录中找到完整的CRM技能集合", {"missing_skills": missing_skills, "searched": diagnostics, "hint": "可通过--skill-map或AICRM_SKILL_MAP提供显式映射"})
        roots = _validate_skill_roots(roots)
    return {
        "metrics_script": roots["crm-metrics"] / "scripts" / "run_metrics.py",
        "anomaly_script": roots["crm-anomaly-detection"] / "scripts" / "run_anomaly_detection.py",
        "activity_script": roots["crm-activity-kb"] / "scripts" / "run_activity_kb.py",
        "activity_workbook": roots["crm-activity-kb"] / "assets" / "activity_knowledge_base.xlsx",
        "investigation_script": roots["crm-data-investigation"] / "scripts" / "run_investigation.py",
        "report_script": roots["crm-report-output"] / "scripts" / "run_report_output.py",
        "template": roots["crm-report-output"] / "assets" / "周报模板.xlsx",
    }


def _run(command: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, env=dict(os.environ), timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise ContractError("STAGE_TIMEOUT","阶段超过600秒",{"stage":Path(command[1]).name}) from exc
    STAGE_TIMINGS.append({"stage":Path(command[1]).name,"seconds":round(time.perf_counter()-started,4),"returncode":completed.returncode})
    if completed.returncode and not allow_failure:
        message = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
        raise ContractError("STAGE_FAILED", "阶段执行失败", {"command": Path(command[1]).name if len(command) > 1 else command[0], "message": message})
    return completed


def _planning_context(anomalies: dict[str, Any], knowledge_base: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "phase": "INVESTIGATION_PLANNING",
        "week_id": anomalies.get("week_id"),
        "input_signature": anomalies.get("input_signature"),
        "implementation_version": anomalies.get("implementation_version"),
        "knowledge_base_status": knowledge_base.get("knowledge_base_status", "unavailable"),
        "kb_version": knowledge_base.get("kb_version"),
        "instructions": [
            "程序已按CRM贡献、趋势和重大例外筛选并生成recommended_plan.json；先执行充分的默认调查包。",
            "模型可在重要性或解释缺口需要时调整主题和查询；不用逐条重写低影响观察理由。",
            "普通品牌CORE_METRICS+CARD_TIER；指定三类高奢CORE_METRICS+AMOUNT_BAND；按区域限定品牌。",
            "默认一轮调查与综合；只有可能改变主因或动作的证据缺口才追加一轮。"
        ],
        "issues": anomalies.get("issues", []),
        "contribution_tree": anomalies.get("contribution_tree", []),
        "reporting_policy": anomalies.get("reporting_policy", {}),
        "brand_regions": anomalies.get("brand_regions", {}),
        "candidate_pool": {
            "anomaly_groups": [g for g in anomalies.get("anomaly_groups", []) if g.get("investigation_eligible")],
            "anomaly_signals": [sg for sg in anomalies.get("anomaly_signals", []) if sg.get("anomaly_group_id") in {g["anomaly_group_id"] for g in anomalies.get("anomaly_groups", []) if g.get("investigation_eligible")}],
            "attribution_context": [],  # Read fresh scoped operating and band facts after investigation.
            "data_quality_status": anomalies.get("data_quality_status", []),
        },
        "query_registry": {
            "focus": ["CONTRIBUTION", "MEMBER_BEHAVIOR", "BRAND_OPERATION"],
            "comparison_basis": ["WOW", "YOY", "YTD", "CONTINUOUS_PERIOD", "EIGHT_WEEK_BASELINE"],
            "scope": {"dimension_type": "TOTAL|REGION|CARD|REGION_CARD|BRAND", "dimension_key": "string"},
            "member_behavior_analysis_items": ["CORE_METRICS", "MEMBER_COUNT", "TRANSACTIONS", "TICKET_SIZE", "CARD_TIER", "NEW_EXISTING", "LARGE_SPEND", "ACTIVITY_RESPONSE", "AMOUNT_BAND", "WEEKLY_HISTORY"],
            "activity_response_filters": {
                "activity_id": "optional activity_id",
                "activity_window": {"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"},
                "baseline_weeks": 4, "excluded_windows": [], "baseline_event_check": "UNVERIFIED", "baseline_context_sources": [],
                "baseline_window": "optional; defaults to the equally long preceding window",
                "post_window": "optional; defaults to the equally long following window",
            },
            "max_top_k": 20,
        },
        "output_schema": {
            "schema_version": "3.0",
            "week_id": anomalies.get("week_id"),
            "input_signature": anomalies.get("input_signature"),
            "implementation_version": anomalies.get("implementation_version"),
            "kb_version": knowledge_base.get("kb_version"),
            "selected_cases": [{"insight_case_id": "IC-...", "anomaly_group_ids": ["AG-..."], "selection_reason": "...", "queries": [], "activity_queries": []}],
            "omitted_anomaly_groups": [{"anomaly_group_id": "AG-...", "reason": "..."}],
        },
    }


def _model_fact_projection(fact: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "fact_id",
        "metric_id",
        "metric",
        "scope",
        "comparison_basis",
        "display_value",
        "display_change",
        "display_unit",
        "display_fields",
        "scope_details",
        "periods",
        "coverage_status",
        "applicability_limits",
        "activity_context",
    )
    projected = {key: fact.get(key) for key in allowed if key in fact}
    if not isinstance(projected.get("display_fields"), dict):
        projected["display_fields"] = {
            "current": fact.get("display_value"),
            "change": fact.get("display_change"),
        }
    return projected


def _model_evidence_projection(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    projected_cases: list[dict[str, Any]] = []
    for raw_case in evidence.get("cases", []):
        if not isinstance(raw_case, dict):
            continue
        activities = []
        for raw_activity in raw_case.get("activities", []):
            if not isinstance(raw_activity, dict):
                continue
            activities.append({key: value for key, value in raw_activity.items() if key != "retrieval_score"})
        projected_cases.append(
            {
                "insight_case_id": raw_case.get("insight_case_id"),
                "anomaly_group_ids": raw_case.get("anomaly_group_ids", []),
                "signal_ids": raw_case.get("signal_ids", []),
                "fact_ids": raw_case.get("fact_ids", []),
                "activity_ids": raw_case.get("activity_ids", []),
                "signals": raw_case.get("signals", []),
                "facts": [
                    _model_fact_projection(fact)
                    for fact in raw_case.get("facts", [])
                    if isinstance(fact, dict)
                ],
                "activities": activities,
                "data_quality": raw_case.get("data_quality", []),
                "unavailable_evidence": raw_case.get("unavailable_evidence", []),
                "coverage": raw_case.get("coverage", []),
                "analysis_item_statuses": raw_case.get("analysis_item_statuses", []),
            }
        )
    return projected_cases


def _story_case_projection(anomalies: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    groups = {
        str(item.get("anomaly_group_id")): item
        for item in anomalies.get("anomaly_groups", [])
        if isinstance(item, dict)
    }
    projected: list[dict[str, Any]] = []
    for raw_case in plan.get("selected_cases", []):
        if not isinstance(raw_case, dict):
            continue
        member_ids = [str(item) for item in raw_case.get("anomaly_group_ids", [])]
        members = [groups[group_id] for group_id in member_ids if group_id in groups]
        projected.append(
            {
                "insight_case_id": raw_case.get("insight_case_id"),
                "anomaly_group_ids": member_ids,
                "week_ids": sorted({str(item.get("week_id")) for item in members}),
                "dimension_types": sorted({str(item.get("dimension_type", "")).upper() for item in members}),
                "dimension_keys": sorted({str(item.get("dimension_key", "")) for item in members}),
                "metric_ids": sorted({str(item.get("metric_id", "")).upper() for item in members}),
                "severities": sorted({str(item.get("severity", item.get("group_severity", ""))).upper() for item in members}),
                "must_investigate": any(item.get("must_investigate") is True for item in members),
            }
        )
    return projected


def _synthesis_context(anomalies: dict[str, Any], plan: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    reporting_policy = anomalies.get("reporting_policy", {})
    if not isinstance(reporting_policy, dict):
        reporting_policy = {}
    return {
        "schema_version": "3.0",
        "phase": "INSIGHT_SYNTHESIS",
        "week_id": anomalies.get("week_id"),
        "input_signature": anomalies.get("input_signature"),
        "implementation_version": anomalies.get("implementation_version"),
        "reporting_policy": reporting_policy,
        "instructions": [
            "按独立业务问题综合证据，优先影响金额与风险。区域主题可有多个品牌分支；品牌持续趋势可单列并相互引用。",
            "每个主题使用issue_id/issue_type/primary_scope/comparison_basis/independent_issue_reason。多分支使用root_node_id/nodes/edges，范围与期间必须匹配。",
            "claims声明claim_id、claim_role、statement与primary_insight_id；相同事实可复用，同一结论只展开一次，父子销售影响不可累加。",
            "交付仅报告、洞察、分析说明。正文不限条数；超过八条完成去重复核。不生成summary_items。",
            "重大未解释风险必须披露；数据质量用data_quality_items单列，说明受影响判断。不得因名额或不能给出营销建议省略。",
            "仅引用正式展示数字及声明范围中的证据；不得把Capture数学变化当因果。客单价为周人均消费，笔单价为销售额/消费笔数。",
            "每条行动与归因分开判断：SERVICE日常服务、CONDITIONAL条件性合作、VERIFY核查、MONITOR观察、CAMPAIGN营销刺激。LOW仍可提出核查或条件性方案，不得直接营销刺激。",
            "优先读取writing_context索引和当前主题事实，用--draft自动组装结果；写作前读取四个完整few-shot、writing-policy及marketing-operations，并按其中的品牌活动合作和VIC服务分工生成建议；业务输出统一称为门店。title/narrative直接面向业务，analysis_note写简短依据和限制，程序不再拼接正文。"
        ],
        "selected_cases": plan.get("selected_cases", []),
        "case_hierarchy": _story_case_projection(anomalies, plan),
        "evidence_projection": "FORMAL_FACT_DISPLAY_FIELDS_ONLY",
        "evidence_ledgers": _model_evidence_projection(evidence),
        "run_context": {"knowledge_base_status": evidence.get("knowledge_base_status"), "kb_version": evidence.get("kb_version")},
        "output_schema": {
            "schema_version": "3.0",
            "week_id": anomalies.get("week_id"),
            "input_signature": anomalies.get("input_signature"),
            "implementation_version": anomalies.get("implementation_version"),
            "kb_version": evidence.get("kb_version"),
            "report_notes": ["共性数据口径，集中简短说明"],
            "data_quality_items": [{"reason":"仅有重大数据质量问题时填写", "affected_judgments":"受影响判断", "anomaly_group_ids":[]}],
            "deduplication_review": {"completed":False,"retained_issue_ids":[],"reason":"超过八条时提供"},
            "insights": [{"issue_id":"ISSUE-...", "issue_type":"REGION_CONTRIBUTION|BRAND_TREND|INDEPENDENT_RISK", "primary_scope":{}, "comparison_basis":"WOW", "independent_issue_reason":"独立问题价值", "claims":[{"claim_id":"CLAIM-...","claim_role":"CONTRIBUTION|TREND|BEHAVIOR|ACTION","statement":"结论","primary_insight_id":"ISSUE-..."}], "insight_case_id": "IC-PRIMARY", "supporting_case_ids": ["IC-REGION", "IC-BRAND", "IC-MEMBER-TIER"], "anomaly_group_ids": [], "title": "业务主题", "narrative": "分析段与行动段原文换段，由--draft生成", "action_text": "完整行动段", "action_kind": "SERVICE|CONDITIONAL|VERIFY|MONITOR|CAMPAIGN", "action_condition": "条件性合作或观察的触发条件，正文中可见", "analysis_note": {"basis": "简短判断依据", "limitations": "必要限制，没有则空字符串"}, "summary": "内部客观数据", "impact_assessment": {"type": "REAL_OPERATION|POINTS_PARTICIPATION|MIXED|UNRESOLVED", "statement": "经营与积分参与判断"}, "primary_driver": {}, "supporting_factors": [], "confidence": "HIGH|MEDIUM|LOW", "confidence_reason": "时间、范围、指标方向匹配情况及证据缺口", "verification_metrics": ["下一周复核指标"], "recommendation": {}, "follow_up_items": [], "signal_ids": [], "fact_ids": [], "activity_ids": []}],
            "monitor_items": [{"insight_case_id": "IC-...", "anomaly_group_ids": [], "reason": "...", "verification_metrics": [], "signal_ids": [], "fact_ids": []}],
            "run_status": {"data_quality": "...", "attribution": "...", "knowledge_base": "..."},
        },
    }


def _report_export_projection(
    plan: dict[str, Any],
    evidence: dict[str, Any],
    insight_bundle: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Collapse validated story cases for the unchanged single-case report exporter."""
    insights = [dict(item) for item in insight_bundle.get("insights", []) if isinstance(item, dict)]
    if not any(item.get("supporting_case_ids") for item in insights) and len({i.get("insight_case_id") for i in insights})==len(insights):
        return plan, evidence, insight_bundle

    plan_cases = {
        str(item.get("insight_case_id")): item
        for item in plan.get("selected_cases", [])
        if isinstance(item, dict)
    }
    evidence_cases = {
        str(item.get("insight_case_id")): item
        for item in evidence.get("cases", [])
        if isinstance(item, dict)
    }
    projected_plan = dict(plan)
    projected_evidence = dict(evidence)
    projected_insights = dict(insight_bundle)
    collapsed_plan_cases: list[dict[str, Any]] = []
    collapsed_evidence_cases: list[dict[str, Any]] = []

    def unique(values: list[Any]) -> list[Any]:
        output: list[Any] = []
        seen: set[str] = set()
        for value in values:
            key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
            if key not in seen:
                seen.add(key)
                output.append(value)
        return output

    for insight in insights:
        primary_id = str(insight.get("insight_case_id"))
        story_case_ids = [primary_id, *[str(item) for item in insight.get("supporting_case_ids", [])]]
        plan_case = dict(plan_cases[primary_id])
        plan_case["anomaly_group_ids"] = unique(
            [group_id for case_id in story_case_ids for group_id in plan_cases[case_id].get("anomaly_group_ids", [])]
        )
        export_id=str(insight["issue_id"])
        plan_case["insight_case_id"]=export_id
        insight["insight_case_id"]=export_id
        collapsed_plan_cases.append(plan_case)

        ledger = dict(evidence_cases[primary_id])
        ledger["insight_case_id"]=export_id
        ledger["anomaly_group_ids"] = list(plan_case["anomaly_group_ids"])
        for field in (
            "signal_ids", "fact_ids", "audit_record_ids", "activity_ids", "signals", "facts",
            "audit_records", "activities", "data_quality", "unavailable_evidence", "coverage",
        ):
            ledger[field] = unique(
                [value for case_id in story_case_ids for value in evidence_cases[case_id].get(field, [])]
            )
        collapsed_evidence_cases.append(ledger)

    monitor_ids = {
        str(item.get("insight_case_id"))
        for item in insight_bundle.get("monitor_items", [])
        if isinstance(item, dict)
    }
    for case_id in plan_cases:
        if case_id in monitor_ids:
            collapsed_plan_cases.append(dict(plan_cases[case_id]))
            collapsed_evidence_cases.append(dict(evidence_cases[case_id]))

    projected_plan["selected_cases"] = collapsed_plan_cases
    projected_evidence["cases"] = collapsed_evidence_cases
    projected_insights["insights"] = insights
    return projected_plan, projected_evidence, projected_insights


def _activity_queries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for case in plan.get("selected_cases", []):
        for item in case.get("activity_queries", []):
            query = dict(item)
            query["query"] = dict(query.get("query", {}))
            query["query"]["anomaly_group_id"] = query.get("anomaly_group_id")
            output.append(query)
    return output


def _run_activities(output_dir: Path, plan: dict[str, Any], round_number: int, activity_script: Path) -> Path:
    queries = _activity_queries(plan)
    result_path = output_dir / f"activity_investigation_round_{round_number}.json"
    if not queries:
        kb_status_path = output_dir / "knowledge_base_status.json"
        kb = read_json(kb_status_path) if kb_status_path.is_file() else {"knowledge_base_status": "unavailable", "kb_version": None}
        return write_json(result_path, {"schema_version": "3.0", "knowledge_base_status": kb.get("knowledge_base_status", "unavailable"), "kb_version": kb.get("kb_version"), "results": [], "errors": []})
    request_path = output_dir / f"activity_queries_round_{round_number}.json"
    write_json(request_path, {"queries": queries})
    completed = _run([sys.executable, str(activity_script), "--state-dir", str(output_dir / "activity-kb-state"), "search", "--input", str(request_path)], allow_failure=True)
    if completed.returncode:
        return write_json(result_path, {"schema_version": "3.0", "knowledge_base_status": "unavailable", "kb_version": None, "results": [], "errors": [{"message": completed.stderr.strip() or completed.stdout.strip()}]})
    raw = json.loads(completed.stdout)
    results = raw.get("results", [])
    for query, result in zip(queries, results):
        result["insight_case_id"] = query.get("insight_case_id")
        result["anomaly_group_id"] = query.get("anomaly_group_id")
    return write_json(result_path, raw)


def _invalidate_downstream(output_dir: Path) -> None:
    """A new prepare run invalidates every artifact derived from prior inputs."""
    names = (
        "investigation_plan.json",
        "plan_validation_attempts_round_1.json",
        "plan_validation_attempts_round_2.json",
        "evidence_ledger.json",
        "insight_synthesis_context.json",
        "insight_bundle.json",
        "insight_validation.json",
        "insight_validation_attempts.json",
        "final_summary.json",
        "writing_context.json",
        "writing_draft_template.json",
        "assembled_insights.json",
    )
    for name in names:
        (output_dir / name).unlink(missing_ok=True)
    for path in (output_dir / "writing").glob("case_*.json"):
        path.unlink()
    for pattern in (
        "investigation_round_*.json",
        "investigation_summary_round_*.json",
        "activity_investigation*.json",
        "activity_queries_round_*.json",
        "CRM周报_*.xlsx",
    ):
        for path in output_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def _prepare(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    resources = args.pipeline_resources
    week_start = date.fromisoformat(args.week_start)
    if week_start.weekday() != 0:
        raise ContractError("PERIOD_INVALID", "week_start必须是周一")
    source_files={k:str(Path(getattr(args,k)).expanduser().resolve()) for k in ("crm","mall_n","mall_s","mall_w")}
    if args.coverage_json: source_files["coverage_json"]=str(Path(args.coverage_json).expanduser().resolve())
    digest=hashlib.sha256(json.dumps({"week_start":args.week_start,"files":{k:hashlib.sha256(Path(v).read_bytes()).hexdigest() for k,v in source_files.items()}},sort_keys=True).encode()).hexdigest()
    # Include relevant implementation bytes; a code update cannot reuse stale metrics.
    metric_root=resources["metrics_script"].parents[1]
    metric_code=hashlib.sha256(b"".join(p.read_bytes() for p in sorted(metric_root.rglob("*.py")))).hexdigest()
    cache_key=digest+metric_code
    cache_path=output_dir/"prepare_cache.json"
    cached=read_json(cache_path) if cache_path.is_file() else {}
    metric_hit=cached.get("cache_key")==cache_key and all((output_dir/n).is_file() for n in ("metric_bundle.json","rich_metrics.json"))
    if metric_hit:
        metric_hit=all(cached.get("artifact_hashes",{}).get(n)==hashlib.sha256((output_dir/n).read_bytes()).hexdigest() for n in ("metric_bundle.json","rich_metrics.json"))
    analysis_files=[Path(__file__),Path(__file__).with_name("selection.py"),*sorted(resources["anomaly_script"].parents[1].rglob("*.py")),*sorted(resources["anomaly_script"].parents[1].rglob("*.json"))]
    analysis_key=hashlib.sha256(b"".join(p.read_bytes() for p in analysis_files)+(Path(args.activity_workbook).read_bytes() if Path(args.activity_workbook).is_file() else b"missing-kb")).hexdigest()
    if metric_hit and cached.get("analysis_key")!=analysis_key: _invalidate_downstream(output_dir)
    if not metric_hit: _invalidate_downstream(output_dir)
    write_json(output_dir/"source_manifest.json",dict(files=source_files,week_start=args.week_start))
    metric_path = output_dir / "metric_bundle.json"
    rich_path = output_dir / "rich_metrics.json"
    anomaly_path = output_dir / "anomaly_bundle.json"
    if not metric_hit:
        command=[sys.executable, str(resources["metrics_script"]), "--crm", args.crm, "--mall-n", args.mall_n, "--mall-s", args.mall_s, "--mall-w", args.mall_w, "--week-start", args.week_start, "--output", str(metric_path), "--rich-output", str(rich_path), "--audit-dir", str(output_dir)]
        if args.coverage_json:command.extend(["--coverage-json",args.coverage_json])
        _run(command)
    write_json(cache_path,{"cache_key":cache_key,"analysis_key":analysis_key,"artifact_hashes":{n:hashlib.sha256((output_dir/n).read_bytes()).hexdigest() for n in ("metric_bundle.json","rich_metrics.json")}})
    _run([sys.executable, str(resources["anomaly_script"]), "--metrics", str(metric_path), "--output", str(anomaly_path)])
    kb_completed = _run([sys.executable, str(resources["activity_script"]), "--workbook", str(Path(args.activity_workbook).expanduser().resolve()), "--state-dir", str(output_dir / "activity-kb-state"), "ensure"], allow_failure=True)
    try:
        kb_status = json.loads(kb_completed.stdout) if kb_completed.stdout.strip() else {"knowledge_base_status": "unavailable", "message": kb_completed.stderr.strip()}
    except json.JSONDecodeError:
        kb_status = {"knowledge_base_status": "unavailable", "message": kb_completed.stderr.strip() or kb_completed.stdout.strip()}
    write_json(output_dir / "knowledge_base_status.json", kb_status)
    anomalies = require_mapping(read_json(anomaly_path), "anomaly_bundle")
    anomalies=select_issues(anomalies,read_json(metric_path),read_json(rich_path))
    write_json(anomaly_path,anomalies)
    write_json(output_dir/"recommended_plan.json",default_plan(anomalies,kb_status.get("kb_version")))
    write_json(output_dir/"observation_records.json",anomalies.get("observations",[]))
    context = write_json(output_dir / "investigation_planning_context.json", _planning_context(anomalies, kb_status))
    summary = {"ok": True, "phase": "prepare", "metrics_cache_hit": metric_hit, "issue_count":len(anomalies.get("issues",[])), "week_id": anomalies.get("week_id"), "candidate_anomaly_count": len(anomalies.get("anomaly_groups", [])), "must_investigate_count": sum(item.get("must_investigate") is True for item in anomalies.get("anomaly_groups", [])), "planning_context": str(context), "knowledge_base_status": kb_status.get("knowledge_base_status", "unavailable")}
    write_json(output_dir / "prepare_summary.json", summary)
    return summary


def _investigate(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    if not args.plan:
        args.plan=str(output_dir/"recommended_plan.json")
    plan_path = Path(args.plan).expanduser().resolve()
    plan = require_mapping(read_json(plan_path), "plan")
    if args.round == 1:
        from validate_plan import validate_first_round
        validate_first_round(read_json(output_dir/"anomaly_bundle.json"),plan)
        plan_path=write_json(output_dir/"normalized_plan.json",plan)
    anomaly_path = output_dir / "anomaly_bundle.json"
    rich_path = output_dir / "rich_metrics.json"
    frozen_path = Path(args.frozen_plan).expanduser().resolve() if args.frozen_plan else output_dir / "investigation_plan.json"
    attempts_path = output_dir / f"plan_validation_attempts_round_{args.round}.json"
    attempts = read_json(attempts_path).get("attempts", 0) if attempts_path.is_file() else 0
    write_json(attempts_path, {"attempts": attempts + 1})
    if args.round == 1:
        kb_status_path = output_dir / "knowledge_base_status.json"
        kb_status = read_json(kb_status_path) if kb_status_path.is_file() else {"kb_version": None}
        if "kb_version" not in plan or plan.get("kb_version") != kb_status.get("kb_version"):
            raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "InvestigationPlan知识库版本与prepare阶段不一致")
        _run([sys.executable, str(VALIDATE_PLAN_SCRIPT), "--anomalies", str(anomaly_path), "--plan", str(plan_path)])
        frozen_path = write_json(output_dir / "investigation_plan.json", plan)
    else:
        _run([sys.executable, str(VALIDATE_PLAN_SCRIPT), "--plan", str(plan_path), "--frozen-plan", str(frozen_path)])
    resources = args.pipeline_resources
    requested={item for c in plan.get("selected_cases",[]) for q in c.get("queries",[]) for item in q.get("analysis_items",[])}
    extended=any(q.get("comparison_basis")=="YTD" and q.get("focus")=="MEMBER_BEHAVIOR" for c in plan.get("selected_cases",[]) for q in c.get("queries",[]))
    if "ACTIVITY_RESPONSE" in requested or extended:
        source=read_json(output_dir/"source_manifest.json")
        command=[sys.executable,str(resources["metrics_script"]),"--week-start",source["week_start"],"--output",str(output_dir/("detail_metric_bundle.json" if extended else "metric_bundle.json")),"--rich-output",str(rich_path)]
        for k,v in source["files"].items():command.extend(["--"+k.replace("_","-"),v])
        if "ACTIVITY_RESPONSE" in requested:command.append("--include-member-days")
        if extended:command.append("--include-extended-members")
        else:command.append("--details-only")
        _run(command)
        if read_json(output_dir/("detail_metric_bundle.json" if extended else "metric_bundle.json"))["input_signature"]!=read_json(anomaly_path)["input_signature"]:raise ContractError("ARTIFACT_IDENTITY_MISMATCH","源文件已变化，请重新prepare")
    investigation_path = output_dir / f"investigation_round_{args.round}.json"
    command = [sys.executable, str(EXECUTE_SCRIPT), "--metrics", str(rich_path), "--anomalies", str(anomaly_path), "--plan", str(plan_path), "--round", str(args.round), "--max-concurrency", str(args.max_concurrency), "--output", str(investigation_path), "--data-script", str(resources["investigation_script"])]
    if args.round == 2:
        command.extend(("--frozen-plan", str(frozen_path)))
    _run(command)
    activity_path = _run_activities(output_dir, plan, args.round, resources["activity_script"])
    investigation_paths = [output_dir / "investigation_round_1.json"]
    activity_paths = [output_dir / "activity_investigation_round_1.json"]
    if args.round == 2:
        investigation_paths.append(investigation_path)
        activity_paths.append(activity_path)
    combined_activity = {"schema_version": "3.0", "knowledge_base_status": "available", "kb_version": None, "results": [], "errors": []}
    for path in activity_paths:
        if not path.is_file():
            continue
        payload = read_json(path)
        if payload.get("knowledge_base_status") == "unavailable":
            combined_activity["knowledge_base_status"] = "unavailable"
        combined_activity["kb_version"] = combined_activity["kb_version"] or payload.get("kb_version")
        combined_activity["results"].extend(payload.get("results", []))
        combined_activity["errors"].extend(payload.get("errors", []))
    combined_activity_path = write_json(output_dir / "activity_investigation.json", combined_activity)
    evidence_path = output_dir / "evidence_ledger.json"
    ledger_command = [sys.executable, str(LEDGER_SCRIPT), "--anomalies", str(anomaly_path), "--plan", str(frozen_path)]
    for path in investigation_paths:
        ledger_command.extend(("--investigation", str(path)))
    ledger_command.extend(("--activities", str(combined_activity_path), "--output", str(evidence_path)))
    _run(ledger_command)
    anomalies = require_mapping(read_json(anomaly_path), "anomaly_bundle")
    frozen = require_mapping(read_json(frozen_path), "frozen_plan")
    evidence = require_mapping(read_json(evidence_path), "evidence")
    context_path = write_json(output_dir / "insight_synthesis_context.json", _synthesis_context(anomalies, frozen, evidence))
    writing = write_workspace(output_dir, read_json(context_path), frozen, evidence)
    summary = {"ok": True, "phase": "investigate", "round": args.round, "query_count": read_json(investigation_path).get("query_count"), "case_count": len(evidence.get("cases", [])), "synthesis_context": str(context_path), "knowledge_base_status": evidence.get("knowledge_base_status")}
    summary.update(writing)
    write_json(output_dir / f"investigation_summary_round_{args.round}.json", summary)
    return summary


def _writing(output_dir: Path) -> dict[str, Any]:
    plan = read_json(output_dir / "investigation_plan.json")
    evidence = read_json(output_dir / "evidence_ledger.json")
    context = _synthesis_context(read_json(output_dir / "anomaly_bundle.json"), plan, evidence)
    return dict(ok=True, phase="writing", **write_workspace(output_dir, context, plan, evidence))


def _finalize(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    resources = args.pipeline_resources
    if bool(args.insights) == bool(args.draft):
        raise ContractError("INPUT_MISSING", "finalize提供--draft或--insights其中一个；优先使用--draft")
    if args.draft:
        bundle = assemble_draft(read_json(args.draft), read_json(output_dir / "investigation_plan.json"), read_json(output_dir / "evidence_ledger.json"))
        insight_source = write_json(output_dir / "assembled_insights.json", bundle)
    else:
        insight_source = Path(args.insights).expanduser().resolve()
    attempts_path = output_dir / "insight_validation_attempts.json"
    attempts = read_json(attempts_path).get("attempts", 0) if attempts_path.is_file() else 0
    attempts += 1
    write_json(attempts_path, {"attempts": attempts})
    anomaly_path = output_dir / "anomaly_bundle.json"
    plan_path = output_dir / "investigation_plan.json"
    evidence_path = output_dir / "evidence_ledger.json"
    _run([sys.executable, str(VALIDATE_INSIGHTS_SCRIPT), "--anomalies", str(anomaly_path), "--plan", str(plan_path), "--evidence", str(evidence_path), "--insights", str(insight_source), "--output", str(output_dir / "insight_validation.json")])
    insight_bundle = require_mapping(read_json(insight_source), "insight_bundle")
    final_insight_path = write_json(output_dir / "insight_bundle.json", insight_bundle)
    metrics = require_mapping(read_json(output_dir / "metric_bundle.json"), "metric_bundle")
    week_start = date.fromisoformat(metrics["windows"]["current"]["start"])
    report_path = output_dir / f"CRM周报_{week_start.isoformat()}_{(week_start + timedelta(days=6)).isoformat()}.xlsx"
    plan = read_json(plan_path)
    evidence = read_json(evidence_path)
    report_plan, report_evidence, report_insights = _report_export_projection(plan, evidence, insight_bundle)
    if report_plan is plan:
        _run([sys.executable, str(resources["report_script"]), "--metrics", str(output_dir / "metric_bundle.json"), "--anomalies", str(anomaly_path), "--plan", str(plan_path), "--insights", str(final_insight_path), "--evidence", str(evidence_path), "--template", str(Path(args.template).expanduser().resolve()), "--output", str(report_path)])
    else:
        with tempfile.TemporaryDirectory(prefix="report-story-", dir=output_dir) as temporary_dir:
            temporary_root = Path(temporary_dir)
            export_plan_path = write_json(temporary_root / "plan.json", report_plan)
            export_evidence_path = write_json(temporary_root / "evidence.json", report_evidence)
            export_insights_path = write_json(temporary_root / "insights.json", report_insights)
            _run([sys.executable, str(resources["report_script"]), "--metrics", str(output_dir / "metric_bundle.json"), "--anomalies", str(anomaly_path), "--plan", str(export_plan_path), "--insights", str(export_insights_path), "--evidence", str(export_evidence_path), "--template", str(Path(args.template).expanduser().resolve()), "--output", str(report_path)])
    summary = {"ok": True, "phase": "finalize", "report": str(report_path), "insight_bundle": str(final_insight_path), "week_id": metrics.get("week_id"), "candidate_anomaly_count": len(read_json(anomaly_path).get("anomaly_groups", [])), "selected_anomaly_count": sum(len(case.get("anomaly_group_ids", [])) for case in plan.get("selected_cases", [])), "report_case_count": len(insight_bundle.get("insights", [])), "monitor_case_count": len(insight_bundle.get("monitor_items", [])), "fact_count": len({fid for case in evidence.get("cases", []) for fid in case.get("fact_ids", [])}), "audit_record_count": sum(len(case.get("audit_record_ids", [])) for case in evidence.get("cases", [])), "activity_evidence_count": sum(len(case.get("activity_ids", [])) for case in evidence.get("cases", [])), "kb_version": evidence.get("kb_version"), "run_status": insight_bundle.get("run_status", {})}
    write_json(output_dir / "final_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WorkBuddy内置模型主导的CRM周报编排")
    parser.add_argument("--phase", choices=("prepare", "investigate", "writing", "finalize"), required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--crm")
    parser.add_argument("--mall-n")
    parser.add_argument("--mall-s")
    parser.add_argument("--mall-w")
    parser.add_argument("--week-start")
    parser.add_argument("--coverage-json")
    parser.add_argument("--activity-workbook")
    parser.add_argument("--template")
    parser.add_argument("--skill-map", default=os.environ.get("AICRM_SKILL_MAP"), help="可选技能名到安装根目录的JSON对象或JSON文件")
    parser.add_argument("--project-resources-root", default=os.environ.get("AICRM_PROJECT_RESOURCES"), help="可选WorkBuddy project-resources目录")
    parser.add_argument("--plan")
    parser.add_argument("--frozen-plan")
    parser.add_argument("--round", type=int, choices=(1, 2), default=1)
    parser.add_argument("--max-concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--insights")
    parser.add_argument("--draft", help="由writing_draft_template填写的模型草稿，自动组装固定字段及完整正文")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started=time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    STAGE_TIMINGS.clear()
    try:
        args.pipeline_resources = resolve_pipeline_resources(skill_map=args.skill_map, project_resources_root=args.project_resources_root)
        args.activity_workbook = args.activity_workbook or str(args.pipeline_resources["activity_workbook"])
        args.template = args.template or str(args.pipeline_resources["template"])
        if args.phase == "prepare":
            missing = [name for name in ("crm", "mall_n", "mall_s", "mall_w", "week_start") if not getattr(args, name)]
            if missing:
                raise ContractError("INPUT_MISSING", "prepare缺少必要输入", {"missing": missing})
            result = _prepare(args, output_dir)
        elif args.phase == "investigate":
            result = _investigate(args, output_dir)
        elif args.phase == "writing":
            result = _writing(output_dir)
        else:
            result = _finalize(args, output_dir)
        result["elapsed_seconds"]=round(time.perf_counter()-started,4)
        result["started_at"] = started_at
        result["stage_timings"]=STAGE_TIMINGS
        write_json(output_dir/f"timing_{args.phase}.json",result)
        if args.phase == "investigate":
            write_json(output_dir/f"timing_investigate_round_{args.round}.json",result)
        with (output_dir / "phase_history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContractError, OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        error = exc.as_dict() if isinstance(exc, ContractError) else {"code": "CRM_WEEKLY_REPORT_FAILED", "message": str(exc), "details": {}}
        failure = {"ok":False,"phase":args.phase,"round":args.round if args.phase == "investigate" else None,"started_at":started_at,"error":error,"elapsed_seconds":round(time.perf_counter()-started,4),"stage_timings":STAGE_TIMINGS}
        write_json(output_dir/f"timing_{args.phase}.json",failure)
        if args.phase == "investigate":
            write_json(output_dir/f"timing_investigate_round_{args.round}.json",failure)
        with (output_dir / "phase_history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(failure, ensure_ascii=False) + "\n")
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
