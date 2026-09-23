---
name: crm-report-output
description: 将已验证指标和模型撰写的自然中文洞察导出为报告、洞察、分析说明三个Sheet。不拼接业务判断，不交付审计附录。
allowed-tools: Read, Write, Bash
---

# CRM 报告输出

输入与运行方法见 [输出契约](references/contract.md)。保留原报告业务表格，洞察仅主题及narrative，分析说明仅简短依据/来源/置信度/限制。不写摘要、告警、索引、观察记录和会员审计证据。原始源文件和模板资源不覆盖。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_report_output.py" --metrics <metric_bundle.json> --anomalies <anomaly_bundle.json> --plan <investigation_plan.json> --insights <insight_bundle.json> --evidence <evidence_ledger.json> --output <CRM周报.xlsx>
```
