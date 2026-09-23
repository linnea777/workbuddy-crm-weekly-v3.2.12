# 交付契约 · 实现3.2.2 / Schema 3.0

业务输出中，品牌经营点统一称为“门店”，不使用“柜台”；正文、建议对象、行动及跟进项保持一致。原始数据字段与品牌原名保留，以免影响匹配。

发布包3.2.12：计算实现继续为3.2.2；营销建议按本项目VIC分工与品牌沟通流程生成。有值指标规则保持不变。

全场与区域主题另须operating_assessment：`{statement, fact_ids}`。statement为正文中经营销售/积分记录占比判断的原句，fact_ids引用本主题同范围同基准的CRM_SALES、MALL_SALES、CAPTURE_RATIO。三项比较可用时不得只列数字而不判断；必要证据不可用时填unavailable_reason，并在正文说明相同缺口。

全场主题对没有独立区域正文的区域，另在operating_assessment.regions中按N/S/W键填写同结构判断，并在全场正文写出该区域的比较。已有独立区域正文的范围无需重复展开。

身份字段：schema_version、week_id、input_signature、implementation_version、kb_version原样复制运行上下文。旧实现的plan/insights不直接混用；重新prepare。

InvestigationPlan由recommended_plan.json起步。selected_cases每项含insight_case_id、issue_id、issue_type、primary_scope、comparison_basis、independent_issue_reason、anomaly_group_ids、selection_reason、queries、activity_queries。一个case保持同一锚定对象；其查询可向同区域多个非异常品牌下钻。omitted_anomaly_groups保留程序观察理由。不同独立主题可共享异常或事实，不能重复主题。

查询含query_id、insight_case_id、anomaly_group_id、focus、comparison_basis、scope、analysis_items；品牌scope为`{"dimension_type":"BRAND","dimension_key":"源品牌原名","region_id":"N"}`。CONTRIBUTION可指定next_dimension与top_k（1–20，查询分页预算，不是正文上限）。每个分析项单独返回状态。

InsightBundle根字段：
- 不生成summary_items；旧字段可以读取但不交付。report_notes为全报告共性口径短句列表。
- insights：必要主题全量，不设条数上限。
- monitor_items：已调查但不独立展开的case；含insight_case_id/anomaly_group_ids/reason/verification_metrics/signal_ids/fact_ids。重大问题只能由其他正文解释或明确数据质量提示披露。
- data_quality_items：`{reason,affected_judgments,anomaly_group_ids}`。数据不足可空异常ID；不可隐去受影响的重大判断。
- deduplication_review：超过八条时`{completed:true,retained_issue_ids:[全部主题ID],reason:"说明为何各主题独立必要"}`。
- run_status：data_quality、attribution、knowledge_base；任何阶段未完成均不能伪报完整。

每条insight新增title（业务主题）、narrative（模型直接写作的完整正文）、analysis_note（basis/limitations短文本）；可选activity_assessment见活动政策。以下字段供内部判断和校验，不直接拼入正文：

每条insight：issue_id、issue_type（REGION_CONTRIBUTION/BRAND_TREND/INDEPENDENT_RISK）、primary_scope、comparison_basis、independent_issue_reason、insight_case_id、supporting_case_ids（可空）、anomaly_group_ids（声明case的并集）、summary、impact_assessment（type/statement）、primary_driver（statement/evidence_strength/evidence_ids）、supporting_factors、confidence、confidence_reason、verification_metrics、recommendation、follow_up_items、signal_ids、fact_ids、activity_ids、claims、related_insight_ids。

claims至少一条：`{claim_id,claim_role,statement,primary_insight_id}`；claim_role=CONTRIBUTION/TREND/BEHAVIOR/ACTION；一个claim只完整展开一次，primary_insight_id为所属issue_id。共享基础事实允许，重复计入影响金额不允许。

多品牌分支用root_node_id/nodes/edges。node含node_id、scope、comparison_basis、fact_ids；edge含parent_node_id/child_node_id。合法层级TOTAL→REGION→BRAND→CARD，允许同层多个兄弟。每个非根节点有唯一父级、无环、全部连到根；品牌region_id继承父区域，卡级节点还带brand_id。节点只引用本洞察已声明、且时间范围匹配的事实。无独立case的下钻品牌使用节点，不伪造supporting_case_ids。

recommendation.applicable=false时给not_applicable_reason；true时给target/action/channel/timing/fact_ids，引用已定位主因与事实。LOW仍列具体follow_up_items；仅CAMPAIGN正式营销刺激必须false，核查、观察及条件性合作按action-delivery.md执行。verification_metrics只写验证指标，动作放follow_up_items。

正式数字仅来自已引用display_fields；叙事、摘要均不自行算比例。完整EvidenceLedger含审计记录，模型只读insight_synthesis_context的正式展示投影。

输出Excel恰好包含报告、洞察、分析说明。洞察仅主题/洞察两列，程序原样输出narrative；分析说明包含主题/置信度/判断依据/资料来源/必要说明。共性口径集中一次。不导出告警、证据明细或审计记录。内部引用仍用于数字和范围校验。

正文术语统一为crm sales、mall sales、Capture Ratio；分析后自然衔接有依据的营销建议，填写对应recommendation。保持主题/洞察两列，可在同一narrative中换段，不增设建议列。

recommendation.channel新增会员体验活动、品牌联合活动，用于上新预览、DIY、手工坊等合作建议。与积分活动等既有渠道执行相同的证据与置信度校验，不要求把体验活动归类为积分活动或线下核查。


新版写作优先使用writing_draft_template与finalize --draft。模型填写analysis_text及action_text，程序原样合成narrative并保持两列版式。正式InsightBundle增加action_text/action_kind及条件性合作或观察使用的action_condition；具体要求见[action-delivery.md](action-delivery.md)。已选择的recommendation.action必须与action_text一致，不能仅保存在内部。
