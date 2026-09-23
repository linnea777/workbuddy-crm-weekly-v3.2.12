from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0.0"
FOCUSES = {"CONTRIBUTION", "MEMBER_BEHAVIOR", "BRAND_OPERATION"}
COMPARISON_BASES = {"WOW", "YOY", "YTD", "CONTINUOUS_PERIOD", "EIGHT_WEEK_BASELINE"}
ANALYSIS_ITEMS = {
    "CORE_METRICS", "CONTRIBUTORS", "MEMBER_COUNT", "TRANSACTIONS", "TICKET_SIZE",
    "CARD_TIER", "NEW_EXISTING", "LARGE_SPEND", "ACTIVITY_RESPONSE", "REFUND", "OPERATING_STATUS", "AMOUNT_BAND", "WEEKLY_HISTORY",
}
DIMENSION_ALIASES = {
    "TOTAL": "TOTAL",
    "REGION": "REGION",
    "DISTRICT": "REGION",
    "CARD": "CARD",
    "CARD_LEVEL": "CARD",
    "CARD_TIER": "CARD",
    "MEMBER_TIER": "CARD",
    "BRAND": "BRAND",
    "STORE": "STORE",
    "REGION_CARD": "REGION_CARD",
    "DISTRICT_CARD": "REGION_CARD",
    "REGION_CARD_TIER": "REGION_CARD",
    "REGION_MEMBER_TIER": "REGION_CARD",
}
REGION_ALIASES = {
    "北区": "N",
    "南区": "S",
    "西区": "W",
    "N": "N",
    "S": "S",
    "W": "W",
    "TOTAL": "Total",
    "Total": "Total",
}
ADDITIVE_METRICS = {"CRM_SALES", "MALL_SALES"}
MAX_BATCH_SIZE = 50
MAX_TOP_K = 20


class InvestigationError(ValueError):
    """A request error that is safe to return to the caller."""


@dataclass(frozen=True)
class Scope:
    dimension_type: str
    dimension_key: str
    region_id: str | None = None

    @property
    def label(self) -> str:
        if self.dimension_type == "TOTAL":
            return "TOTAL"
        return f"{self.dimension_type}:{self.dimension_key}" + (f"@{self.region_id}" if self.region_id else "")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _rate(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fact_id(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16].upper()
    return f"FACT-{digest}"


def _audit_id(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16].upper()
    return f"AUD-{digest}"


def _normalize_dimension(value: Any) -> str:
    key = str(value or "").strip().upper().replace("-", "_")
    try:
        return DIMENSION_ALIASES[key]
    except KeyError as exc:
        raise InvestigationError(f"Unsupported dimension_type: {value!r}") from exc


def _normalize_scope(raw: Any, dimension_key: Any = None) -> Scope:
    if raw is None or raw == "":
        return Scope("TOTAL", "Total")
    if isinstance(raw, str):
        text = raw.strip()
        if text.upper() == "TOTAL":
            return Scope("TOTAL", "Total")
        if ":" in text:
            dimension_type, scope_key = text.split(":", 1)
        else:
            dimension_type, scope_key = text, dimension_key
    elif isinstance(raw, Mapping):
        dimension_type = raw.get("dimension_type", raw.get("type"))
        scope_key = raw.get("dimension_key", raw.get("key"))
    else:
        raise InvestigationError("scope must be a string or object")

    dimension = _normalize_dimension(dimension_type)
    if isinstance(scope_key, Mapping):
        if dimension == "REGION":
            scope_key = scope_key.get("region")
        elif dimension == "CARD":
            scope_key = scope_key.get("card_tier", scope_key.get("member_tier"))
        elif dimension == "REGION_CARD":
            region = scope_key.get("region")
            card = scope_key.get("card_tier", scope_key.get("member_tier"))
            scope_key = f"{region}|{card}" if region and card else scope_key.get("region_card_tier")
        elif dimension in {"BRAND", "STORE"}:
            scope_key = scope_key.get("brand", scope_key.get("store"))
    key = str(scope_key or "").strip()
    if not key:
        raise InvestigationError("scope.dimension_key is required")
    if dimension == "REGION":
        key = REGION_ALIASES.get(key, REGION_ALIASES.get(key.upper(), key))
    if dimension == "REGION_CARD":
        parts = [part.strip() for part in key.replace("|", "/").split("/", 1)]
        if len(parts) != 2 or not all(parts):
            raise InvestigationError("REGION_CARD key must be 'region|card'")
        parts[0] = REGION_ALIASES.get(parts[0], REGION_ALIASES.get(parts[0].upper(), parts[0]))
        key = "|".join(parts)
    region = raw.get("region_id") if isinstance(raw, Mapping) else None
    if region is not None and region not in {"N","S","W"}: raise InvestigationError("region_id must be N/S/W")
    return Scope(dimension, key, region)


def _periods(bundle: Mapping[str, Any]) -> dict[str, Any]:
    report = bundle.get("report", {})
    return {"current": report.get("current"), "previous": report.get("previous")}


def _metric_record(current: Any, previous: Any) -> dict[str, float | None]:
    cur = _number(current)
    prev = _number(previous)
    change = None if cur is None or prev is None else cur - prev
    return {
        "current": _round(cur),
        "previous": _round(prev),
        "change": _round(change),
        "change_rate": _round(_rate(cur, prev)),
    }


class InvestigationEngine:
    """Query a pre-aggregated MetricBundle without reading transaction rows.

    The engine intentionally accepts only aggregate dictionaries. It never opens
    source workbooks and never returns member identifiers or transaction rows.
    """

    def __init__(self, metric_bundle: Mapping[str, Any]):
        if not isinstance(metric_bundle, Mapping):
            raise TypeError("metric_bundle must be a mapping")
        self.bundle = metric_bundle
        self.periods = _periods(metric_bundle)
        self.query_cache = {}
        self.cache_hits = 0

    def investigate(self, request: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        queries = self._queries(request)
        results = [self._run_safely(query, index) for index, query in enumerate(queries, 1)]
        if all(item["status"] == "OK" for item in results):
            status = "OK"
        elif all(item["status"] in {"INVALID_QUERY", "DATA_UNAVAILABLE"} for item in results):
            status = "FAILED"
        else:
            status = "PARTIAL"
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "query_count": len(results),
            "results": results,
        }

    def _amount_bands(self,scope,current_rows,previous_rows,basis):
        aliases={'HERMES':{'HERMES','HERMES 爱马仕','HERMÈS','爱马仕'},'LV':{'LV','LOUIS VUITTON','LOUIS VUITTON 路易威登','路易威登'},'DIOR':{'DIOR','DIOR 迪奥','迪奥'}}
        brand=next((k for k,v in aliases.items() if scope.dimension_key.upper() in v),None)
        if scope.dimension_type not in {'BRAND','STORE'} or brand is None:
            return [],'AMOUNT_BAND: NOT_APPLICABLE for ordinary brands'
        if basis not in {'WOW','YOY'}:
            return [],'AMOUNT_BAND: natural-week WOW/YOY required'
        cuts=self.bundle.get('band_config',{}).get(brand)
        if not cuts:return [],'AMOUNT_BAND: band configuration unavailable'
        def bands(rows):
            members=self._member_rollup(rows)
            buckets=[{'sales':0.0,'members':0,'transactions':0} for _ in range(len(cuts)+2)]
            for member in members.values():
                value=member['crm_sales']
                idx=0 if value<=0 else 1+next((i for i,c in enumerate(cuts) if value<=c),len(cuts))
                b=buckets[idx];b['sales']+=value;b['members']+=1;b['transactions']+=member['transactions']
            return buckets
        now,previous=bands(current_rows),bands(previous_rows)
        positive=sum(b['sales'] for b in now[1:]);previous_positive=sum(b['sales'] for b in previous[1:]);count=sum(b['members'] for b in now[1:])
        previous_count=sum(b['members'] for b in previous[1:])
        facts=[]
        for i,(a,b) in enumerate(zip(now,previous)):
            lower=None if i==0 else 0 if i==1 else cuts[i-2]
            upper=0 if i==0 else cuts[i-1] if i<=len(cuts) else None
            label='净消费≤0调整项' if i==0 else f'({lower},{upper if upper is not None else "∞"}]元'
            fact=self._fact(metric_id='REFUND_ADJUSTMENT' if i==0 else 'AMOUNT_BAND_CRM_SALES',scope=scope,current=a['sales'],previous=b['sales'],source='member_week_net_amount_bands',comparison_basis=basis,extra={'band_index':i,'lower':lower,'upper':upper,'lower_inclusive':False,'upper_inclusive':True,'band_config':cuts,'definition':'MEMBER_WEEK_NET_POSITIVE_WITH_NONPOSITIVE_ADJUSTMENT'})
            share=a['sales']/positive if positive and i else None
            previous_share=b['sales']/previous_positive if previous_positive and i else None
            fact.update(period=self.bundle.get('report',{}).get('current'),region=scope.region_id,brand=scope.dimension_key,band_lower=lower,band_upper=upper,lower_inclusive=False,upper_inclusive=True,member_count=a['members'],transaction_count=a['transactions'],crm_sales=a['sales'],sales_share_brand=share,previous_member_count=b['members'],previous_transaction_count=b['transactions'],previous_crm_sales=b['sales'],coverage_status='OBSERVED',denominator='SUM_OF_POSITIVE_MEMBER_WEEK_NET_SALES')
            fact['display_fields'].update(amount_band=label,member_count=str(a['members']),previous_member_count=str(b['members']),member_change=f"{a['members']-b['members']:+d}",transaction_count=str(a['transactions']),previous_transaction_count=str(b['transactions']),transaction_change=f"{a['transactions']-b['transactions']:+d}",sales_share_brand=None if share is None else f'{share*100:.2f}%',previous_sales_share_brand=None if previous_share is None else f'{previous_share*100:.2f}%',share_change_pp=None if share is None or previous_share is None else f'{(share-previous_share)*100:+.2f}个百分点',member_share=None if not count or not i else f"{a['members']/count*100:.2f}%",previous_member_share=None if not previous_count or not i else f"{b['members']/previous_count*100:.2f}%",positive_member_net_sales=self._display('CRM_SALES',positive),net_crm_sales=self._display('CRM_SALES',sum(x['sales'] for x in now)))
            fact['applicability_limits']=['按会员该品牌该区域自然周净消费累计分段；正净额段与非正净额调整项合计等于净CRM。跨期同段不等于同一批会员，不代表流失。']
            facts.append(fact)
        return facts,None

    def _weekly_history_fact(self,query,scope):
        dimension={'CARD':'MEMBER_TIER','REGION_CARD':'REGION_MEMBER_TIER'}.get(scope.dimension_type,scope.dimension_type)
        key='TOTAL' if dimension=='TOTAL' else scope.dimension_key
        rec=next((r for r in self.bundle.get('anomaly_records',[]) if r['dimension_type']==dimension and r['dimension_key']==key and r['metric_id']=='CRM_SALES' and (r.get('region_id')==scope.region_id or (not r.get('region_id') and len(self.bundle.get('brand_regions',{}).get(key,[]))==1))),None)
        if not rec:return self._unavailable(scope,'Weekly history unavailable')
        sequence=rec.get('history',[])+[{'week_id':rec['week_id'],'value':rec.get('current_value')}]
        fact=self._fact(metric_id='WEEKLY_HISTORY',scope=scope,current=rec.get('current_value'),previous=rec.get('previous_value'),source='anomaly_records.weekly_history',comparison_basis='CONTINUOUS_PERIOD',extra={'sequence':sequence})
        fact['display_fields']={'weekly_sales':[{'week':r['week_id'],'crm_sales':self._display('CRM_SALES',r['value'])} for r in sequence]}
        fact['applicability_limits']=['缺周保留N/A；只能在连续有效后缀上判断持续下降。']
        return {'status':'OK','facts':[fact],'fact_ids':[fact['fact_id']],'evidence_status':'AVAILABLE'}

    @staticmethod
    def _item_status(result,items):
        mapping={'CORE_METRICS':{'CRM_SALES','ACTIVE_MEMBERS','TRANSACTIONS','AVG_TICKET','SALES_PER_ACTIVE_MEMBER'},'CONTRIBUTORS':{'CRM_SALES','MALL_SALES'},'MEMBER_COUNT':{'ACTIVE_MEMBERS'},'TRANSACTIONS':{'TRANSACTIONS'},'TICKET_SIZE':{'AVG_TICKET','SALES_PER_ACTIVE_MEMBER'},'CARD_TIER':{'CARD_TIER_CRM_SALES'},'AMOUNT_BAND':{'AMOUNT_BAND_CRM_SALES'},'NEW_EXISTING':{'NEW_EXISTING_ACTIVE_MEMBERS'},'LARGE_SPEND':{'LARGE_SPEND_CONTRIBUTION'},'ACTIVITY_RESPONSE':{'ACTIVITY_RESPONSE'},'REFUND':{'REFUND_ADJUSTMENT'},'OPERATING_STATUS':{'CRM_SALES','MALL_SALES','CAPTURE_RATIO'},'WEEKLY_HISTORY':{'WEEKLY_HISTORY'}}
        statuses=[]
        for item in items:
            matching=[f for f in result.get('facts',[]) if f.get('metric_id') in mapping.get(item,set())]
            complete=bool(matching) and all(f.get('values',{}).get('previous') is not None for f in matching)
            if item in {'CORE_METRICS','OPERATING_STATUS'}:complete &= mapping[item].issubset({f['metric_id'] for f in matching})
            statuses.append({'analysis_item':item,'status':'COMPLETED' if complete else 'UNAVAILABLE','fact_ids':[f['fact_id'] for f in matching],'reason':None if complete else result.get('unavailable_reason') or 'Requested analysis has no complete comparable facts'})
        result['analysis_item_statuses']=statuses
        if any(x['status']!='COMPLETED' for x in statuses) and result.get('status')=='OK':
            result.update(status='PARTIAL',evidence_status='PARTIAL',unavailable_reason='部分请求分析项不可用；参见analysis_item_statuses')
        return result

    def _queries(self, request: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        if isinstance(request, Mapping):
            raw = request.get("queries", [request])
        elif isinstance(request, Sequence) and not isinstance(request, (str, bytes)):
            raw = request
        else:
            raise InvestigationError("request must be a query, list of queries, or {'queries': [...]} object")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise InvestigationError("queries must be a list")
        if not raw:
            raise InvestigationError("queries must not be empty")
        if len(raw) > MAX_BATCH_SIZE:
            raise InvestigationError(f"queries exceeds maximum batch size {MAX_BATCH_SIZE}")
        if not all(isinstance(item, Mapping) for item in raw):
            raise InvestigationError("each query must be an object")
        return list(raw)

    def _run_safely(self, query: Mapping[str, Any], index: int) -> dict[str, Any]:
        semantic = {k:v for k,v in query.items() if k not in {"query_id","insight_case_id","anomaly_group_id"}}
        cache_key = _canonical_json(semantic)
        if cache_key in self.query_cache:
            self.cache_hits += 1
            result = deepcopy(self.query_cache[cache_key])
        else:
            result = self._run_uncached(query,index)
            if result.get("status") == "OK": self.query_cache[cache_key] = deepcopy(result)
        if "analysis_item_statuses" not in result:result=self._item_status(result,query.get("analysis_items",[]))
        result.update(query_id=str(query.get("query_id") or f"Q-{index:03d}"),anomaly_group_id=query.get("anomaly_group_id"))
        return result

    def _run_uncached(self, query: Mapping[str, Any], index: int) -> dict[str, Any]:
        query_id = str(query.get("query_id") or f"Q-{index:03d}")
        anomaly_group_id = str(query.get("anomaly_group_id") or "")
        focus = str(query.get("focus") or "").strip().upper()
        comparison_basis = str(query.get("comparison_basis") or "WOW").strip().upper()
        base = {
            "query_id": query_id,
            "anomaly_group_id": anomaly_group_id,
            "focus": focus,
            "comparison_basis": comparison_basis,
        }
        try:
            if not anomaly_group_id:
                raise InvestigationError("anomaly_group_id is required")
            if focus not in FOCUSES:
                raise InvestigationError(f"Unsupported focus: {focus!r}")
            if comparison_basis not in COMPARISON_BASES:
                raise InvestigationError(f"Unsupported comparison_basis: {comparison_basis!r}")
            analysis_items = query.get("analysis_items", [])
            if not isinstance(analysis_items, Sequence) or isinstance(analysis_items, (str, bytes)):
                raise InvestigationError("analysis_items must be an array")
            invalid_items = sorted({str(item).upper() for item in analysis_items} - ANALYSIS_ITEMS)
            if invalid_items:
                raise InvestigationError(f"Unsupported analysis_items: {invalid_items}")
            scope = _normalize_scope(query.get("scope"), query.get("dimension_key"))
            if scope.region_id and scope.dimension_type in {"BRAND","STORE"}:
                if scope.region_id not in self.bundle.get("brand_regions",{}).get(scope.dimension_key,[]):
                    raise InvestigationError("Brand does not belong to the declared region")
            if comparison_basis=="CONTINUOUS_PERIOD" and set(analysis_items)!={"WEEKLY_HISTORY"}:
                return base | self._item_status(self._unavailable(scope,"CONTINUOUS_PERIOD requires WEEKLY_HISTORY; a WOW pair is not a trend comparison"),analysis_items)
            if focus == "CONTRIBUTION": result = self._contribution(query, scope)
            elif focus == "MEMBER_BEHAVIOR" and set(analysis_items)=={"ACTIVITY_RESPONSE"}:
                fact, audits, missing = self._activity_response_fact(query, scope, self.bundle.get('crm', {}))
                result = {'status': 'DATA_UNAVAILABLE' if fact is None else 'PARTIAL' if missing else 'OK',
                          'evidence_status': 'UNAVAILABLE' if fact is None else 'PARTIAL' if missing else 'AVAILABLE',
                          'scope': scope.label, 'facts': [fact] if fact else [],
                          'fact_ids': [fact['fact_id']] if fact else [], 'audit_records': audits,
                          'unavailable_reason': missing}
            elif focus == "MEMBER_BEHAVIOR" and set(analysis_items)=={"WEEKLY_HISTORY"}: result = self._weekly_history_fact(query,scope)
            elif focus == "MEMBER_BEHAVIOR": result = self._member_behavior(query, scope)
            else: result = self._brand_operation(query, scope)
            result = self._item_status(result, analysis_items)
            return base | result
        except InvestigationError as exc:
            return base | {
                "status": "INVALID_QUERY",
                "evidence_status": "FAILED",
                "unavailable_reason": str(exc),
                "error": {"code": "INVALID_QUERY", "message": str(exc)},
                "fact_ids": [],
                "facts": [],
            }
        except (KeyError, TypeError) as exc:
            return base | {
                "status": "DATA_UNAVAILABLE",
                "evidence_status": "UNAVAILABLE",
                "unavailable_reason": str(exc),
                "error": {"code": "DATA_UNAVAILABLE", "message": str(exc)},
                "fact_ids": [],
                "facts": [],
            }

    def _fact(
        self,
        *,
        metric_id: str,
        scope: Scope,
        current: Any,
        previous: Any,
        source: str,
        comparison_basis: str = "WOW",
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = _metric_record(current, previous)
        stable = {
            "input_signature": self.bundle.get("input_signature"),
            "implementation_version": self.bundle.get("implementation_version"),
            "metric_id": metric_id,
            "scope": scope.label,
            "scope_details": {"dimension_type": scope.dimension_type, "dimension_key": scope.dimension_key, "region_id": scope.region_id},
            "periods": {"current":self.bundle.get("report",{}).get("ytd" if comparison_basis=="YTD" else "current"),"previous":self.bundle.get("report",{}).get({"WOW":"previous","YOY":"yoy","YTD":"prior_ytd"}.get(comparison_basis,"previous"))},
            "values": values,
            "source": source,
            "comparison_basis": comparison_basis,
        }
        if extra:
            stable["extra"] = dict(extra)
        amount_metrics = {"CRM_SALES", "MALL_SALES", "AVG_TICKET", "SALES_PER_ACTIVE_MEMBER", "LARGE_SPEND_CONTRIBUTION", "ACTIVITY_RESPONSE", "CARD_TIER_CRM_SALES", "AMOUNT_BAND_CRM_SALES", "REFUND_ADJUSTMENT"}
        member_metrics = {"ACTIVE_MEMBERS", "NEW_REGISTRATIONS", "BRAND_CARD_TIER_ACTIVE_MEMBERS", "NEW_EXISTING_ACTIVE_MEMBERS"}
        unit = "万元" if metric_id in amount_metrics else ("百分点" if metric_id == "CAPTURE_RATIO" else "人" if metric_id in member_metrics else "笔")
        display_value = self._display(metric_id, values["current"])
        display_change = self._display(metric_id, values["change"], change=True)
        return {
            "fact_id": _fact_id(stable),
            **stable,
            "metric": metric_id,
            "current": values["current"],
            "baseline": values["previous"],
            "change": values["change"],
            "display_value": display_value,
            "display_change": display_change,
            "display_unit": unit,
            "display_fields": {
                "current": display_value,
                "baseline": self._display(metric_id, values["previous"]),
                "change": display_change,
                "change_rate": None if values["current"] is None or values["previous"] in (None,0) else f"{((Decimal(str(values['current']))-Decimal(str(values['previous'])))/Decimal(str(values['previous']))*100).quantize(Decimal('0.1'),rounding=ROUND_HALF_UP):+.1f}%",
            },
            "applicability_limits": [],
        }

    @staticmethod
    def _display(metric_id: str, value: float | None, *, change: bool = False) -> str | None:
        if value is None:
            return None
        sign = "+" if change and value > 0 else ""
        if metric_id in {"CRM_SALES", "MALL_SALES", "AVG_TICKET", "SALES_PER_ACTIVE_MEMBER", "LARGE_SPEND_CONTRIBUTION", "ACTIVITY_RESPONSE", "CARD_TIER_CRM_SALES", "AMOUNT_BAND_CRM_SALES", "REFUND_ADJUSTMENT"}:
            return f"{sign}{value / 10000:.2f}万"
        if metric_id == "CAPTURE_RATIO":
            return f"{sign}{value * 100:.2f}%" if not change else f"{sign}{value * 100:.2f}个百分点"
        return f"{sign}{value:g}"

    def _contribution(self, query: Mapping[str, Any], scope: Scope) -> dict[str, Any]:
        filters = query.get("filters") if isinstance(query.get("filters"), Mapping) else {}
        metric_id = str(query.get("metric_id") or filters.get("metric_id") or "CRM_SALES").strip().upper()
        comparison_basis = str(query.get("comparison_basis") or "WOW").upper()
        if metric_id not in ADDITIVE_METRICS:
            raise InvestigationError("CONTRIBUTION supports only additive CRM_SALES or MALL_SALES")
        next_dimension = _normalize_dimension(query.get("next_dimension"))
        if next_dimension == "TOTAL":
            raise InvestigationError("next_dimension cannot be TOTAL")
        try:
            top_k = int(query.get("top_k", 5))
        except (TypeError, ValueError) as exc:
            raise InvestigationError("top_k must be an integer") from exc
        if not 1 <= top_k <= MAX_TOP_K:
            raise InvestigationError(f"top_k must be between 1 and {MAX_TOP_K}")

        parent = self._lookup(metric_id, scope, comparison_basis)
        children = self._children(metric_id, scope, next_dimension, comparison_basis)
        if not children:
            return self._unavailable(
                scope,
                f"No aggregate path for {metric_id} {scope.label} -> {next_dimension}",
            )
        parent_fact = self._fact(
            metric_id=metric_id,
            scope=scope,
            current=parent.get("current"),
            previous=parent.get("previous"),
            source=parent["source"],
            comparison_basis=comparison_basis,
            extra={"contribution_context": {"role": "parent", "scope": scope.label,
                                           "next_dimension": next_dimension, "top_k": top_k}},
        )
        parent_change = parent_fact["values"]["change"]
        if parent_change is None:
            return self._unavailable(scope, "Parent current/previous values are incomplete")

        child_facts = []
        for child_scope, record in children:
            fact = self._fact(
                metric_id=metric_id,
                scope=child_scope,
                current=record.get("current"),
                previous=record.get("previous"),
                source=record["source"],
                comparison_basis=comparison_basis,
                extra={"contribution_context": {"role": "child", "parent_scope": scope.label,
                                               "next_dimension": next_dimension}},
            )
            change = fact["values"]["change"]
            if change is None:
                continue
            fact["contribution_share"] = _round(change / parent_change) if parent_change else None
            child_facts.append(fact)

        full_child_change = sum(item["values"]["change"] for item in child_facts)
        tolerance = max(0.01, abs(parent_change) * 1e-9)
        residual = parent_change - full_child_change
        reconciled = abs(residual) <= tolerance

        if parent_change > 0:
            aligned = [item for item in child_facts if item["values"]["change"] > 0]
            offsets = [item for item in child_facts if item["values"]["change"] < 0]
        elif parent_change < 0:
            aligned = [item for item in child_facts if item["values"]["change"] < 0]
            offsets = [item for item in child_facts if item["values"]["change"] > 0]
        else:
            aligned = [item for item in child_facts if item["values"]["change"] > 0]
            offsets = [item for item in child_facts if item["values"]["change"] < 0]
        total_base=self.bundle.get("crm",{}).get("district",{}).get("Total",{}).get("previous")
        for fact in child_facts:
            delta=fact["values"]["change"]
            if delta is None: continue
            denom=sum(abs(f["values"]["change"]) for f in child_facts if f["values"]["change"] is not None and f["values"]["change"]*delta>0)
            fact["display_fields"].update(impact_amount=self._display(metric_id,delta,change=True),directional_contribution_share=None if not denom else f"{abs(delta)/denom*100:.2f}%",directional_denominator=self._display(metric_id,denom),impact_pp_total=None if not total_base else f"{delta/total_base*100:+.2f}个百分点",contribution_share=None if not parent_change else f"{delta/parent_change*100:+.2f}%")
        aligned.sort(key=lambda item: (-abs(item["values"]["change"]), item["scope"]))
        offsets.sort(key=lambda item: (-abs(item["values"]["change"]), item["scope"]))
        contributors = aligned[:top_k]
        offset_items = offsets[:top_k]
        returned_ids = {item["fact_id"] for item in contributors + offset_items}
        returned_change = sum(item["values"]["change"] for item in contributors + offset_items)
        other_change = parent_change - returned_change

        parent_fact["display_fields"].update(explained_change=self._display(metric_id,returned_change,change=True),remaining_change=self._display(metric_id,other_change,change=True),reconciliation_residual=self._display(metric_id,residual,change=True))
        facts = [parent_fact] + contributors + offset_items
        return {
            "status": "OK" if reconciled else "PARTIAL",
            "evidence_status": "AVAILABLE" if reconciled else "PARTIAL",
            "scope": scope.label,
            "metric_id": metric_id,
            "next_dimension": next_dimension,
            "contributors": contributors,
            "offsets": offset_items,
            "other": {
                "change": _round(other_change),
                "includes_unreturned_children": any(item["fact_id"] not in returned_ids for item in child_facts),
                "includes_reconciliation_residual": not reconciled,
            },
            "reconciled": reconciled,
            "reconciliation_status": "PASS" if reconciled else "PARTIAL",
            "reconciliation": {
                "reconciled": reconciled,
                "parent_change": _round(parent_change),
                "all_children_change": _round(full_child_change),
                "residual": _round(residual),
                "tolerance": _round(tolerance),
            },
            "fact_ids": [item["fact_id"] for item in facts],
            "facts": facts,
            "data_quality_status": self._quality(scope),
        }

    def _member_behavior(self, query: Mapping[str, Any], scope: Scope) -> dict[str, Any]:
        crm = self._mapping(self.bundle, "crm")
        comparison_basis = str(query.get("comparison_basis") or "WOW").upper()
        filters = query.get("filters") if isinstance(query.get("filters"), Mapping) else {}
        target_metric = str(filters.get("target_metric_id") or "").upper()
        analysis_items = {str(item).upper() for item in query.get("analysis_items", [])}
        member_records = crm.get("member_week_facts")
        if isinstance(member_records, Sequence) and not isinstance(member_records, (str, bytes)):
            return self._member_behavior_from_records(query, scope, crm, analysis_items)
        facts: list[dict[str, Any]] = []
        missing: list[str] = []

        sales = self._lookup("CRM_SALES", scope, comparison_basis)
        facts.append(
            self._fact(
                metric_id="CRM_SALES",
                scope=scope,
                current=sales.get("current"),
                previous=sales.get("previous"),
                source=sales["source"],
                comparison_basis=comparison_basis,
            )
        )

        if scope.dimension_type in {"TOTAL", "REGION"}:
            key = "Total" if scope.dimension_type == "TOTAL" else scope.dimension_key
            profile = self._mapping(crm, "period_profile")
            current_profile = self._mapping(self._mapping(profile, "current"), key)
            previous_profile = self._mapping(self._mapping(profile, "previous"), key)
            for metric_id, field in (("ACTIVE_MEMBERS", "active_members"), ("TRANSACTIONS", "transactions")):
                facts.append(
                    self._fact(
                        metric_id=metric_id,
                        scope=scope,
                        current=current_profile.get(field),
                        previous=previous_profile.get(field),
                        source=f"crm.period_profile.*.{key}.{field}",
                        comparison_basis=comparison_basis,
                    )
                )
            cur_sales = _number(sales.get("current"))
            prev_sales = _number(sales.get("previous"))
            cur_tx = _number(current_profile.get("transactions"))
            prev_tx = _number(previous_profile.get("transactions"))
            facts.append(
                self._fact(
                    metric_id="AVG_TICKET",
                    scope=scope,
                    current=cur_sales / cur_tx if cur_sales is not None and cur_tx else None,
                    previous=prev_sales / prev_tx if prev_sales is not None and prev_tx else None,
                    source="derived:CRM_SALES/TRANSACTIONS",
                    comparison_basis=comparison_basis,
                )
            )
        elif scope.dimension_type == "CARD":
            active = self._mapping(crm, "active").get(scope.dimension_key)
            if not isinstance(active, Mapping):
                raise KeyError(f"Missing active member aggregate for {scope.label}")
            facts.append(
                self._fact(
                    metric_id="ACTIVE_MEMBERS",
                    scope=scope,
                    current=active.get("current"),
                    previous=active.get("previous"),
                    source=f"crm.active.{scope.dimension_key}",
                    comparison_basis=comparison_basis,
                )
            )
            cur_members = _number(active.get("current"))
            prev_members = _number(active.get("previous"))
            cur_sales = _number(sales.get("current"))
            prev_sales = _number(sales.get("previous"))
            facts.append(
                self._fact(
                    metric_id="SALES_PER_ACTIVE_MEMBER",
                    scope=scope,
                    current=cur_sales / cur_members if cur_sales is not None and cur_members else None,
                    previous=prev_sales / prev_members if prev_sales is not None and prev_members else None,
                    source="derived:CRM_SALES/ACTIVE_MEMBERS",
                    comparison_basis=comparison_basis,
                )
            )
            current_tx = _number(sales.get("transactions"))
            previous_tx = _number(sales.get("previous_transactions"))
            if current_tx is None or previous_tx is None:
                missing.extend(["TRANSACTIONS", "AVG_TICKET"])
            else:
                facts.append(
                    self._fact(
                        metric_id="TRANSACTIONS",
                        scope=scope,
                        current=current_tx,
                        previous=previous_tx,
                        source=f"crm.cards.{scope.dimension_key}.transactions",
                        comparison_basis=comparison_basis,
                    )
                )
                facts.append(
                    self._fact(
                        metric_id="AVG_TICKET",
                        scope=scope,
                        current=cur_sales / current_tx if cur_sales is not None and current_tx else None,
                        previous=prev_sales / previous_tx if prev_sales is not None and previous_tx else None,
                        source="derived:CRM_SALES/TRANSACTIONS",
                        comparison_basis=comparison_basis,
                    )
                )
        elif scope.dimension_type == "REGION_CARD":
            current_members = _number(sales.get("active_members"))
            previous_members = _number(sales.get("previous_active_members"))
            current_tx = _number(sales.get("transactions"))
            previous_tx = _number(sales.get("previous_transactions"))
            for metric_id, current, previous, source in (
                ("ACTIVE_MEMBERS", current_members, previous_members, f"crm.district_card.{scope.dimension_key}.active_members"),
                ("TRANSACTIONS", current_tx, previous_tx, f"crm.district_card.{scope.dimension_key}.transactions"),
            ):
                if current is None or previous is None:
                    missing.append(metric_id)
                    continue
                facts.append(
                    self._fact(
                        metric_id=metric_id,
                        scope=scope,
                        current=current,
                        previous=previous,
                        source=source,
                        comparison_basis=comparison_basis,
                    )
                )
            cur_sales = _number(sales.get("current"))
            prev_sales = _number(sales.get("previous"))
            if current_tx is None or previous_tx is None:
                missing.append("AVG_TICKET")
            else:
                facts.append(
                    self._fact(
                        metric_id="AVG_TICKET",
                        scope=scope,
                        current=cur_sales / current_tx if cur_sales is not None and current_tx else None,
                    previous=prev_sales / previous_tx if prev_sales is not None and previous_tx else None,
                    source="derived:CRM_SALES/TRANSACTIONS",
                    comparison_basis=comparison_basis,
                )
            )
        elif scope.dimension_type == "BRAND":
            record = self._lookup("CRM_SALES", scope, comparison_basis)
            current_tx = _number(record.get("transactions"))
            previous_tx = _number(record.get("previous_transactions"))
            if current_tx is None or previous_tx is None:
                missing.extend(["MEMBER_COUNT", "TRANSACTIONS", "AVG_TICKET"])
            else:
                facts.extend([
                    self._fact(metric_id="TRANSACTIONS", scope=scope, current=current_tx, previous=previous_tx, source=f"crm.brands.{scope.dimension_key}.transactions", comparison_basis=comparison_basis),
                    self._fact(metric_id="AVG_TICKET", scope=scope, current=record.get("ticket"), previous=record.get("previous_ticket"), source=f"crm.brands.{scope.dimension_key}.ticket", comparison_basis=comparison_basis),
                ])
        else:
            return self._unavailable(scope, "MEMBER_BEHAVIOR supports TOTAL, REGION, CARD, or REGION_CARD aggregates")

        if scope.dimension_type == "CARD" and target_metric == "NEW_REGISTRATIONS":
            new_recruited = self._mapping(crm, "new_recruited").get(scope.dimension_key)
            if isinstance(new_recruited, Mapping):
                facts.append(self._fact(metric_id="NEW_REGISTRATIONS", scope=scope, current=new_recruited.get("current"), previous=new_recruited.get("previous"), source=f"crm.new_recruited.{scope.dimension_key}", comparison_basis=comparison_basis))
            else:
                missing.append("NEW_REGISTRATIONS")
        valid_facts = [item for item in facts if item["values"]["current"] is not None]
        return {
            "status": "PARTIAL" if missing else "OK",
            "evidence_status": "PARTIAL" if missing else "AVAILABLE",
            "scope": scope.label,
            "metrics": valid_facts,
            "missing_metrics": missing,
            "fact_ids": [item["fact_id"] for item in valid_facts],
            "facts": valid_facts,
            "data_quality_status": self._quality(scope),
            "unavailable_reason": "、".join(sorted(set(missing))) if missing else None,
        }

    @staticmethod
    def _member_period_pair(comparison_basis: str) -> tuple[str, str]:
        pairs = {
            "WOW": ("current", "previous"),
            "YOY": ("current", "yoy"),
            "YTD": ("ytd", "prior_ytd"),
            "CONTINUOUS_PERIOD": ("current", "previous"),
        }
        if comparison_basis not in pairs:
            raise KeyError(f"MEMBER_BEHAVIOR does not support {comparison_basis} member periods")
        return pairs[comparison_basis]

    @staticmethod
    def _scope_member_rows(records: Sequence[Mapping[str, Any]], scope: Scope, period: str) -> list[Mapping[str, Any]]:
        output: list[Mapping[str, Any]] = []
        for item in records:
            if scope.region_id and item.get("region") != scope.region_id:
                continue
            if item.get("period") != period:
                continue
            if scope.region_id and item.get("region") != scope.region_id:
                continue
            if scope.dimension_type == "REGION" and item.get("region") != scope.dimension_key:
                continue
            if scope.dimension_type == "CARD" and item.get("card_tier") != scope.dimension_key:
                continue
            if scope.dimension_type == "REGION_CARD":
                region, card = scope.dimension_key.split("|", 1)
                if item.get("region") != region or item.get("card_tier") != card:
                    continue
            if scope.dimension_type in {"BRAND", "STORE"} and item.get("brand") != scope.dimension_key:
                continue
            output.append(item)
        return output

    @staticmethod
    def _member_rollup(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for item in records:
            member_id = str(item.get("member_id", ""))
            if not member_id:
                continue
            bucket = output.setdefault(
                member_id,
                {
                    "crm_sales": 0.0,
                    "transactions": 0,
                    "member_record_ids": [],
                    "brands": set(),
                    "regions": set(),
                    "card_tiers": set(),
                    "member_types": set(),
                },
            )
            bucket["crm_sales"] += _number(item.get("crm_sales")) or 0.0
            bucket["transactions"] += int(_number(item.get("transactions")) or 0)
            if item.get("member_record_id"):
                bucket["member_record_ids"].append(str(item["member_record_id"]))
            for source_field, target_field in (
                ("brand", "brands"),
                ("region", "regions"),
                ("card_tier", "card_tiers"),
                ("member_type", "member_types"),
            ):
                if item.get(source_field):
                    bucket[target_field].add(str(item[source_field]))
        return output

    @classmethod
    def _member_summary(cls, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        members = cls._member_rollup(records)
        return {
            "sales": sum(item["crm_sales"] for item in members.values()),
            "transactions": sum(item["transactions"] for item in members.values()),
            "active_members": sum(item["transactions"] > 0 for item in members.values()),
            "members": members,
        }

    @staticmethod
    def _format_amount(value: Any, *, change: bool = False) -> str | None:
        number = _number(value)
        if number is None:
            return None
        sign = "+" if change and number > 0 else ""
        return f"{sign}{number / 10000:.2f}万"

    @staticmethod
    def _format_count(value: Any, *, change: bool = False) -> str | None:
        number = _number(value)
        if number is None:
            return None
        sign = "+" if change and number > 0 else ""
        return f"{sign}{number:g}人"

    @classmethod
    def _audit_records(
        cls,
        fact: Mapping[str, Any],
        current_rows: Sequence[Mapping[str, Any]],
        baseline_rows: Sequence[Mapping[str, Any]],
        *,
        evidence_type: str,
        current_period: str,
        baseline_period: str,
        post_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        current = cls._member_rollup(current_rows)
        baseline = cls._member_rollup(baseline_rows)
        post = cls._member_rollup(post_rows or [])
        records: list[dict[str, Any]] = []
        for member_id in sorted(set(current) | set(baseline) | set(post)):
            current_item = current.get(member_id, {})
            baseline_item = baseline.get(member_id, {})
            post_item = post.get(member_id, {})
            current_sales = _number(current_item.get("crm_sales")) or 0.0
            baseline_sales = _number(baseline_item.get("crm_sales")) or 0.0
            post_sales = _number(post_item.get("crm_sales")) or 0.0
            metadata = [current_item, baseline_item, post_item]
            payload = {
                "fact_id": fact["fact_id"],
                "member_id": member_id,
                "evidence_type": evidence_type,
                "current_period": current_period,
                "baseline_period": baseline_period,
            }
            records.append(
                {
                    "audit_record_id": _audit_id(payload),
                    "fact_id": fact["fact_id"],
                    "fact_ids": [fact["fact_id"]],
                    "evidence_type": evidence_type,
                    "member_id": member_id,
                    "brand": "、".join(sorted(set().union(*(item.get("brands", set()) for item in metadata)))),
                    "region": "、".join(sorted(set().union(*(item.get("regions", set()) for item in metadata)))),
                    "card_tier": "、".join(sorted(set().union(*(item.get("card_tiers", set()) for item in metadata)))),
                    "member_type": "、".join(sorted(set().union(*(item.get("member_types", set()) for item in metadata)))),
                    "current_period": current_period,
                    "baseline_period": baseline_period,
                    "current_sales": _round(current_sales),
                    "baseline_sales": _round(baseline_sales),
                    "sales_change": _round(current_sales - baseline_sales),
                    "current_transactions": int(current_item.get("transactions", 0)),
                    "baseline_transactions": int(baseline_item.get("transactions", 0)),
                    "post_sales": _round(post_sales) if post_rows is not None else None,
                    "post_transactions": int(post_item.get("transactions", 0)) if post_rows is not None else None,
                    "member_record_ids": sorted(set().union(*(set(item.get("member_record_ids", [])) for item in metadata))),
                    "display_fields": {
                        "current_sales": cls._format_amount(current_sales),
                        "baseline_sales": cls._format_amount(baseline_sales),
                        "sales_change": cls._format_amount(current_sales - baseline_sales, change=True),
                        "current_transactions": f"{int(current_item.get('transactions', 0))}笔",
                        "baseline_transactions": f"{int(baseline_item.get('transactions', 0))}笔",
                        "post_sales": cls._format_amount(post_sales) if post_rows is not None else None,
                        "post_transactions": f"{int(post_item.get('transactions', 0))}笔" if post_rows is not None else None,
                    },
                }
            )
        return records

    def _member_behavior_from_records(
        self,
        query: Mapping[str, Any],
        scope: Scope,
        crm: Mapping[str, Any],
        analysis_items: set[str],
    ) -> dict[str, Any]:
        comparison_basis = str(query.get("comparison_basis") or "WOW").upper()
        current_period, baseline_period = self._member_period_pair(comparison_basis)
        status = self._mapping(crm, "member_period_status")
        for period in (current_period, baseline_period):
            period_status = self._mapping(status, period)
            expected_regions = [scope.region_id] if scope.region_id else [scope.dimension_key] if scope.dimension_type=="REGION" else self.bundle.get("brand_regions",{}).get(scope.dimension_key,[]) if scope.dimension_type in {"BRAND","STORE"} else ["N","S","W"]
            if period_status.get("available") is not True or any(not period_status.get("regions",{}).get(r,False) for r in expected_regions):
                return self._unavailable(scope, f"Member aggregate period is unavailable: {period}")

        records = self._sequence(crm, "member_week_facts")
        current_rows = self._scope_member_rows(records, scope, current_period)
        baseline_rows = self._scope_member_rows(records, scope, baseline_period)
        if scope.dimension_type in {"BRAND","STORE"}:
            for rows,period in ((current_rows,current_period),(baseline_rows,baseline_period)):
                start=status[period].get("start_date")
                regions=[scope.region_id] if scope.region_id else self.bundle.get("brand_regions",{}).get(scope.dimension_key,[])
                if not rows and not (regions and all(self.bundle.get("coverage_manifest",{}).get("crm",{}).get(start,{}).get(r)=="COMPLETE" for r in regions)):
                    return self._unavailable(scope,"Brand absence has no complete-coverage declaration")
        current = self._member_summary(current_rows)
        baseline = self._member_summary(baseline_rows)
        source = f"crm.member_week_facts[{current_period}|{baseline_period}]"
        facts = [
            self._fact(metric_id="CRM_SALES", scope=scope, current=current["sales"], previous=baseline["sales"], source=source, comparison_basis=comparison_basis),
            self._fact(metric_id="ACTIVE_MEMBERS", scope=scope, current=current["active_members"], previous=baseline["active_members"], source=source, comparison_basis=comparison_basis),
            self._fact(metric_id="TRANSACTIONS", scope=scope, current=current["transactions"], previous=baseline["transactions"], source=source, comparison_basis=comparison_basis),
            self._fact(
                metric_id="AVG_TICKET",
                scope=scope,
                current=current["sales"] / current["transactions"] if current["transactions"] else None,
                previous=baseline["sales"] / baseline["transactions"] if baseline["transactions"] else None,
                source="derived:member_week_facts.CRM_SALES/TRANSACTIONS",
                comparison_basis=comparison_basis,
            ),
            self._fact(
                metric_id="SALES_PER_ACTIVE_MEMBER",
                scope=scope,
                current=current["sales"] / current["active_members"] if current["active_members"] else None,
                previous=baseline["sales"] / baseline["active_members"] if baseline["active_members"] else None,
                source="derived:member_week_facts.CRM_SALES/ACTIVE_MEMBERS",
                comparison_basis=comparison_basis,
            ),
        ]
        audit_records: list[dict[str, Any]] = []
        missing: list[str] = []

        if "CARD_TIER" in analysis_items:
            card_tiers = ["SILVER","GOLD","BLACK","BLACK_DIAMOND","STAR_DIAMOND"]
            for card_tier in card_tiers:
                current_card = [item for item in current_rows if item.get("card_tier") == card_tier]
                baseline_card = [item for item in baseline_rows if item.get("card_tier") == card_tier]
                current_card_summary = self._member_summary(current_card)
                baseline_card_summary = self._member_summary(baseline_card)
                fact = self._fact(
                    metric_id="BRAND_CARD_TIER_ACTIVE_MEMBERS",
                    scope=Scope(f"{scope.dimension_type}_CARD", f"{scope.dimension_key}|{card_tier}", scope.region_id),
                    current=current_card_summary["active_members"],
                    previous=baseline_card_summary["active_members"],
                    source=source,
                    comparison_basis=comparison_basis,
                    extra={"card_tier": card_tier},
                )
                fact["display_fields"].update(
                    {
                        "card_tier": card_tier,
                        "current_sales": self._format_amount(current_card_summary["sales"]),
                        "baseline_sales": self._format_amount(baseline_card_summary["sales"]),
                        "sales_change": self._format_amount(current_card_summary["sales"] - baseline_card_summary["sales"], change=True),
                    }
                )
                sales_fact=self._fact(metric_id="CARD_TIER_CRM_SALES",scope=Scope(f"{scope.dimension_type}_CARD", f"{scope.dimension_key}|{card_tier}",scope.region_id),current=current_card_summary["sales"],previous=baseline_card_summary["sales"],source=source,comparison_basis=comparison_basis,extra={"card_tier":card_tier})
                now_share=current_card_summary["sales"]/current["sales"] if current["sales"] else None
                before_share=baseline_card_summary["sales"]/baseline["sales"] if baseline["sales"] else None
                delta=current_card_summary["sales"]-baseline_card_summary["sales"]
                deltas=[self._member_summary([x for x in current_rows if x.get("card_tier")==c])["sales"]-self._member_summary([x for x in baseline_rows if x.get("card_tier")==c])["sales"] for c in card_tiers]
                denom=sum(abs(v) for v in deltas if v*delta>0)
                sales_fact["display_fields"].update(card_tier=card_tier,member_count=str(current_card_summary["active_members"]),previous_member_count=str(baseline_card_summary["active_members"]),sales_share=None if now_share is None else f"{now_share*100:.2f}%",previous_sales_share=None if before_share is None else f"{before_share*100:.2f}%",share_change_pp=None if now_share is None or before_share is None else f"{(now_share-before_share)*100:+.2f}个百分点",directional_contribution_share=None if not denom else f"{abs(delta)/denom*100:.2f}%")
                facts.append(sales_fact)
                audits = self._audit_records(fact, current_card, baseline_card, evidence_type="CARD_TIER", current_period=current_period, baseline_period=baseline_period)
                fact["audit_record_ids"] = [item["audit_record_id"] for item in audits]
                facts.append(fact)
                audit_records.extend(audits)

        if "AMOUNT_BAND" in analysis_items:
            band_facts, band_missing = self._amount_bands(scope,current_rows,baseline_rows,comparison_basis)
            facts.extend(band_facts)
            if band_missing: missing.append(band_missing)

        if "REFUND" in analysis_items:
            facts.append(self._fact(metric_id="REFUND_ADJUSTMENT",scope=scope,current=sum(float(r.get("refund_amount") or 0) for r in current_rows),previous=sum(float(r.get("refund_amount") or 0) for r in baseline_rows),source=source,comparison_basis=comparison_basis))

        if "NEW_EXISTING" in analysis_items:
            member_types = sorted({str(item.get("member_type")) for item in [*current_rows, *baseline_rows] if item.get("member_type")})
            for member_type in member_types:
                current_type = [item for item in current_rows if item.get("member_type") == member_type]
                baseline_type = [item for item in baseline_rows if item.get("member_type") == member_type]
                current_type_summary = self._member_summary(current_type)
                baseline_type_summary = self._member_summary(baseline_type)
                fact = self._fact(
                    metric_id="NEW_EXISTING_ACTIVE_MEMBERS",
                    scope=Scope(f"{scope.dimension_type}_MEMBER_TYPE", f"{scope.dimension_key}|{member_type}"),
                    current=current_type_summary["active_members"],
                    previous=baseline_type_summary["active_members"],
                    source=source,
                    comparison_basis=comparison_basis,
                    extra={"member_type": member_type},
                )
                fact["display_fields"].update(
                    {
                        "member_type": member_type,
                        "current_sales": self._format_amount(current_type_summary["sales"]),
                        "baseline_sales": self._format_amount(baseline_type_summary["sales"]),
                        "sales_change": self._format_amount(current_type_summary["sales"] - baseline_type_summary["sales"], change=True),
                    }
                )
                audits = self._audit_records(fact, current_type, baseline_type, evidence_type="NEW_EXISTING", current_period=current_period, baseline_period=baseline_period)
                fact["audit_record_ids"] = [item["audit_record_id"] for item in audits]
                facts.append(fact)
                audit_records.extend(audits)

        if "LARGE_SPEND" in analysis_items:
            if comparison_basis not in {"WOW", "YOY", "CONTINUOUS_PERIOD"}:
                missing.append("LARGE_SPEND仅支持自然周WOW、YOY或连续期比较")
            elif scope.dimension_type not in {"BRAND", "STORE"}:
                missing.append("LARGE_SPEND仅支持品牌范围")
            else:
                threshold = 100_000.0
                current_large_members = {member_id for member_id, item in current["members"].items() if item["crm_sales"] >= threshold}
                baseline_large_members = {member_id for member_id, item in baseline["members"].items() if item["crm_sales"] >= threshold}
                current_large = [item for item in current_rows if str(item.get("member_id")) in current_large_members]
                baseline_large = [item for item in baseline_rows if str(item.get("member_id")) in baseline_large_members]
                current_large_summary = self._member_summary(current_large)
                baseline_large_summary = self._member_summary(baseline_large)
                sales_gap = current["sales"] - baseline["sales"]
                large_spend_change = current_large_summary["sales"] - baseline_large_summary["sales"]
                contribution_share = large_spend_change / sales_gap if sales_gap else None
                fact = self._fact(
                    metric_id="LARGE_SPEND_CONTRIBUTION",
                    scope=scope,
                    current=current_large_summary["sales"],
                    previous=baseline_large_summary["sales"],
                    source=source,
                    comparison_basis=comparison_basis,
                    extra={"threshold": threshold},
                )
                fact["contribution_share"] = _round(contribution_share)
                fact["display_fields"].update(
                    {
                        "threshold": self._format_amount(threshold),
                        "current_large_spend_member_count": self._format_count(len(current_large_members)),
                        "baseline_large_spend_member_count": self._format_count(len(baseline_large_members)),
                        "large_spend_member_change": self._format_count(len(current_large_members) - len(baseline_large_members), change=True),
                        "current_large_spend_amount": self._format_amount(current_large_summary["sales"]),
                        "baseline_large_spend_amount": self._format_amount(baseline_large_summary["sales"]),
                        "large_spend_amount_change": self._format_amount(large_spend_change, change=True),
                        "sales_gap": self._format_amount(sales_gap, change=True),
                        "contribution_share": None if contribution_share is None else f"{contribution_share * 100:+.2f}%",
                    }
                )
                audits = self._audit_records(fact, current_large, baseline_large, evidence_type="LARGE_SPEND", current_period=current_period, baseline_period=baseline_period)
                fact["audit_record_ids"] = [item["audit_record_id"] for item in audits]
                facts.append(fact)
                audit_records.extend(audits)

        if "ACTIVITY_RESPONSE" in analysis_items:
            activity_fact, activity_audits, activity_missing = self._activity_response_fact(query, scope, crm)
            if activity_fact is None:
                missing.append(activity_missing or "ACTIVITY_RESPONSE数据不可用")
            else:
                facts.append(activity_fact)
                audit_records.extend(activity_audits)
                if activity_missing:
                    missing.append(activity_missing)

        valid_facts = [item for item in facts if item["values"]["current"] is not None]
        return {
            "status": "PARTIAL" if missing else "OK",
            "evidence_status": "PARTIAL" if missing else "AVAILABLE",
            "scope": scope.label,
            "metrics": valid_facts,
            "missing_metrics": sorted(set(missing)),
            "fact_ids": [item["fact_id"] for item in valid_facts],
            "facts": valid_facts,
            "audit_record_ids": [item["audit_record_id"] for item in audit_records],
            "audit_records": audit_records,
            "data_quality_status": self._quality(scope),
            "unavailable_reason": "、".join(sorted(set(missing))) if missing else None,
        }

    @staticmethod
    def _date_window(raw: Any, field: str) -> tuple[date, date]:
        if not isinstance(raw, Mapping):
            raise InvestigationError(f"filters.{field} must be an object")
        try:
            start = date.fromisoformat(str(raw["start_date"]))
            end = date.fromisoformat(str(raw["end_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise InvestigationError(f"filters.{field} must contain valid start_date/end_date") from exc
        if start > end:
            raise InvestigationError(f"filters.{field}.start_date must not be after end_date")
        return start, end

    @staticmethod
    def _scope_day_rows(records: Sequence[Mapping[str, Any]], scope: Scope, bounds: tuple[date, date]) -> list[Mapping[str, Any]]:
        output = []
        for item in records:
            try:
                event_date = date.fromisoformat(str(item.get("date")))
            except ValueError:
                continue
            if not bounds[0] <= event_date <= bounds[1]:
                continue
            if scope.region_id and item.get("region") != scope.region_id:
                continue
            if scope.dimension_type == "REGION" and item.get("region") != scope.dimension_key:
                continue
            if scope.dimension_type == "CARD" and item.get("card_tier") != scope.dimension_key:
                continue
            if scope.dimension_type == "REGION_CARD":
                region, card = scope.dimension_key.split("|", 1)
                if item.get("region") != region or item.get("card_tier") != card:
                    continue
            if scope.dimension_type in {"BRAND", "STORE"} and item.get("brand") != scope.dimension_key:
                continue
            output.append(item)
        return output

    def _activity_response_fact(
        self,
        query: Mapping[str, Any],
        scope: Scope,
        crm: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
        from .activity_comparison import activity_response
        return activity_response(self, query, scope, crm)

    def _brand_operation(self, query: Mapping[str, Any], scope: Scope) -> dict[str, Any]:
        comparison_basis = str(query.get("comparison_basis") or "WOW").upper()
        crm = self._lookup("CRM_SALES", scope, comparison_basis)
        mall = self._lookup("MALL_SALES", scope, comparison_basis)
        common_stores = scope.dimension_type == "BRAND" or query.get("filters", {}).get("operating_scope") == "COMMON_STORES_TWO_PERIODS"
        matched=self.bundle.get("matched_operating",{}).get(scope.label) if comparison_basis=="WOW" and common_stores else None
        if matched is not None:
            crm=dict(crm,current=matched["crm_current"],previous=matched["crm_previous"],source="matched_operating.crm")
            mall=dict(mall,current=matched["mall_current"],previous=matched["mall_previous"],source="matched_operating.mall")
        crm_fact = self._fact(
            metric_id="CRM_SALES",
            scope=scope,
            current=crm.get("current"),
            previous=crm.get("previous"),
            source=crm["source"],
            comparison_basis=comparison_basis,
        )
        mall_fact = self._fact(
            metric_id="MALL_SALES",
            scope=scope,
            current=mall.get("current"),
            previous=mall.get("previous"),
            source=mall["source"],
            comparison_basis=comparison_basis,
        )
        crm_cur = _number(crm.get("current"))
        crm_prev = _number(crm.get("previous"))
        mall_cur = _number(mall.get("current"))
        mall_prev = _number(mall.get("previous"))
        capture_cur = crm_cur / mall_cur if crm_cur is not None and mall_cur else None
        capture_prev = crm_prev / mall_prev if crm_prev is not None and mall_prev else None
        capture_fact = self._fact(
            metric_id="CAPTURE_RATIO",
            scope=scope,
            current=capture_cur,
            previous=capture_prev,
            source="derived:CRM_SALES/MALL_SALES",
            comparison_basis=comparison_basis,
            extra={
                "pp_change": _round((capture_cur - capture_prev) * 100)
                if capture_cur is not None and capture_prev is not None
                else None
            },
        )
        facts = [crm_fact, mall_fact, capture_fact]
        if not common_stores:
            for fact in facts:
                fact['coverage_status'] = 'OBSERVED_AGGREGATE'
                fact['display_fields']['coverage_scope'] = 'REGIONAL_TOTALS'
                fact['applicability_limits'].append('区域/全场按源文件有效金额汇总；Mall缺值单列说明，不要求CRM与Mall交易店铺名单完全一致。')
        if matched is not None:
            for fact in facts:
                fact["coverage_status"]="MATCHED_FULL" if matched["full_coverage"] else "MATCHED_SUBSET"
                fact["display_fields"].update(coverage_scope=matched["scope"],matched_store_count=str(matched["store_count"]),expected_store_count=str(matched["expected_store_count"]))
                fact["applicability_limits"].append("CRM/Mall仅对两期共同覆盖店铺比较；不等于积分意愿因果证明")
        if all(v is not None for v in (crm_cur,crm_prev,mall_cur,mall_prev,capture_cur,capture_prev)):
            mall_component=(capture_cur+capture_prev)/2*(mall_cur-mall_prev)
            capture_component=(mall_cur+mall_prev)/2*(capture_cur-capture_prev)
            capture_fact["display_fields"].update(mall_component=self._display("CRM_SALES",mall_component,change=True),capture_component=self._display("CRM_SALES",capture_component,change=True),crm_change=self._display("CRM_SALES",crm_cur-crm_prev,change=True))
            capture_fact["applicability_limits"].append("两期均值会计拆分：两分量之和等于CRM变化，不能作为因果证据")
        missing = [item["metric_id"] for item in facts if item["values"]["current"] is None]
        return {
            "status": "PARTIAL" if missing else "OK",
            "evidence_status": "PARTIAL" if missing else "AVAILABLE",
            "scope": scope.label,
            "operation_metrics": {item["metric_id"]: item for item in facts},
            "missing_metrics": missing,
            "fact_ids": [item["fact_id"] for item in facts if item["metric_id"] not in missing],
            "facts": [item for item in facts if item["metric_id"] not in missing],
            "data_quality_status": self._quality(scope),
            "refund": {"evidence_status": "UNAVAILABLE", "unavailable_reason": "当前聚合输入未提供退款原交易周字段"},
            "operating_status": {"evidence_status": "UNAVAILABLE", "unavailable_reason": "当前聚合输入未提供闭店、装修或营业时间调整主档"},
            "unavailable_reason": "、".join(missing) if missing else None,
        }

    def _lookup(self, metric_id: str, scope: Scope, comparison_basis: str = "WOW") -> dict[str, Any]:
        if scope.dimension_type in {"BRAND","STORE"} and scope.region_id:
            if metric_id == "CRM_SALES":
                row=next((r for r in self.bundle["crm"]["district_brands"] if r["district"]==scope.region_id and r["brand"]==scope.dimension_key),None)
                if row is None: raise KeyError("Scoped brand aggregate unavailable")
                return self._comparison_record(dict(row),comparison_basis)|{"source":"crm.district_brands"}
            if metric_id == "MALL_SALES":
                table=self.bundle["mall"]["district_brand"]
                row={k:table.get(k,{}).get(scope.region_id,{}).get(scope.dimension_key) for k in ("current","previous","yoy","ytd","prior_ytd")}
                return self._comparison_record(row,comparison_basis)|{"source":"mall.district_brand"}
        if metric_id == "CRM_SALES":
            crm = self._mapping(self.bundle, "crm")
            if scope.dimension_type in {"TOTAL", "REGION"}:
                key = "Total" if scope.dimension_type == "TOTAL" else scope.dimension_key
                return self._comparison_record(dict(self._mapping(self._mapping(crm, "district"), key)), comparison_basis) | {"source": f"crm.district.{key}"}
            if scope.dimension_type == "CARD":
                return self._comparison_record(dict(self._mapping(self._mapping(crm, "cards"), scope.dimension_key)), comparison_basis) | {"source": f"crm.cards.{scope.dimension_key}"}
            if scope.dimension_type == "BRAND":
                return self._comparison_record(dict(self._mapping(self._mapping(crm, "brands"), scope.dimension_key)), comparison_basis) | {"source": f"crm.brands.{scope.dimension_key}"}
            if scope.dimension_type == "STORE":
                item = self._find(self._sequence(crm, "stores"), "store", scope.dimension_key)
                return self._comparison_record(dict(item), comparison_basis) | {"source": f"crm.stores[{scope.dimension_key}]"}
            if scope.dimension_type == "REGION_CARD":
                region, card = scope.dimension_key.split("|", 1)
                item = self._find_multi(self._sequence(crm, "district_card"), {"district": region, "card": card})
                return self._comparison_record(dict(item), comparison_basis) | {"source": f"crm.district_card[{region}|{card}]"}
        elif metric_id == "MALL_SALES":
            mall = self._mapping(self.bundle, "mall")
            if scope.dimension_type in {"TOTAL", "REGION"}:
                key = "Total" if scope.dimension_type == "TOTAL" else scope.dimension_key
                return self._comparison_record(dict(self._mapping(self._mapping(mall, "district"), key)), comparison_basis) | {"source": f"mall.district.{key}"}
            if scope.dimension_type == "BRAND":
                brand = self._mapping(mall, "brand")
                return self._comparison_record({
                    "current": self._mapping(brand, "current").get(scope.dimension_key),
                    "previous": self._mapping(brand, "previous").get(scope.dimension_key),
                    "yoy": self._mapping_or_empty(brand.get("yoy")).get(scope.dimension_key),
                    "ytd": self._mapping_or_empty(brand.get("ytd")).get(scope.dimension_key),
                    "prior_ytd": self._mapping_or_empty(brand.get("prior_ytd")).get(scope.dimension_key),
                    "source": f"mall.brand.*.{scope.dimension_key}",
                }, comparison_basis)
            if scope.dimension_type == "STORE":
                store_values = self._mapping(mall, "store")
                return self._comparison_record({
                    "current": self._sum_store(self._mapping(store_values, "current"), scope.dimension_key),
                    "previous": self._sum_store(self._mapping(store_values, "previous"), scope.dimension_key),
                    "yoy": self._sum_store(self._mapping_or_empty(store_values.get("yoy")), scope.dimension_key),
                    "ytd": self._sum_store(self._mapping_or_empty(store_values.get("ytd")), scope.dimension_key),
                    "prior_ytd": self._sum_store(self._mapping_or_empty(store_values.get("prior_ytd")), scope.dimension_key),
                    "source": f"mall.store.*.*.{scope.dimension_key}",
                }, comparison_basis)
        raise KeyError(f"Missing aggregate for {metric_id} at {scope.label}")

    def _children(self, metric_id: str, scope: Scope, dimension: str, comparison_basis: str = "WOW") -> list[tuple[Scope, dict[str, Any]]]:
        if metric_id == "CRM_SALES":
            crm = self._mapping(self.bundle, "crm")
            if scope.dimension_type == "TOTAL" and dimension == "REGION":
                district = self._mapping(crm, "district")
                return [
                    (Scope("REGION", key), dict(self._mapping(district, key)) | {"source": f"crm.district.{key}"})
                    for key in ("N", "S", "W")
                    if key in district
                ] if comparison_basis == "WOW" else [(child, self._comparison_record(record, comparison_basis)) for child, record in [
                    (Scope("REGION", key), dict(self._mapping(district, key)) | {"source": f"crm.district.{key}"}) for key in ("N", "S", "W") if key in district
                ]]
            if scope.dimension_type == "TOTAL" and dimension == "CARD":
                cards = self._mapping(crm, "cards")
                return [
                    (Scope("CARD", key), dict(value) | {"source": f"crm.cards.{key}"})
                    for key, value in cards.items()
                    if key != "Total" and isinstance(value, Mapping)
                ]
            if scope.dimension_type == "TOTAL" and dimension == "BRAND":
                brands = self._mapping(crm, "brands")
                return [
                    (Scope("BRAND", key), dict(value) | {"source": f"crm.brands.{key}"})
                    for key, value in brands.items()
                    if isinstance(value, Mapping)
                ]
            if scope.dimension_type == "TOTAL" and dimension == "STORE":
                return [
                    (Scope("STORE", str(item["store"])), dict(item) | {"source": f"crm.stores[{item['store']}]"})
                    for item in self._sequence(crm, "stores")
                    if item.get("store")
                ]
            if scope.dimension_type == "REGION" and dimension == "CARD":
                return [
                    (Scope("REGION_CARD", f"{scope.dimension_key}|{item['card']}"), dict(item) | {"source": f"crm.district_card[{scope.dimension_key}|{item['card']}]"})
                    for item in self._sequence(crm, "district_card")
                    if item.get("district") == scope.dimension_key and item.get("card")
                ]
            if scope.dimension_type == "REGION" and dimension == "STORE":
                return [
                    (Scope("STORE", str(item["store"])), dict(item) | {"source": f"crm.district_stores[{scope.dimension_key}|{item['store']}]"})
                    for item in self._sequence(crm, "district_stores")
                    if item.get("district") == scope.dimension_key and item.get("store")
                ]
            if scope.dimension_type == "REGION" and dimension == "BRAND":
                return [
                    (Scope("BRAND", str(item["brand"]), scope.dimension_key), dict(item) | {"source": f"crm.district_brands[{scope.dimension_key}|{item['brand']}]"})
                    for item in self._sequence(crm, "district_brands")
                    if item.get("district") == scope.dimension_key and item.get("brand")
                ]
        elif metric_id == "MALL_SALES":
            mall = self._mapping(self.bundle, "mall")
            if scope.dimension_type == "TOTAL" and dimension == "REGION":
                district = self._mapping(mall, "district")
                return [
                    (Scope("REGION", key), dict(self._mapping(district, key)) | {"source": f"mall.district.{key}"})
                    for key in ("N", "S", "W")
                    if key in district
                ]
            if scope.dimension_type in {"TOTAL", "REGION"} and dimension == "STORE":
                store = self._mapping(mall, "store")
                current = self._mapping(store, "current")
                previous = self._mapping(store, "previous")
                regions = ("N", "S", "W") if scope.dimension_type == "TOTAL" else (scope.dimension_key,)
                rows = []
                for region in regions:
                    cur = self._mapping_or_empty(current.get(region))
                    prev = self._mapping_or_empty(previous.get(region))
                    for key in sorted(set(cur) | set(prev)):
                        rows.append(
                            (
                                Scope("STORE", key),
                                {
                                    "current": cur.get(key, 0),
                                    "previous": prev.get(key, 0),
                                    "source": f"mall.store.*.{region}.{key}",
                                },
                            )
                        )
                return self._merge_children(rows)
            if scope.dimension_type == "TOTAL" and dimension == "BRAND":
                brand = self._mapping(mall, "brand")
                current = self._mapping(brand, "current")
                previous = self._mapping(brand, "previous")
                return [
                    (
                        Scope("BRAND", key),
                        {
                            "current": current.get(key, 0),
                            "previous": previous.get(key, 0),
                            "source": f"mall.brand.*.{key}",
                        },
                    )
                    for key in sorted(set(current) | set(previous))
                ]
            if scope.dimension_type == "REGION" and dimension == "BRAND":
                district_brand = self._mapping(mall, "district_brand")
                current = self._mapping_or_empty(self._mapping_or_empty(district_brand.get("current")).get(scope.dimension_key))
                previous = self._mapping_or_empty(self._mapping_or_empty(district_brand.get("previous")).get(scope.dimension_key))
                return [
                    (
                        Scope("BRAND", key),
                        {
                            "current": current.get(key, 0),
                            "previous": previous.get(key, 0),
                            "source": f"mall.district_brand.*.{scope.dimension_key}.{key}",
                        },
                    )
                    for key in sorted(set(current) | set(previous))
                ]
        return []

    def _quality(self, scope: Scope) -> list[dict[str, Any]]:
        mall = self.bundle.get("mall")
        if not isinstance(mall, Mapping):
            return [{"code": "MALL_DATA_UNAVAILABLE", "severity": "WARNING"}]
        issues = mall.get("quality", [])
        if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes)):
            return []
        regions = {scope.dimension_key} if scope.dimension_type == "REGION" else {"N", "S", "W"}
        output = []
        for issue in issues:
            if not isinstance(issue, Mapping) or issue.get("district") not in regions:
                continue
            output.append(
                {
                    "code": "MALL_SALES_PARTIAL_MISSING",
                    "severity": "WARNING",
                    "affected_metric_id": "MALL_SALES",
                    "period": issue.get("period"),
                    "region": issue.get("district"),
                    "missing_cells": issue.get("missing_cells"),
                    "affected_stores": issue.get("affected_stores"),
                }
            )
        return output

    @staticmethod
    def _comparison_record(record: Mapping[str, Any], comparison_basis: str) -> dict[str, Any]:
        """Project an aggregate record onto the explicitly requested comparison.

        The returned keys remain ``current``/``previous`` so the downstream fact
        contract stays stable, while the fact itself records ``comparison_basis``.
        Missing comparison fields are rejected instead of silently substituting a
        WOW value for YOY/YTD evidence.
        """

        basis = str(comparison_basis or "WOW").upper()
        period_fields = {
            "WOW": ("current", "previous"),
            "YOY": ("current", "yoy"),
            "YTD": ("ytd", "prior_ytd"),
            # A continuous-decline query compares the latest completed period with
            # its immediately preceding period; the signal history remains upstream.
            "CONTINUOUS_PERIOD": ("current", "previous"),
            "EIGHT_WEEK_BASELINE": ("current", "eight_week_baseline"),
        }
        if basis not in period_fields:
            raise KeyError(f"Unsupported comparison basis: {basis}")
        current_field, previous_field = period_fields[basis]
        if current_field not in record or previous_field not in record:
            raise KeyError(
                f"Missing {basis} aggregate fields: {current_field}/{previous_field}"
            )

        output = dict(record)
        output["current"] = record.get(current_field)
        output["previous"] = record.get(previous_field)

        transaction_fields = {
            "WOW": ("transactions", "previous_transactions"),
            "YOY": ("transactions", "yoy_transactions"),
            "YTD": ("ytd_transactions", "prior_ytd_transactions"),
            "CONTINUOUS_PERIOD": ("transactions", "previous_transactions"),
        }
        transaction_pair = transaction_fields.get(basis)
        if transaction_pair and any(field in record for field in transaction_pair):
            current_tx_field, previous_tx_field = transaction_pair
            output["transactions"] = record.get(current_tx_field)
            output["previous_transactions"] = record.get(previous_tx_field)
            current_tx = _number(output.get("transactions"))
            previous_tx = _number(output.get("previous_transactions"))
            current = _number(output.get("current"))
            previous = _number(output.get("previous"))
            output["ticket"] = current / current_tx if current is not None and current_tx else None
            output["previous_ticket"] = previous / previous_tx if previous is not None and previous_tx else None
        return output

    @staticmethod
    def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        result = value.get(key)
        if not isinstance(result, Mapping):
            raise KeyError(f"Missing mapping: {key}")
        return result

    @staticmethod
    def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _sequence(value: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
        result = value.get(key)
        if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
            raise KeyError(f"Missing list: {key}")
        return result

    @staticmethod
    def _find(items: Iterable[Mapping[str, Any]], field: str, expected: Any) -> Mapping[str, Any]:
        for item in items:
            if item.get(field) == expected:
                return item
        raise KeyError(f"Missing aggregate where {field}={expected}")

    @staticmethod
    def _find_multi(items: Iterable[Mapping[str, Any]], expected: Mapping[str, Any]) -> Mapping[str, Any]:
        for item in items:
            if all(item.get(key) == value for key, value in expected.items()):
                return item
        label = ", ".join(f"{key}={value}" for key, value in expected.items())
        raise KeyError(f"Missing aggregate where {label}")

    @staticmethod
    def _sum_store(regions: Mapping[str, Any], store: str) -> float | None:
        found = False
        total = 0.0
        for values in regions.values():
            if isinstance(values, Mapping) and store in values:
                value = _number(values[store])
                if value is not None:
                    total += value
                    found = True
        return total if found else None

    @staticmethod
    def _merge_children(rows: Sequence[tuple[Scope, dict[str, Any]]]) -> list[tuple[Scope, dict[str, Any]]]:
        merged: dict[str, dict[str, Any]] = {}
        for scope, item in rows:
            bucket = merged.setdefault(scope.dimension_key, {"current": 0.0, "previous": 0.0, "sources": []})
            bucket["current"] += _number(item.get("current")) or 0.0
            bucket["previous"] += _number(item.get("previous")) or 0.0
            bucket["sources"].append(item.get("source"))
        return [
            (
                Scope("STORE", key),
                {
                    "current": value["current"],
                    "previous": value["previous"],
                    "source": "+".join(sorted(str(source) for source in value["sources"])),
                },
            )
            for key, value in sorted(merged.items())
        ]

    @staticmethod
    def _unavailable(scope: Scope, message: str) -> dict[str, Any]:
        return {
            "status": "DATA_UNAVAILABLE",
            "scope": scope.label,
            "error": {"code": "DATA_UNAVAILABLE", "message": message},
            "fact_ids": [],
            "facts": [],
        }


def investigate_anomalies(
    metric_bundle: Mapping[str, Any],
    request: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Run one batch of deterministic aggregate investigations."""

    return InvestigationEngine(metric_bundle).investigate(request)
