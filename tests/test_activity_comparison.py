import sys
import unittest
from pathlib import Path
from copy import deepcopy
from datetime import date, timedelta

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'.codebuddy/skills/crm-data-investigation/runtime'))
sys.path.insert(0, str(ROOT/'.codebuddy/skills/crm-weekly-report/scripts'))
from data_investigation.engine import InvestigationEngine
from validate_insights import validate_activity_assessment, _validate_narrative_numbers, _official_number_tokens
from common import ContractError

START=date(2026,7,6)


def fixture(active_days=(0,1,2), multiplier=2, other_multiplier=1):
    daily=[];mall=[];coverage={}
    for week in range(5):
        start=START-timedelta(weeks=week)
        coverage[start.isoformat()]={'N':'COMPLETE'}
        for d in range(7):
            day=start+timedelta(days=d)
            base=400 if d>=5 else 100
            factor=(multiplier if d in active_days else other_multiplier) if week==0 else 1
            for member in range(2):
                daily.append(dict(date=str(day),region='N',brand='LV',member_id=f'm{member}',card_tier='金卡',crm_sales=base*factor/2,transactions=1,refund_amount=0))
            mall.append(dict(date=str(day),region='N',brand='LV',mall_sales=base*2*(1.5 if week==0 and d in active_days else 1)))
    bundle=dict(schema_version='3.0',input_signature='activity-fixture',implementation_version='3.2.2',
                report={'current':[str(START),str(START+timedelta(days=6))]},
                brand_regions={'LV':['N']},coverage_manifest={'crm':coverage},
                crm={'member_day_facts':daily,'member_day_coverage':{'available':True,'start_date':str(START-timedelta(weeks=4)),'end_date':str(START+timedelta(days=6))}},
                mall={'day_facts':mall})
    q=dict(query_id='q',insight_case_id='c',anomaly_group_id='a',focus='MEMBER_BEHAVIOR',comparison_basis='WOW',
           scope={'dimension_type':'BRAND','dimension_key':'LV','region_id':'N'},analysis_items=['ACTIVITY_RESPONSE'],
           filters={'activity_id':'ACT-LV','activity_window':{'start_date':str(START+timedelta(days=min(active_days))),'end_date':str(START+timedelta(days=max(active_days)))},
                    'baseline_weeks':4,'baseline_event_check':'VERIFIED','baseline_context_sources':['provided complete activity calendar']})
    return bundle,q


def run(bundle,q):
    return InvestigationEngine(bundle).investigate(q)['results'][0]


class ActivityComparison(unittest.TestCase):
    def test_weekday_windows_member_dedup_and_mall(self):
        b,q=fixture();r=run(b,q);self.assertEqual(r['status'],'OK')
        f=r['facts'][0]; x=f['extra']
        self.assertEqual(f['current'],600)
        self.assertEqual(f['baseline'],300)
        self.assertEqual(x['activity']['active_members'],2)
        self.assertEqual(x['activity']['transactions'],6)
        self.assertEqual(x['non_activity']['sales'],1000)
        self.assertAlmostEqual(x['activity_week_share'],600/1600)
        self.assertEqual(x['operating']['current_mall'],900)
        self.assertEqual(x['baseline_count'],4)
        self.assertFalse(f['audit_record_ids'])

    def test_weekend_uses_weekends_not_prior_weekdays(self):
        b,q=fixture((5,6),1);f=run(b,q)['facts'][0]
        self.assertEqual(f['current'],800)
        self.assertEqual(f['baseline'],800)
        self.assertEqual(f['change'],0)

    def test_share_increases_without_activity_growth(self):
        b,q=fixture(multiplier=1,other_multiplier=.5);f=run(b,q)['facts'][0];x=f['extra']
        self.assertEqual(f['change'],0)
        self.assertGreater(x['activity_week_share'],x['baseline_week_share'])

    def test_known_activity_or_holiday_excludes_whole_baseline_week(self):
        b,q=fixture();q['filters']['excluded_windows']=[{'start_date':'2026-06-27','end_date':'2026-06-28','reason':'other promotion'}]
        f=run(b,q)['facts'][0]
        self.assertEqual(f['extra']['baseline_count'],3)
        self.assertEqual(f['extra']['baseline_windows'][1]['status'],'EXCLUDED_EVENT_WINDOW')

    def test_missing_unknown_day_not_zero_but_declared_complete_zero(self):
        b,q=fixture();b['crm']['member_day_facts']=[r for r in b['crm']['member_day_facts'] if r['date']!='2026-07-07']
        self.assertEqual(run(b,q)['facts'][0]['current'],400)
        b['coverage_manifest']={}
        self.assertFalse(run(b,q)['facts'])

    def test_partial_region_rejects_even_with_observed_rows(self):
        b,q=fixture();b['coverage_manifest']['crm'][str(START)]['N']='PARTIAL'
        self.assertFalse(run(b,q)['facts'])

    def test_future_rows_excluded_from_post_and_future_activity_rejected(self):
        b,q=fixture();b['crm']['member_day_facts'].append(dict(b['crm']['member_day_facts'][0],date='2026-07-13',crm_sales=999999))
        b['crm']['member_day_coverage']['end_date']='2026-07-31'
        q['filters']['post_window']={'start_date':'2026-07-09','end_date':'2026-07-20'}
        f=run(b,q)['facts'][0];self.assertEqual(f['extra']['post']['sales'],1000)
        q['filters']['activity_window']={'start_date':'2026-07-17','end_date':'2026-07-19'}
        self.assertFalse(run(b,q)['facts'])

    def test_missing_mall_cannot_use_partial_denominator(self):
        b,q=fixture();b['mall']['day_facts'][0]['mall_sales']=None
        self.assertFalse(run(b,q)['facts'][0]['extra']['operating']['available'])

    def test_cross_week_activity_clips_and_omits_its_own_baseline(self):
        b,q=fixture();q['filters']['activity_window']={'start_date':'2026-06-29','end_date':'2026-07-08'}
        f=run(b,q)['facts'][0]
        self.assertEqual(f['extra']['activity_window'],['2026-07-06','2026-07-08'])
        self.assertEqual(f['extra']['baseline_count'],3)

    def test_unequal_weekday_custom_baseline_rejected(self):
        b,q=fixture();q['filters']['baseline_window']={'start_date':'2026-07-03','end_date':'2026-07-05'}
        self.assertEqual(run(b,q)['status'],'INVALID_QUERY')

    def test_supported_claim_distinguishes_consumption_and_points(self):
        b,q=fixture();f=run(b,q)['facts'][0]
        insight={'confidence':'MEDIUM','fact_ids':[f['fact_id']],'activity_ids':['ACT-LV'],
                 'activity_assessment':{'strength':'SUPPORTED','outcome':'BOTH','fact_ids':[f['fact_id']],'confound_review':'活动日历、促销及客群结构已复核'}}
        validate_activity_assessment(insight,[f])
        missing=deepcopy(f);missing['extra']['operating']['available']=False
        with self.assertRaises(ContractError):validate_activity_assessment(insight,[missing])
        unknown=deepcopy(f);unknown['extra']['baseline_event_check']='UNVERIFIED'
        with self.assertRaises(ContractError):validate_activity_assessment(insight,[unknown])
        other=deepcopy(f);other['extra']['activity_id']='ACT-OTHER'
        with self.assertRaises(ContractError):validate_activity_assessment(insight,[other])

    def test_new_narrative_field_still_checks_numbers(self):
        _validate_narrative_numbers({'narrative':'增长+10.00万'},{'+10.00万'},'test')
        with self.assertRaises(ContractError):
            _validate_narrative_numbers({'narrative':'增长+99.00万'},{'+10.00万'},'test')
        _validate_narrative_numbers({'narrative':'销售下降24.5%'},{'-24.5%'},'test')
        with self.assertRaises(ContractError):
            _validate_narrative_numbers({'narrative':'销售增长24.5%'},{'-24.5%'},'test')
        with self.assertRaises(ContractError):
            validate_activity_assessment({'narrative':'双倍积分活动带动了消费增长'},[])

    def test_association_label_cannot_bypass_explicit_attribution(self):
        with self.assertRaises(ContractError):
            validate_activity_assessment({'narrative':'双倍积分活动带动了消费增长', 'activity_assessment':{'strength':'ASSOCIATED'}}, [])
        validate_activity_assessment({'narrative':'双倍积分活动可能带动消费增长', 'activity_assessment':{'strength':'ASSOCIATED'}}, [])

    def test_count_unit_is_derived_from_metric_not_arbitrary_number(self):
        tokens = _official_number_tokens({'facts':[{'metric_id':'ACTIVE_MEMBERS','display_fields':{'current':'98'}}, {'metric_id':'CRM_SALES','display_fields':{'current':'304.94万'}}]}, [])
        _validate_narrative_numbers({'narrative':'消费会员98人'}, tokens, 'test')
        with self.assertRaises(ContractError):
            _validate_narrative_numbers({'narrative':'消费98笔'}, tokens, 'test')


if __name__=='__main__':unittest.main()
