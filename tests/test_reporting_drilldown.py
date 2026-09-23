import unittest
from copy import deepcopy
from datetime import date
import pandas as pd
from test_regression import frames, START
from rich_metrics import align_known_mall_names, activity_daily_payload
from crm_weekly_metrics.calculator import Window
from crm_weekly_metrics.coverage import scope_value
from selection import default_plan


class ReportingDrilldown(unittest.TestCase):
    def test_known_alias_aligns_by_region_without_mutating_inputs(self):
        crm,mall,_=frames()
        mall.loc[mall.store_name.str.startswith('LOUIS'),'store_name']='Louis Vuitton'
        original=mall.copy(deep=True)
        fixed,mapping=align_known_mall_names(crm,mall)
        self.assertEqual(len(mapping),2)
        self.assertIn('LOUIS VUITTON 路易威登',set(fixed.store_name))
        pd.testing.assert_frame_equal(mall,original)
        self.assertEqual(fixed.mall_sales.sum(),mall.mall_sales.sum())

    def test_ambiguous_alias_and_cafe_not_merged(self):
        crm,mall,_=frames()
        extra=mall.iloc[[0]].copy();extra.store_name='Louis Vuitton'
        cafe=mall.iloc[[0]].copy();cafe.store_name='Le Café Louis Vuitton'
        mall=pd.concat([mall,extra,cafe],ignore_index=True)
        fixed,mapping=align_known_mall_names(crm,mall)
        self.assertFalse(mapping)
        self.assertIn('Le Café Louis Vuitton',set(fixed.store_name))

    def test_global_card_does_not_require_transaction_in_each_region(self):
        crm,_,_=frames()
        crm=crm[(crm.event_time==START)&~((crm.district=='W')&(crm.card_level=='星钻卡'))]
        window=Window(START,START.replace(day=30))
        expected=crm.loc[crm.card_level=='星钻卡','paid_amount'].sum()
        self.assertEqual(scope_value(crm,'event_time','paid_amount',window,card='星钻卡'),expected)
        self.assertIsNone(scope_value(crm[crm.district!='W'],'event_time','paid_amount',window,card='星钻卡'))
        self.assertIsNone(scope_value(crm[crm.card_level!='星钻卡'],'event_time','paid_amount',window,card='星钻卡'))
        self.assertIsNone(scope_value(crm,'event_time','paid_amount',window,region='W',card='星钻卡'))

    def test_total_default_plan_includes_five_cards_and_two_decompositions(self):
        a=dict(schema_version='3.0',week_id='w',input_signature='s',implementation_version='3.2.2',issues=[dict(issue_id='i',anomaly_group_ids=['g'],primary_scope={'dimension_type':'TOTAL','dimension_key':'TOTAL'},issue_type='INDEPENDENT_RISK',independent_issue_reason='material')])
        queries=default_plan(a,None)['selected_cases'][0]['queries']
        self.assertEqual(len([q for q in queries if q['scope']['dimension_type']=='CARD']),5)
        self.assertEqual({q.get('next_dimension') for q in queries if q['focus']=='CONTRIBUTION'},{'CARD','REGION'})

if __name__=='__main__':unittest.main()
