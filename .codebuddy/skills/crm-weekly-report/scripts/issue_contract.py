"""Issue ownership, disclosure and scoped branch validation for schema 3.0 / impl 3.1."""
from common import ContractError, require_text


def reject_quota_reason(reason):
    text=str(reason).lower()
    if any(t in text for t in ('名额','条数上限','数量上限','达到五条','达到八条','达到5条','达到8条','max_report','quota','hard limit')):
        raise ContractError('QUOTA_OMISSION_FORBIDDEN','不能因数量名额省略必要问题')


def validate_tree(insight, facts, brand_regions):
    nodes=insight.get('nodes',[]);edges=insight.get('edges',[])
    if not nodes:
        if insight.get('supporting_case_ids'):
            raise ContractError('BRANCH_TREE_REQUIRED','多case洞察需要显式nodes/edges')
        return
    by_id={n.get('node_id'):n for n in nodes};root=insight.get('root_node_id')
    if len(by_id)!=len(nodes) or None in by_id or root not in by_id:raise ContractError('TREE_INVALID','节点ID必须唯一且根存在')
    parents={}
    for edge in edges:
        a,b=edge.get('parent_node_id'),edge.get('child_node_id')
        if a not in by_id or b not in by_id or b==root or b in parents:raise ContractError('TREE_INVALID','非根节点必须恰好有一个合法父级')
        parents[b]=a
        pa,ch=by_id[a]['scope'],by_id[b]['scope'];pd,cd=pa['dimension_type'],ch['dimension_type']
        if (pd,cd) not in {('TOTAL','REGION'),('REGION','BRAND'),('BRAND','CARD')}:
            raise ContractError('TREE_SCOPE_INVALID','父子维度层级不合法')
        if pd=='REGION' and (ch.get('region_id')!=pa['dimension_key'] or pa['dimension_key'] not in brand_regions.get(ch['dimension_key'],[])):
            raise ContractError('TREE_SCOPE_INVALID','品牌范围必须继承父区域并满足主数据隶属')
        if pd=='BRAND' and (ch.get('brand_id')!=pa['dimension_key'] or ch.get('region_id')!=pa.get('region_id')):
            raise ContractError('TREE_SCOPE_INVALID','卡级分支必须继承品牌和区域')
    if set(parents)!=set(by_id)-{root}:raise ContractError('TREE_INVALID','树中存在未连接节点')
    for nid,node in by_id.items():
        if node.get('week_id') and insight.get('week_id') and node['week_id']!=insight['week_id']:raise ContractError('TREE_PERIOD_INVALID','分支不得跨报告周拼接')
        seen=set();cur=nid
        while cur!=root:
            if cur in seen:raise ContractError('TREE_CYCLE','树不能有环')
            seen.add(cur);cur=parents[cur]
        if node.get('comparison_basis',insight.get('comparison_basis'))!=insight.get('comparison_basis'):
            raise ContractError('TREE_PERIOD_INVALID','同一贡献树比较基准必须一致；趋势单列主题')
        for fid in node.get('fact_ids',[]):
            if fid not in facts:raise ContractError('TREE_FACT_INVALID','节点只能引用洞察已声明的正式事实')
            scope=node['scope'];fs=facts[fid].get('scope_details',{})
            if scope['dimension_type']=='BRAND' and (fs.get('dimension_key','').split('|')[0]!=scope['dimension_key'] or fs.get('region_id')!=scope.get('region_id')):
                raise ContractError('TREE_FACT_SCOPE_INVALID','品牌分支事实区域/品牌不匹配')
            if facts[fid].get('comparison_basis')!=node.get('comparison_basis',insight.get('comparison_basis')):
                raise ContractError('TREE_PERIOD_INVALID','分支事实比较基准不匹配')


def validate_disclosure(bundle,plan,anomalies):
    insights=bundle.get('insights',[]); summaries=bundle.get('summary_items',[])
    if len(summaries)>5:raise ContractError('SUMMARY_LIMIT','摘要最多五点')
    ids=[require_text(i.get('issue_id'),'issue_id') for i in insights]
    if len(ids)!=len(set(ids)):raise ContractError('ISSUE_DUPLICATE','一个主题只完整展开一次')
    for summary in summaries:
        require_text(summary.get('text'),'summary.text')
        if not summary.get('related_insight_ids') or not set(summary['related_insight_ids']).issubset(ids):raise ContractError('SUMMARY_REFERENCE_INVALID','摘要必须链接已披露主题')
    if len(insights)>8:
        review=bundle.get('deduplication_review',{})
        if review.get('completed') is not True or set(review.get('retained_issue_ids',[]))!=set(ids) or not review.get('reason'):
            raise ContractError('INSIGHT_REVIEW_REQUIRED','超过八条须完成去重与重要性复核，必要主题全部保留')
    claim_owners={}
    claim_content=set()
    for insight in insights:
        if not insight.get('claims'):raise ContractError('CLAIMS_REQUIRED','每个主题至少声明一条结论的归属')
        for claim in insight['claims']:
            cid=require_text(claim.get('claim_id'),'claim_id')
            if cid in claim_owners or claim.get('primary_insight_id')!=insight['issue_id']:
                raise ContractError('CLAIM_DUPLICATE','同一结论只能有一个主主题')
            if claim.get('claim_role') not in {'CONTRIBUTION','TREND','BEHAVIOR','ACTION'}:raise ContractError('CLAIM_ROLE_INVALID','结论类型不受支持')
            require_text(claim.get('statement'),'claim.statement')
            fingerprint=(claim['claim_role'],str(insight.get('primary_scope')),claim['statement'].strip())
            if fingerprint in claim_content:raise ContractError('CLAIM_CONTENT_DUPLICATE','同范围同类型同结论不能换编号重复展开')
            claim_content.add(fingerprint)
            claim_owners[cid]=insight['issue_id']
        if not set(insight.get('related_insight_ids',[])).issubset(ids):raise ContractError('ISSUE_REFERENCE_INVALID','关联主题不存在')
    for m in bundle.get('monitor_items',[]):reject_quota_reason(m.get('reason'))
    quality=bundle.get('data_quality_items',[])
    disclosed=set().union(*(set(i.get('anomaly_group_ids',[])) for i in insights),*(set(i.get('anomaly_group_ids',[])) for i in quality),set())
    required={g['anomaly_group_id'] for g in anomalies.get('anomaly_groups',[]) if g.get('material')}
    if required-disclosed:raise ContractError('MATERIAL_ISSUE_UNDISCLOSED','重大问题必须在正文或明确数据质量提示披露',{'missing':sorted(required-disclosed)})
    for q in quality:
        require_text(q.get('reason'),'data_quality.reason');require_text(q.get('affected_judgments'),'data_quality.affected_judgments')
    return {'summary_count':len(summaries),'insight_count':len(insights)}


def validate_query_scope(query,anchor,brand_regions):
    raw=query.get("scope")
    if isinstance(raw,str):
        parts=raw.split(":",1);raw={"dimension_type":parts[0],"dimension_key":parts[1] if len(parts)>1 else "TOTAL"}
    if not isinstance(raw,dict):raise ContractError("QUERY_SCOPE_INVALID","scope必须是合法范围")
    dim,key=raw.get("dimension_type"),raw.get("dimension_key")
    ad,ak=anchor.get("dimension_type"),anchor.get("dimension_key")
    if ad=="REGION":
        if dim=="BRAND":
            if raw.get("region_id")!=ak or ak not in brand_regions.get(key,[]):raise ContractError("QUERY_SCOPE_INVALID","区域品牌下钻必须继承区域")
        elif not (dim=="REGION" and key==ak) and not (dim in {"REGION_CARD","REGION_MEMBER_TIER"} and str(key).startswith(ak+"|")):
            raise ContractError("QUERY_SCOPE_INVALID","区域调查不得混入其他区域")
    if ad=="BRAND" and (dim!="BRAND" or key!=ak or (anchor.get("region_id") and raw.get("region_id")!=anchor['region_id'])):
        raise ContractError("QUERY_SCOPE_INVALID","品牌调查必须保持锚定品牌与区域")
