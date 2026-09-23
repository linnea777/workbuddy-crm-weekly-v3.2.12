from typing import Any, Mapping
IMPLEMENTATION_VERSION = '3.2.2'


def enrich_anomaly_bundle(raw: Mapping[str, Any], metric_bundle: Mapping[str, Any]) -> dict[str, Any]:
    output=dict(raw)
    if any(str(x.get('code',x.get('status_code',''))).upper()=='EXCLUDED_ENTITY_PRESENT' for x in output.get('data_quality_status',[])):
        raise ValueError('排除项残留进入异常检测')
    output.update(schema_version='3.0',week_id=metric_bundle.get('week_id'),input_signature=metric_bundle.get('input_signature'),implementation_version=IMPLEMENTATION_VERSION,reporting_policy=metric_bundle.get('reporting_policy',{}))
    output['anomaly_groups']=[dict(g, severity=g.get('group_severity'), must_investigate=False, investigation_eligible=False, selection_reason_code='PENDING_IMPACT_SELECTION', display_fields={'display_value':g.get('display_value'),'display_change':g.get('display_change')}, data_quality=[]) for g in raw.get('anomaly_groups',[])]
    return output
