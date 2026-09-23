"""Business impact selection. Detection severity is never rewritten by this policy."""
import hashlib


def luxury_id(name):
    n=str(name).strip().upper()
    return next((key for key,aliases in {'HERMES':{'HERMES','HERMES 爱马仕','HERMÈS','爱马仕'},'LV':{'LV','LOUIS VUITTON','LOUIS VUITTON 路易威登','路易威登'},'DIOR':{'DIOR','DIOR 迪奥','迪奥'}}.items() if n in aliases),None)


def stable_id(prefix,*parts):
    return prefix+'-'+hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()[:16]


def select_issues(anomalies, metrics, rich):
    policy=metrics['reporting_policy']; groups=anomalies['anomaly_groups']
    signals={s['signal_id']:s for s in anomalies.get('anomaly_signals',[])}
    total=next(r for r in metrics['metrics']['M01'] if r['dimension']=='Total')
    total_base=total.get('base')
    threshold=max(policy['brand_min_change_amount'],(total_base or 0)*policy['brand_min_total_base_share'])
    rows=rich['crm']['district_brands']; regions=[r for r in metrics['metrics']['M01'] if r['dimension']!='Total']
    def change(r):return None if r.get('current') is None or r.get('previous',r.get('base')) is None else r['current']-r.get('previous',r.get('base'))
    selected_regions=set()
    for sign in (-1,1):
        aligned=sorted([r for r in regions if change(r) is not None and change(r)*sign>0],key=lambda r:-abs(change(r)))
        denom=sum(abs(change(r)) for r in aligned); covered=0
        for r in aligned:
            if covered>=denom*policy['directional_coverage_target']:break
            selected_regions.add(r['dimension']);covered+=abs(change(r))
    def ensure_group(dim,key):
        matches=[g for g in groups if g['dimension_type']==dim and g['dimension_key']==key and g['metric_id']=='CRM_SALES']
        if matches:return matches[0]
        rec=next((r for r in metrics['anomaly_records'] if r['dimension_type']==dim and r['dimension_key']==key and r['metric_id']=='CRM_SALES'),{})
        delta=change({'current':rec.get('current_value'),'previous':rec.get('previous_value')})
        g=dict(anomaly_group_id=stable_id('IMPACT',metrics['week_id'],dim,key),week_id=rec.get('week_id',metrics['week_id']),dimension_type=dim,dimension_key=key,metric_id='CRM_SALES',group_kind='IMPACT_CANDIDATE',severity='INFO',group_severity='INFO',signal_ids=[],current_value=rec.get('current_value'),change_amount=delta,direction='UP' if delta and delta>0 else 'DOWN',display_value=None if rec.get('current_value') is None else f"{rec['current_value']/10000:.2f}万",display_change=None if delta is None else f'{delta/10000:+.2f}万',data_quality=[])
        groups.append(g);return g
    issues=[]; tree=[]
    total_delta=change(total)
    if total_delta is not None and abs(total_delta)>=threshold:
        group=ensure_group('TOTAL','TOTAL')
        issues.append(dict(issue_id=stable_id('ISSUE',metrics['week_id'],'TOTAL_STRUCTURE'),issue_type='INDEPENDENT_RISK',primary_scope={'dimension_type':'TOTAL','dimension_key':'TOTAL'},comparison_basis='WOW',anomaly_group_ids=[group['anomaly_group_id']],branches=[],independent_issue_reason='全场重要变化须区分卡级销售、人数、人均消费与区域贡献，不把整体均值当作各卡级普遍变化',impact_amount=total_delta,material=True))
    for r in regions:
        delta=change(r);region=r['dimension'];children=[b for b in rows if b['district']==region and change(b) is not None]
        branch=[]
        for sign in (-1,1):
            aligned=sorted([b for b in children if change(b)*sign>0],key=lambda b:-abs(change(b)))
            denominator=sum(abs(change(b)) for b in aligned)
            for b in aligned:
                if abs(change(b))<threshold:continue
                branch.append({'region_id':region,'brand_id':b['brand'],'impact_amount':change(b),'directional_contribution_share':abs(change(b))/denominator if denominator else None,'impact_pp_total':None if not total_base else change(b)/total_base*100,'analysis_items':['CORE_METRICS','AMOUNT_BAND' if luxury_id(b['brand']) else 'CARD_TIER']})
        tree.append(dict(region_id=region,impact_amount=delta,branches=branch))
        # Opposing large brands inside a stable region are independent structural changes.
        if (region in selected_regions and delta is not None and abs(delta)>=threshold) or branch:
            group=ensure_group('REGION',region)
            issues.append(dict(issue_id=stable_id('ISSUE',metrics['week_id'],'REGION_CONTRIBUTION',region),issue_type='REGION_CONTRIBUTION',primary_scope={'dimension_type':'REGION','dimension_key':region},comparison_basis='WOW',anomaly_group_ids=[group['anomaly_group_id']],branches=branch,independent_issue_reason='区域变化或内部重要正负对冲',impact_amount=delta,material=True))
    # Trend evaluation is independent of the current regional direction and WOW threshold.
    for g in list(groups):
        if g['dimension_type']!='BRAND' or g['metric_id']!='CRM_SALES':continue
        trends=[signals[s] for s in g.get('signal_ids',[]) if s in signals and signals[s].get('signal_type') in ('CONTINUOUS_DECLINE','ROLLING_MEDIAN_DEVIATION') and signals[s].get('direction')=='DOWN']
        impact=max([abs(float(t.get('details',{}).get('cumulative_change_amount') or 0)) for t in trends]+[abs(float(g.get('change_amount') or 0))])
        if trends and (impact>=threshold or luxury_id(g['dimension_key'])):
            regs=rich.get('brand_regions',{}).get(g['dimension_key'],[])
            scope={'dimension_type':'BRAND','dimension_key':g['dimension_key']}
            if g.get('region_id'):scope['region_id']=g['region_id']
            elif len(regs)==1:scope['region_id']=regs[0]
            issues.append(dict(issue_id=stable_id('ISSUE',metrics['week_id'],'BRAND_TREND',g['dimension_key'],g.get('region_id')),issue_type='BRAND_TREND',primary_scope=scope,comparison_basis='CONTINUOUS_PERIOD',anomaly_group_ids=[g['anomaly_group_id']],branches=[],independent_issue_reason='业务趋势规则命中且有累计影响或重点品牌跟进价值',impact_amount=impact,material=True))
    # Preserve large non-regional/current and long-comparison exceptions.
    for g in list(groups):
        if any(g['anomaly_group_id'] in i['anomaly_group_ids'] for i in issues):continue
        amount=abs(float(g.get('change_amount') or 0))
        long_risk=g['dimension_type']=='TOTAL' and g['metric_id']=='CRM_SALES' and any(signals.get(s,{}).get('comparison_basis') in ('YOY','YTD') for s in g.get('signal_ids',[]))
        if (g['metric_id']=='CRM_SALES' and amount>=threshold and g['dimension_type'] in ('MEMBER_TIER','REGION_MEMBER_TIER') and not any(i['issue_type']=='REGION_CONTRIBUTION' for i in issues)) or long_risk:
            issues.append(dict(issue_id=stable_id('ISSUE',metrics['week_id'],'INDEPENDENT_RISK',g['anomaly_group_id']),issue_type='INDEPENDENT_RISK',primary_scope={'dimension_type':g['dimension_type'],'dimension_key':g['dimension_key']},comparison_basis='WOW',anomaly_group_ids=[g['anomaly_group_id']],branches=[],independent_issue_reason='重要卡级风险或长期比较风险需披露',impact_amount=amount,material=True))
    selected={gid for i in issues for gid in i['anomaly_group_ids']}
    observations=[]
    for g in groups:
        eligible=g['anomaly_group_id'] in selected
        g.update(investigation_eligible=eligible,must_investigate=eligible,material=eligible,selection_reason_code='MATERIAL_ISSUE' if eligible else 'LOW_IMPACT_OR_PARENT_COVERED',impact_amount=g.get('change_amount'),comparison_basis='WOW',parent_scope=None,impact_pp_total=None if not total_base or g.get('change_amount') is None else float(g['change_amount'])/total_base*100)
        if not eligible:observations.append({'anomaly_group_id':g['anomaly_group_id'],'reason':'未形成独立重要问题；保留原始告警，贡献由上层主题调查。','reason_code':g['selection_reason_code']})
    anomalies.update(issues=issues,contribution_tree=tree,observations=observations,brand_regions=rich.get('brand_regions',{}),selection_parameters={'brand_change_amount_threshold':threshold,'directional_coverage_target':policy['directional_coverage_target']})
    return anomalies


def default_plan(anomalies,kb_version):
    plan={k:anomalies[k] for k in ('schema_version','week_id','input_signature','implementation_version')}
    plan.update(kb_version=kb_version,selected_cases=[],omitted_anomaly_groups=anomalies.get('observations',[]))
    for issue in anomalies.get('issues',[]):
        cid=issue['issue_id'];gid=issue['anomaly_group_ids'][0];queries=[]
        def add(scope,focus,items,basis='WOW',**extra):
            queries.append(dict(query_id=f'{cid}-Q{len(queries)+1}',insight_case_id=cid,anomaly_group_id=gid,scope=scope,focus=focus,analysis_items=items,comparison_basis=basis,**extra))
        scope=issue['primary_scope']
        if scope['dimension_type']=='TOTAL':
            add(scope,'CONTRIBUTION',['CONTRIBUTORS'],next_dimension='REGION',top_k=3)
            add(scope,'BRAND_OPERATION',['OPERATING_STATUS'])
            for region in ('N', 'S', 'W'):
                add({'dimension_type':'REGION','dimension_key':region},'BRAND_OPERATION',['OPERATING_STATUS'])
            add(scope,'CONTRIBUTION',['CONTRIBUTORS'],next_dimension='CARD',top_k=5)
            for card in ('STAR_DIAMOND','BLACK_DIAMOND','BLACK','GOLD','SILVER'):
                add({'dimension_type':'CARD','dimension_key':card},'MEMBER_BEHAVIOR',['CORE_METRICS'])
        if issue['issue_type']=='REGION_CONTRIBUTION':
            add(scope,'CONTRIBUTION',['CONTRIBUTORS'],next_dimension='BRAND',top_k=20)
            for b in issue['branches']:
                bs={'dimension_type':'BRAND','dimension_key':b['brand_id'],'region_id':b['region_id']}
                add(bs,'MEMBER_BEHAVIOR',b['analysis_items']);add(bs,'BRAND_OPERATION',['OPERATING_STATUS'])
            add(scope,'BRAND_OPERATION',['OPERATING_STATUS'])
        else:
            items=['CORE_METRICS']
            if scope['dimension_type']=='BRAND':items+=['AMOUNT_BAND' if luxury_id(scope['dimension_key']) else 'CARD_TIER']
            add(scope,'MEMBER_BEHAVIOR',items)
            if issue['issue_type']=='BRAND_TREND':add(scope,'MEMBER_BEHAVIOR',['WEEKLY_HISTORY'],'CONTINUOUS_PERIOD')
        plan['selected_cases'].append(dict(insight_case_id=cid,**issue,selection_reason=issue['independent_issue_reason'],queries=queries,activity_queries=[]))
    return plan
