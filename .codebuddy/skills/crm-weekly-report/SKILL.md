---
name: crm-weekly-report
description: 从CRM与Mall源文件生成自然周周报；程序筛选经营影响并执行品牌调查包，当前宿主模型综合证据、组织多品牌主题与趋势专项。
allowed-tools: Read, Write, Bash
---

# CRM周报入口

准备后读取程序生成的规划上下文；准备与调查命令不需要模型逐条重写规则。调整调查时读 [调查政策](references/investigation-policy.md)，综合输出时读 [写作与行动交付](references/action-delivery.md)；合并主题或调整字段时再读 [交付契约](references/insight-contract.md)，归因边界不清时读 [归因判断](references/attribution-policy.md)。仅在活动解释或置信度边界不明确时读对应 [活动政策](references/activity-attribution.md)、[置信度政策](references/confidence-policy.md)。最终写作前必须读取 [四个完整写作示例](references/attribution-examples.md) 与 [业务写作规则](references/writing-policy.md)。相关活动需要判断时，按活动政策使用ACTIVITY_RESPONSE比较活动期与历史相同星期。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_pipeline.py" --phase prepare \
  --crm <CRM.xlsx> --mall-n <北区.xlsx> --mall-s <南区.xlsx> --mall-w <西区.xlsx> \
  --week-start <周一YYYY-MM-DD> --output-dir <运行目录>
```

可加 `--coverage-json <覆盖清单.json>`；契约见 crm-metrics/references/contract.md。程序输出 `recommended_plan.json`、贡献树、精简候选上下文及完整观察记录。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_pipeline.py" --phase investigate --output-dir <运行目录>
```

默认执行推荐计划。需要调整时传 `--plan <JSON>`；第二轮只补查可能改变主因或动作的差异，传 `--round 2 --frozen-plan <第一轮计划>`。只修改策略/说明时可重新运行相应阶段，不因历史校验次数锁死。

先读 `writing_context.json` 索引，再逐主题读其中的事实文件。复制 `writing_draft_template.json` 为自己的草稿，模型填写分析、行动和判断；固定身份及结构由程序组装。参照用户认可的3.2.9洞察风格，细则见[写作与行动交付](references/action-delivery.md)。仅重做写作时用 `--phase writing --output-dir <运行目录>` 重建写作材料，无需重新调查。不要读取 `rich_metrics.json` 或完整 `evidence_ledger.json`；其中含审计用会员记录。

全场及区域正文保留crm sales、mall sales、Capture Ratio的同范围比较与判断，但按业务发现灵活组织行文；全场覆盖三区，未独立展开区域合并在全场正文。洞察后接符合本项目分工的营销建议，写作前读取[会员运营与品牌沟通规则](references/marketing-operations.md)，相关星钻卡与高潜黑钻卡变化应衔接VIC的接待、陪同到店和消费跟进，避免只写品牌活动而漏掉会员服务；细则见业务写作规则。已有可计算值正常展示，缺值说明单列。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_pipeline.py" --phase finalize --output-dir <运行目录> --draft <已填写草稿JSON>
```

仅交付三页Excel（报告、洞察、分析说明）；不默认交付中间JSON、证据或审计包。检查最终Excel中行动段是否完整、对象及条件是否正确；内部 `timing_investigate_round_*.json` 保留各轮，`phase_history.jsonl`保留失败与重试。完整旧InsightBundle仍可用--insights，但需补齐action_text/action_kind并通过新交付校验。缺失事实的分析项不能声明已完成；重要原因未明仍须披露。
