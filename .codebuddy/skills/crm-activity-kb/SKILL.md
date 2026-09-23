---
name: crm-activity-kb
description: 校验、索引、检索、查看版本或回滚CRM活动知识库。周报归因时返回活动ID、时间关系、范围、机制、匹配理由和归因限制；不把时间重合判为因果。
allowed-tools: Read, Write, Bash
---

# CRM 活动知识库

Excel 是唯一人工维护源。查询契约见 [references/contract.md](references/contract.md)。SQLite、日志和备份必须写到 `--state-dir` 指定的用户输出目录，不得写入 Skill 安装目录。

```bash
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_activity_kb.py" --state-dir <输出目录> ensure
"${AICRM_PYTHON:-python3}" "${CODEBUDDY_SKILL_DIR}/scripts/run_activity_kb.py" --state-dir <输出目录> search --input <queries.json>
```

知识库不可用时返回明确降级状态；非活动归因可继续。
