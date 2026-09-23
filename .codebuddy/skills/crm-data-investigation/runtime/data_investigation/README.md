# 数据调查工具

`data_investigation` 对预聚合的 `MetricBundle` 执行确定性调查，不读取原始 Excel、会员卡号或交易明细。

支持三类 `focus`：

- `CONTRIBUTION`：贡献项、抵消项、Top-K 与父子对账。
- `MEMBER_BEHAVIOR`：消费金额、消费人数、交易笔数、笔单价等聚合变化。
- `BRAND_OPERATION`：CRM、Mall Sales 与 Capture Ratio 联合拆解。

## Python 调用

```python
from data_investigation import investigate_anomalies

result = investigate_anomalies(metric_bundle, {
    "queries": [{
        "anomaly_group_id": "AG-001",
        "focus": "CONTRIBUTION",
        "scope": "TOTAL",
        "metric_id": "CRM_SALES",
        "next_dimension": "REGION",
        "top_k": 3
    }]
})
```

## 命令行调用

```bash
python -m data_investigation.cli \
  --metrics outputs/crm-weekly-2026-07-06_07-12/metrics.json \
  --request request.json \
  --output investigation.json
```

请求可以是单条查询、查询数组，或 `{"queries": [...]}`。非 Total 范围既可写成 `"REGION:N"`，也可写成：

```json
{"dimension_type": "REGION", "dimension_key": "N"}
```

每个正式聚合事实均返回稳定的 `fact_id`。ID 包含报告周期、指标、范围、数值与来源摘要；数据变化后 ID 会同步变化，避免引用旧事实。

## MetricBundle 路径

现有 `metrics.json` 可以执行：

- CRM：Total → Region/Card/Brand/Store，Region → Card/Brand/Store。
- Mall：Total → Region/Brand/Store，Region → Brand/Store。
- 会员行为：Total、Region、Card、Region×Card。
- 经营拆解：Total、Region、Brand、Store（以已有聚合为准）。

`tools/analyze_crm.py` 已增加街区内品牌/店铺、卡级与街区×卡级的安全聚合。更新前生成的旧 `metrics.json` 没有这些字段时，相应查询会明确返回 `PARTIAL` 或 `DATA_UNAVAILABLE`。

不支持的父子路径返回 `DATA_UNAVAILABLE`，不会读取原始明细临时计算或猜测结果。
