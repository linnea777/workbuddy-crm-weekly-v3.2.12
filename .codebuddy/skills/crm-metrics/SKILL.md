---
name: crm-metrics
description: 一次加载CRM与北/南/西区Mall Sales，指定任意合法周一后生成同口径MetricBundle和含按需会员聚合的RichMetricsBundle。用于源数据校验、M01–M14或后续异常检测准备；不负责异常解释。
allowed-tools: Read, Write, Bash
---

# CRM 指标与安全聚合

只运行本 Skill 的 `scripts/run_metrics.py`。需要字段、排除项或产物说明时读取 [references/contract.md](references/contract.md)。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_metrics.py" \
  --crm <CRM.xlsx> --mall-n <北区.xlsx> --mall-s <南区.xlsx> --mall-w <西区.xlsx> \
  --week-start <YYYY-MM-DD> \
  --output <metric_bundle.json> --rich-output <rich_metrics.json> \
  --audit-dir <输出目录>
```

正式入口没有默认日期；非周一必须失败。四个排除项在入口统一移除。Rich Metrics 可保留会员编号和会员×周期/自然日聚合，但不包含单笔交易；这些会员记录仅供确定性调查代码和报告审计使用，不得进入模型上下文，也不得从其他文件补读明细。
