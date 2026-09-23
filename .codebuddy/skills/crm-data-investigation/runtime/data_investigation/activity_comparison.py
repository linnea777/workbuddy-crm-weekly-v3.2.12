"""Same-weekday activity comparisons. Observed differences are not causal lift."""
from collections import defaultdict
from datetime import date, timedelta
from statistics import median


def dates(bounds):
    return [bounds[0] + timedelta(days=i) for i in range((bounds[1] - bounds[0]).days + 1)]


def activity_response(engine, query, scope, crm):
    from .engine import InvestigationError
    filters = query.get('filters') or {}
    if not filters.get('activity_window'):
        return None, [], 'ACTIVITY_RESPONSE需要filters.activity_window'
    original = engine._date_window(filters['activity_window'], 'activity_window')
    week = tuple(date.fromisoformat(x) for x in engine.bundle['report']['current'])
    active = (max(original[0], week[0]), min(original[1], week[1]))
    if active[0] > active[1]:
        return None, [], '活动未发生在报告周内；不得使用未来销售证明本周归因'
    count = filters.get('baseline_weeks', 4)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 8:
        raise InvestigationError('baseline_weeks必须为1至8的整数')
    excluded = [engine._date_window(x, 'excluded_windows') for x in filters.get('excluded_windows', [])]
    excluded.append(original)
    if 'baseline_window' in filters:
        b = engine._date_window(filters['baseline_window'], 'baseline_window')
        if (b[1]-b[0]) != (active[1]-active[0]) or (active[0]-b[0]).days % 7 or b[1] >= week[0]:
            raise InvestigationError('baseline_window须为报告周之前、等长且相同星期的窗口')
        candidate_weeks = [week[0] - (active[0]-b[0])]
    else:
        candidate_weeks = [week[0]-timedelta(weeks=i) for i in range(1, count+1)]
    coverage = crm.get('member_day_coverage', {})
    if coverage.get('available') is not True:
        return None, [], '会员自然日聚合不可用'
    coverage_start = date.fromisoformat(coverage['start_date'])
    coverage_end = min(date.fromisoformat(coverage['end_date']), week[1])
    all_rows = engine._scope_day_rows(crm.get('member_day_facts', []), scope, (coverage_start, coverage_end))
    indexed = defaultdict(list)
    for row in all_rows:
        indexed[date.fromisoformat(row['date'])].append(row)
    if scope.region_id:
        regions = [scope.region_id]
    elif scope.dimension_type == 'REGION':
        regions = [scope.dimension_key]
    elif scope.dimension_type == 'REGION_CARD':
        regions = [scope.dimension_key.split('|')[0]]
    elif scope.dimension_type in {'BRAND', 'STORE'}:
        regions = engine.bundle.get('brand_regions', {}).get(scope.dimension_key, [])
    else:
        regions = ['N', 'S', 'W']
    manifest = engine.bundle.get('coverage_manifest', {}).get('crm', {})

    def summary(selected_dates):
        if not selected_dates:
            return None
        rows = [r for d in selected_dates for r in indexed.get(d, [])]
        for d in selected_dates:
            if d < coverage_start or d > coverage_end:
                return None
            declarations = manifest.get((d-timedelta(days=d.weekday())).isoformat(), {})
            for region in regions:
                status = declarations.get(region)
                if status in {'MISSING', 'PARTIAL'}:
                    return None
                if status != 'COMPLETE' and not any(r.get('region') == region for r in indexed.get(d, [])):
                    return None
        if not regions:
            return None
        sales = sum(float(r.get('crm_sales') or 0) for r in rows)
        positive = [r for r in rows if int(r.get('transactions') or 0) > 0]
        members = len({r['member_id'] for r in positive})
        transactions = sum(int(r.get('transactions') or 0) for r in rows)
        by_member = defaultdict(float)
        for r in rows:
            by_member[r['member_id']] += float(r.get('crm_sales') or 0)
        return {'sales': sales, 'active_members': members, 'transactions': transactions,
                'frequency': transactions/members if members else None,
                'top_member_sales': max([0, *by_member.values()]),
                'top_member_share': max([0, *by_member.values()])/sales if sales > 0 else None,
                'refund_amount': sum(float(r.get('refund_amount') or 0) for r in rows)}

    active_days = dates(active)
    week_days = dates(week)
    non_days = [d for d in week_days if d not in active_days]
    current = summary(active_days)
    if current is None:
        return None, [], '活动期间存在未确认完整的缺失日期或区域，不补零'
    whole = summary(week_days)
    non = summary(non_days)
    candidates, accepted = [], []
    for start in candidate_weeks:
        shift = week[0]-start
        a = [d-shift for d in active_days]
        n = [d-shift for d in non_days]
        w = [d-shift for d in week_days]
        item = {'week_start': start.isoformat(), 'activity_dates': [d.isoformat() for d in a],
                'non_activity_dates': [d.isoformat() for d in n]}
        # A polluted non-activity period would also distort within-week concentration.
        if any(any(lo <= d <= hi for lo, hi in excluded) for d in w):
            item['status'] = 'EXCLUDED_EVENT_WINDOW'
        else:
            item.update(activity=summary(a), non_activity=summary(n), week=summary(w))
            item['status'] = 'AVAILABLE' if item['activity'] is not None else 'DATA_UNAVAILABLE'
            if item['status'] == 'AVAILABLE':
                accepted.append(item)
        candidates.append(item)

    def baseline(section, field):
        values = [x[section][field] for x in accepted if x.get(section) is not None and x[section].get(field) is not None]
        return median(values) if values else None

    base = {field: baseline('activity', field) for field in current}
    non_base = {field: baseline('non_activity', field) for field in current}
    share = current['sales']/whole['sales'] if whole and whole['sales'] > 0 else None
    historic_shares = [x['activity']['sales']/x['week']['sales'] for x in accepted if x.get('week') and x['week']['sales'] > 0]
    base_share = median(historic_shares) if historic_shares else None
    limitations = []
    if not accepted:
        limitations.append('没有可用的同星期历史基准，不能判断活动期是否异常增长')
    if filters.get('baseline_event_check') != 'VERIFIED' or not filters.get('baseline_context_sources'):
        limitations.append('历史窗口是否无活动尚未核实；默认历史对比不等于无活动对照')
    if not manifest:
        limitations.append('来源完整性未声明，现有结果为观察值')
    if whole is None:
        limitations.append('整周覆盖不足，活动期占全周比例不可用')
    if non_days and non is None:
        limitations.append('非活动期间数据不足')
    if not non_days:
        limitations.append('活动覆盖整周，没有当周非活动期对照')
    limitations.append('历史差额不是活动净增量；需结合其他促销、节假日、大额消费及消费前移判断')
    if current['refund_amount'] < 0 or (whole and whole['refund_amount'] < 0):
        limitations.append('销售按净额计算，退款会影响活动期占比')

    # Mall uses exact matching stores over all compared dates. No model alias guesses.
    mall_rows = engine._scope_day_rows(engine.bundle.get('mall', {}).get('day_facts', []), scope, (coverage_start, coverage_end))
    mall_index = {(r['date'], r['region'], r['brand']): r.get('mall_sales') for r in mall_rows}
    required_days = active_days + [date.fromisoformat(d) for x in accepted for d in x['activity_dates']]
    stores = {(r['region'], r['brand']) for d in required_days for r in indexed.get(d, [])}
    mall_available = scope.dimension_type in {'TOTAL', 'REGION', 'BRAND', 'STORE'} and bool(stores) and bool(accepted)
    mall_available = mall_available and all(mall_index.get((d.isoformat(), r, b)) is not None for d in required_days for r, b in stores)
    operating = {'available': bool(mall_available), 'matching': 'EXACT_COMMON_STORES', 'store_count': len(stores)}
    if mall_available:
        def mall_sum(ds):
            return sum(float(mall_index[(d.isoformat(), r, b)]) for d in ds for r, b in stores)
        mall_current = mall_sum(active_days)
        mall_history = [mall_sum([date.fromisoformat(d) for d in x['activity_dates']]) for x in accepted]
        ratios = [x['activity']['sales']/m for x, m in zip(accepted, mall_history) if m > 0]
        operating.update(current_mall=mall_current, baseline_mall=median(mall_history),
                         current_capture=current['sales']/mall_current if mall_current > 0 else None,
                         baseline_capture=median(ratios) if len(ratios) == len(accepted) else None)
    else:
        limitations.append('缺少严格匹配的日级Mall对照，不能仅凭CRM增量同时认定实际消费与积分参与均增长')
    post_days = [d for d in week_days if d > active[1]]
    if 'post_window' in filters:
        requested_post = engine._date_window(filters['post_window'], 'post_window')
        post_days = [d for d in dates(requested_post) if active[1] < d <= week[1]]
    post = summary(post_days)
    if not post_days:
        limitations.append('截至报告周末尚无活动后观察期')
    extra = {'activity_id': filters.get('activity_id'), 'original_activity_window': [d.isoformat() for d in original],
             'activity_window': [d.isoformat() for d in active], 'baseline_method': 'MEDIAN_SAME_WEEKDAYS',
             'baseline_weeks_requested': count, 'baseline_windows': candidates, 'baseline_count': len(accepted),
             'baseline_event_check': filters.get('baseline_event_check', 'UNVERIFIED'),
             'baseline_context_sources': filters.get('baseline_context_sources', []),
             'activity': current, 'baseline': base, 'week': whole, 'non_activity': non, 'non_activity_baseline': non_base,
             'activity_week_share': share, 'baseline_week_share': base_share,
             'operating': operating, 'post': post, 'as_of_date': week[1].isoformat()}
    fact = engine._fact(metric_id='ACTIVITY_RESPONSE', scope=scope, current=current['sales'], previous=base['sales'],
                        source='crm.member_day_facts;mall.day_facts', comparison_basis='ACTIVITY_WINDOW', extra=extra)
    display = fact['display_fields']
    amount = lambda v: engine._format_amount(v) if v is not None else '数据不可用'
    number = lambda v, unit: f'{v:g}{unit}' if v is not None else '数据不可用'
    pct = lambda v: f'{v*100:.1f}%' if v is not None else '数据不可用'
    for prefix, value in [('activity', current), ('baseline', base), ('non_activity', non), ('non_activity_baseline', non_base), ('post', post)]:
        v = value or {}
        display.update({prefix+'_sales': amount(v.get('sales')), prefix+'_active_members': number(v.get('active_members'), '人'),
                        prefix+'_transactions': number(v.get('transactions'), '笔'),
                        prefix+'_frequency': f"{v['frequency']:.2f}" if v.get('frequency') is not None else '数据不可用'})
    display.update(activity_window=f'{active[0]}至{active[1]}', baseline_count=f'{len(accepted)}个',
                   activity_week_share=pct(share), baseline_week_share=pct(base_share),
                   top_member_sales=amount(current['top_member_sales']), top_member_share=pct(current['top_member_share']),
                   week_sales=amount(whole['sales'] if whole else None))
    for key in ['current_mall', 'baseline_mall']:
        display[key] = amount(operating.get(key))
    for key in ['current_capture', 'baseline_capture']:
        display[key] = pct(operating.get(key))
    display['baseline_series'] = [
        {'week_start': x['week_start'], 'status': x['status'],
         'activity_sales': amount((x.get('activity') or {}).get('sales')),
         'activity_members': number((x.get('activity') or {}).get('active_members'), '人'),
         'non_activity_sales': amount((x.get('non_activity') or {}).get('sales'))}
        for x in candidates]
    fact['activity_context'] = {'activity_id': filters.get('activity_id'),
                                'baseline_method': 'MEDIAN_SAME_WEEKDAYS',
                                'baseline_event_check': filters.get('baseline_event_check', 'UNVERIFIED'),
                                'baseline_context_sources': filters.get('baseline_context_sources', []),
                                'as_of_date': week[1].isoformat()}
    fact['applicability_limits'] = limitations
    fact['audit_record_ids'] = []
    return fact, [], None if accepted else '同星期历史基准不可用'
