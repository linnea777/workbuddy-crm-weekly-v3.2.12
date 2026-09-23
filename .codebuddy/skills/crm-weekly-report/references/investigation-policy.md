# 调查政策 · 3.1

全场变化达到既定重要性门槛时，默认建立全场结构主题，同时调查区域贡献与五个卡级的销售增量、消费人数、人均消费和笔单价。全场均值不代表每个卡级同步变化；正文应指出主要增量卡级，并区分该卡级人数与人均消费的变化。卡级贡献和区域/品牌贡献为同一净增量的不同切面，不重复相加。

基础阈值覆盖所有约定对象，筛选不改变RED/YELLOW。program selection在prepare阶段完成；完整告警留存，低影响观察理由无需模型逐条重写。

默认起始参数：普通品牌变化额门槛max(5万元,全场基期CRM×0.3%)；区域分别扫描正向和负向变化并优先覆盖80%。每区3个、全场5个只是初始批次预算，不截断重要对象。现实现直接保留全部达金额门槛的品牌，避免预算成为硬上限。无重大品牌时允许区域分散变化。趋势使用原连续下降/历史基线规则与累计影响、重点品牌价值独立评估。

一个区域case可携带多个同区域品牌查询，不要求品牌单独告警。品牌趋势case与区域case可复用事实。模型可调整默认主题和追加查询；记录独立问题价值、比较基准和省略理由。重大问题不能只进观察。

普通品牌：CORE_METRICS+CARD_TIER。LV、Dior、Hermes：CORE_METRICS+AMOUNT_BAND。CORE_METRICS含CRM、去重消费人数、笔数、周人均消费（客单价）、笔单价。其余新老客、大额、活动窗口和会员审计按解释缺口请求。品牌scope显式携带region_id；全场品牌汇总须明确命名。仅三类指定高奢做默认金额分段，Tiffany/Burberry按普通品牌处理。

analysis_items合法值：CORE_METRICS、CONTRIBUTORS、MEMBER_COUNT、TRANSACTIONS、TICKET_SIZE、CARD_TIER、AMOUNT_BAND、WEEKLY_HISTORY、NEW_EXISTING、LARGE_SPEND、ACTIVITY_RESPONSE、REFUND、OPERATING_STATUS。focus为CONTRIBUTION、MEMBER_BEHAVIOR或BRAND_OPERATION；比较基准WOW/YOY/YTD/CONTINUOUS_PERIOD/EIGHT_WEEK_BASELINE。未实现某基准的路径必须明确不可用，不能用WOW假替代。

自然周金额分段支持WOW/YOY；WEEKLY_HISTORY用于有序趋势证据。高奢按会员在区域×品牌×周内净消费累加再分段；正净额段占比以正净额会员销售总额为分母，净额≤0群体单列调整项，全部段合计对账净CRM。跨期同金额段不代表同一批会员。

默认一轮充分调查，第二轮仅补充可能改变主因或动作的证据。每项返回COMPLETED/UNAVAILABLE及fact_ids与原因。每批最多50条、并发最多2批；缓存跨主题复用同语义查询，只重试失败批次，超时明确失败。
