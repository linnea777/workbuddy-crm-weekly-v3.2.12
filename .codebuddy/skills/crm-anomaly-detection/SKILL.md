---
name: crm-anomaly-detection
description: 从MetricBundle确定性生成不截断的完整AnomalyBundle 3.0候选池，含严重度、正式展示字段、可调查维度和必查标记。用于异动检测或模型选题准备；不做Top-N选题、case或归因。
allowed-tools: Read, Write, Bash
---

# CRM 完整异常候选池

只读 `crm-metrics` 产物，需要契约时读取 [references/contract.md](references/contract.md)。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_anomaly_detection.py" \
  --metrics <metric_bundle.json> --output <anomaly_bundle.json>
```

不得输出 `selected_anomaly_groups` 或 `insight_case_id`。排除项残留会按严重数据质量错误阻断，不得引用其排名或结论。
