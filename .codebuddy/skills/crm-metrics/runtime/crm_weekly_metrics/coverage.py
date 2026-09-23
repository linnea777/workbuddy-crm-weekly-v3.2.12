"""Observed amounts stay numeric; completeness is separate from calculability."""
from datetime import timedelta
import pandas as pd
from .calculator import CARD_KEYS, DISTRICTS, Window, _change, _week_id, _slice, _number


def scope_value(frame, date_column, amount_column, window, *, source='crm', region=None, brand=None, card=None, coverage=None, require_complete=False):
    coverage = coverage if coverage is not None else frame.attrs.get('coverage', {})
    data = _slice(frame, date_column, window)
    regions = [region] if region else list(DISTRICTS)
    if brand:
        regions = [region] if region else sorted(set(frame.loc[frame.store_name.eq(brand), 'district']))
        if not regions:
            return None
    total = 0.0
    selected_observed = False
    all_complete = True
    for reg in regions:
        regional = data.loc[data.district.eq(reg)]
        declarations = coverage.get(source, {})
        # Multiweek totals require each intersecting calendar week, never a single start flag.
        monday = window.start - timedelta(days=window.start.weekday())
        states = []
        while monday <= window.end:
            states.append(declarations.get(monday.isoformat(), {}).get(reg))
            monday += timedelta(days=7)
        if 'MISSING' in states or (require_complete and 'PARTIAL' in states):
            return None
        complete = bool(states) and all(state == 'COMPLETE' for state in states)
        all_complete &= complete
        if regional.empty and not complete:
            return None
        selected = regional
        if brand is not None:
            selected = selected.loc[selected.store_name.eq(brand)]
        if card is not None:
            selected = selected.loc[selected.card_level.eq(card)]
        if selected.empty:
            # A global card total sums observed transactions across covered regions;
            # the card need not transact in every region. Entirely absent categories
            # and missing regional populations still remain unknown without coverage.
            if not complete and not (source == 'crm' and card is not None and region is None):
                return None
        elif selected[amount_column].notna().sum() == 0:
            return None
        elif require_complete and selected[amount_column].isna().any():
            return None
        else:
            selected_observed = True
        total += float(selected[amount_column].sum())
    if not selected_observed and not all_complete:
        return None
    return _number(total)


def apply_contract(crm, mall, windows, metrics, records, coverage, quality):
    crm.attrs['coverage'] = coverage
    mall.attrs['coverage'] = coverage
    current = windows['current']
    # Index once; each scope is grouped across calendar weeks, preserving holes.
    weekly = {}
    for source, frame, date_col, amount_col in [('crm',crm,'event_time','paid_amount'), ('mall',mall,'business_date','mall_sales')]:
        copied=frame.copy()
        copied['_week']=copied[date_col].map(lambda d: d-timedelta(days=d.weekday()))
        weekly[source] = {w: g.drop(columns='_week') for w,g in copied.groupby('_week',sort=False)}
    def value(source, window, region=None, brand=None, card=None):
        frame, col, amount = (crm,'event_time','paid_amount') if source=='crm' else (mall,'business_date','mall_sales')
        if (window.end-window.start).days==6 and window.start.weekday()==0:
            part=weekly[source].get(window.start,frame.iloc[:0])
            # Brand's expected regions come from full history, not only this week's rows.
            if brand and not region:
                regs=sorted(set(crm.loc[crm.store_name.eq(brand),'district']))
                vals=[scope_value(part,col,amount,window,source=source,region=r,brand=brand,card=card,coverage=coverage) for r in regs]
                return None if not vals or any(v is None for v in vals) else sum(vals)
            return scope_value(part,col,amount,window,source=source,region=region,brand=brand,card=card,coverage=coverage)
        return scope_value(frame,col,amount,window,source=source,region=region,brand=brand,card=card,coverage=coverage)
    for period in ('current','previous','yoy'):
        for region in DISTRICTS:
            v=value('crm',windows[period],region)
            if v is None:
                quality.append({'status_code':'CRM_COVERAGE_UNKNOWN','level':'ERROR','scope':'CRM','window':period,'dimension':region,'message':'区域缺报、部分数据或覆盖未知；相关比较为N/A，未补零'})
    quality.append({'status_code':'REGISTRATION_SOURCE_LIMIT','level':'WARNING','scope':'M06','message':'仅有交易文件；M06为当周新注册且消费会员，不能代表全部新注册。新注册异常规则跳过，待提供会员主表。'})
    if not coverage:
        quality.append({'status_code':'COVERAGE_UNVERIFIED','level':'WARNING','scope':'ALL','message':'按所提供文件的有效金额计算周值和累计值；完整性声明为可选元数据，不作为有值指标的显示门槛。未知金额未补零。'})
    for period, window in windows.items():
        part = _slice(mall, 'business_date', window)
        for region, rows in part.groupby('district'):
            missing = int(rows.mall_sales.isna().sum())
            if missing:
                quality.append({'status_code':'MALL_OBSERVED_SUM','level':'WARNING','scope':'M14','window':period,'dimension':region,'missing_cells':missing,'message':'Mall按有效金额合计，缺值单列；相关比例为报表口径观察值，不宣称缺值已补齐。'})
    # Base tables use the same values as detection.
    for mid, base in [('M01','previous'),('M02','yoy'),('M03','previous_ytd')]:
        now='ytd' if mid=='M03' else 'current'
        for row in metrics[mid]:
            region=None if row['dimension']=='Total' else row['dimension']
            row.update(_change(value('crm',windows[now],region),value('crm',windows[base],region)))
    for row in metrics['M04']:
        card=None if row['card_level']=='Total' else row['card_level']
        row.update(_change(value('crm',current,card=card),value('crm',windows['previous'],card=card)))
    for row in metrics['M13']:
        region=None if row['dimension']=='Total' else row['dimension']
        c,p=value('crm',current,region),value('crm',windows['previous'],region)
        m,n=value('mall',current,region),value('mall',windows['previous'],region)
        # Regional capture uses regional totals. Common-store comparison is separate.
        ratio=lambda a,b: None if a is None or b in (None,0) else a/b
        row.update(current=ratio(c,m),base=ratio(p,n),current_mall_sales=m,previous_mall_sales=n)
        row['change_pp']=None if row['current'] is None or row['base'] is None else float(_change(row['current'],row['base'])['change_amount'])*100
        row['change_rate_percent']=_change(row['current'],row['base'])['change_rate_percent']
        row['coverage_status']='OBSERVED_AGGREGATE'
    for row in metrics['M14']:
        region=None if row['dimension']=='Total' else row['dimension']
        for key,period in [('current','current'),('previous','previous'),('yoy_base','yoy')]:row[key]=value('mall',windows[period],region)
        for prefix,base in [('wow','previous'),('yoy','yoy_base')]:
            change=_change(row['current'],row[base]);row[prefix+'_change_amount']=change['change_amount'];row[prefix+'_change_rate_percent']=change['change_rate_percent']
    existing={(r['dimension_type'],r['dimension_key'],r['metric_id']) for r in records}
    for card,key in CARD_KEYS.items():
        if ('MEMBER_TIER',key,'CRM_SALES') not in existing:
            records.append(dict(week_id=_week_id(current.start),dimension_type='MEMBER_TIER',dimension_key=key,metric_id='CRM_SALES',source_record_id=f'card:{key}',attribution_context={}))
    scoped_records=[]
    for brand, frame in crm.groupby('store_name'):
        regs=sorted(set(frame.district))
        if len(regs)>1:
            original=next(r for r in records if r['dimension_type']=='BRAND' and r['dimension_key']==brand)
            for reg in regs:
                scoped_records.append(dict(original, region_id=reg, source_record_id=original['source_record_id']+':'+reg))
    records.extend(scoped_records)
    for rec in records:
        dim,key,metric=rec['dimension_type'],rec['dimension_key'],rec['metric_id']
        region = key if dim=='REGION' else key.split('|')[0] if dim=='REGION_MEMBER_TIER' else None
        region = rec.get("region_id") or region
        card = next((c for c,k in CARD_KEYS.items() if k==(key.split('|')[-1])),None) if dim in ('MEMBER_TIER','REGION_MEMBER_TIER') else None
        brand = key if dim=='BRAND' else None
        if metric=='NEW_REGISTRATIONS':
            rec.update(current_value=None,previous_value=None,wow_rate=None,history=[])
            continue
        def scoped(win):return value('crm',win,region,brand,card)
        if metric=='CRM_SALES':
            now,before=scoped(current),scoped(windows['previous'])
            rec.update(current_value=now,previous_value=before,wow_rate=None if _change(now,before)['change_rate_percent'] is None else _change(now,before)['change_rate_percent']/100)
            for rate_field,base_field,period in [('yoy_rate','yoy_reference_value','yoy'),('ytd_yoy_rate','ytd_reference_value','previous_ytd')]:
                a=scoped(windows['ytd']) if period=='previous_ytd' else now;b=scoped(windows[period]);ch=_change(a,b)
                rec[base_field]=b;rec[rate_field]=None if ch['change_rate_percent'] is None else ch['change_rate_percent']/100
                if period=='previous_ytd':rec['ytd_current_value']=a
            rec['history']=[{'week_id':_week_id(current.start-timedelta(weeks=i)),'value':scoped(Window(current.start-timedelta(weeks=i),current.end-timedelta(weeks=i)))} for i in range(27,0,-1)]
        elif metric=='CAPTURE_RATIO':
            row=next(r for r in metrics['M13'] if r['dimension']==('Total' if dim=='TOTAL' else key))
            rec.update(current_value=row['current'],previous_value=row['base'])
            hist=[]
            for i in range(27,0,-1):
                w=Window(current.start-timedelta(weeks=i),current.end-timedelta(weeks=i));a=scoped(w);b=value('mall',w,region)
                hist.append({'week_id':_week_id(w.start),'value':None if a is None or b in (None,0) else a/b})
            rec['history']=hist
        elif scoped(current) is None or scoped(windows['previous']) is None:
            rec.update(current_value=None,previous_value=None,wow_rate=None)
        rec['coverage_status']='OBSERVED' if rec.get('current_value') is not None else 'UNKNOWN'
    return records
