---
name: crm-data-investigation
description: 只读RichMetricsBundle 3.0执行CONTRIBUTION、MEMBER_BEHAVIOR和BRAND_OPERATION受控调查，由代码计算会员、卡级、新老客、大额消费和活动响应事实，返回稳定fact_id及会员审计记录。不读取原始Excel或返回单笔交易。
allowed-tools: Read, Write, Bash
---

# CRM 受控数据调查

构造查询前读取 [references/contract.md](references/contract.md)。每次最多 50 条，超限由 `crm-weekly-report` 拆批。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_investigation.py" \
  --metrics <rich_metrics.json> \
  --request <investigation_request.json> \
  --output <investigation_result.json>
```

`fact_id` 使用周、case、异常、查询参数和正式值稳定生成，不依赖并发完成顺序。

`audit_records` 可包含会员编号与已计算的会员级聚合，仅在后台用于数值核验，不导出到交付Excel；归因模型只读取正式事实展示投影。

调查活动响应时读取 [活动期间对比](references/activity-comparison.md)，按相同星期比较；返回聚合事实，不返回会员名单。
