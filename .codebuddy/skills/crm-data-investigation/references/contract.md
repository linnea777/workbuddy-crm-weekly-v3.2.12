# 调查返回契约

查询注册字段由engine.py与周报validate_plan.py维护。品牌scope必须继承region_id；全场汇总显式省略region_id。普通品牌基础+CARD_TIER，高奢基础+AMOUNT_BAND，其他切面按需。

每个analysis_item返回COMPLETED或UNAVAILABLE、fact_ids和原因，缺少请求结果不得用基础事实假报成功。事实包含input_signature、implementation_version、scope_details、periods、comparison_basis、display_fields及适用限制；fact_id不含case编号。成员审计仅供内部计算核验，不写交付报告，模型不读取。

AMOUNT_BAND按会员区域×品牌×自然周净消费分段，净额≤0单列调整项；所有段合计对账净CRM，份额分母为正净额会员总销售。CARD_TIER有正式销售事实及销售占比/变化/方向贡献。人均消费为客单价，销售/笔数为笔单价。

相同输入与实现、范围、周期、切面和配置复用缓存；失败或部分完成结果不缓存为成功。WEEKLY_HISTORY保留缺口；未支持的比较路径明确不可用。

ACTIVITY_RESPONSE的同星期基准、活动期占比和非活动期比较详见 [活动调查](activity-comparison.md)。
# 区域经营口径（3.2.2）

TOTAL/REGION的OPERATING_STATUS默认返回区域CRM/Mall总额及其比例，标记OBSERVED_AGGREGATE。需要限定两期共同完整门店时，在filters中明确operating_scope=COMMON_STORES_TWO_PERIODS。品牌调查仍优先同店比较；同店子集结果不能冒充区域总额。全场销售贡献同时请求REGION和CARD两个切面，分开对账。
