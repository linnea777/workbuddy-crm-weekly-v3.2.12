# WorkBuddy CRM周报 Agent 3.2.12

六技能计算、调查并导出三页周报。以用户认可的3.2.9洞察风格为参照，保留四个完整few-shot。模型分别写分析和行动，程序原文换段展示；新增行动交付检查、分主题写作入口、确定性组装与数字格式规范化。

安装见[安装说明.md](安装说明.md)，写作见[行动交付](.codebuddy/skills/crm-weekly-report/references/action-delivery.md)。Python 3.10+，依赖pandas/openpyxl。完整导入六技能并更新SystemPrompt.md。

发布版本3.2.12，计算实现仍为3.2.2。59项测试通过，另完成认可内容的七条真实资料回放与最终Excel检查。详见[验收记录.txt](验收记录.txt)。尚未在WorkBuddy宿主由模型从头生成验收，不承诺端到端提速比例。
