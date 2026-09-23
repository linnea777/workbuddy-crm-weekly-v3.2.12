# 置信度

置信度评估本条具体结论，不按事实数量或告警颜色评级。HIGH为充分且相互一致的证据支持；MEDIUM为较强方向性解释但存在实质限制；LOW为原因未分清或关键依据缺失。结构变化可高置信，活动因果仍可较低，应在analysis_note区分。

证据充分的业务归因可以明确写“活动带动了增长”，不强制所有活动都用“可能”。标准见activity-attribution.md。历史对照差额不自动等于净增量，直接因果措辞仍要求HIGH/DIRECT和有效引用。未来或竞品活动只能作候选解释。

LOW需follow_up_items；CAMPAIGN营销刺激不得适用，核查和条件性合作仍可提出，前提须写入行动段。分类型证据要求见[action-delivery.md](action-delivery.md)。所有主题都交付action_text，程序将其与模型分析原样换段展示。confidence_reason保留内部判断说明，analysis_note仅写业务需要的简短依据和限制，不列证据数量。
