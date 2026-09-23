import unittest
from datetime import date
import pandas as pd
from test_regression import frames, artifacts, START
from crm_weekly_metrics.coverage import scope_value
from crm_weekly_metrics.calculator import Window
from data_investigation.engine import InvestigationEngine
from validate_insights import _validate_total_region_disclosure
from common import ContractError


class ObservedValues(unittest.TestCase):
    def test_total_card_only_narrative_cannot_omit_available_regions(self):
        ledger = {'facts': [dict(fact_id=k,scope='REGION:'+k,metric_id='CRM_SALES',display_fields={'impact_amount':v}) for k,v in [('N','+10.00万'),('S','-2.00万'),('W','-1.00万')]]}
        insight = {'primary_scope':{'dimension_type':'TOTAL'},'narrative':'全场增长主要来自黑钻卡。','fact_ids':['N','S','W']}
        with self.assertRaises(ContractError):
            _validate_total_region_disclosure(insight, ledger)
        insight['narrative'] = '北区增加10.00万，南区减少2.00万、西区减少1.00万，北区是主要增量来源。'
        _validate_total_region_disclosure(insight, ledger)
        insight['fact_ids'] = ['N','S']
        with self.assertRaises(ContractError):
            _validate_total_region_disclosure(insight, ledger)

    def test_ytd_numeric_without_completeness_manifest(self):
        c, m, _ = frames()
        metrics, _, _ = artifacts(c, m, {})
        year = c[c.event_time.between(date(START.year, 1, 1), START)]
        total = next(r for r in metrics['metrics']['M03'] if r['dimension'] == 'Total')
        self.assertAlmostEqual(total['current'], year.paid_amount.sum())

    def test_mixed_missing_mall_preserves_sum_and_all_missing_does_not_become_zero(self):
        _, m, _ = frames()
        m = m[(m.business_date == START) & (m.district == 'N')].copy()
        m.iloc[0, m.columns.get_loc('mall_sales')] = float('nan')
        w = Window(START, START)
        self.assertAlmostEqual(scope_value(m,'business_date','mall_sales',w,source='mall',region='N'), m.mall_sales.sum())
        self.assertIsNone(scope_value(m,'business_date','mall_sales',w,source='mall',region='N',require_complete=True))
        m.mall_sales = float('nan')
        self.assertIsNone(scope_value(m,'business_date','mall_sales',w,source='mall',region='N'))

    def test_partial_declaration_keeps_observed_value_missing_declaration_blocks(self):
        c, _, _ = frames()
        w = Window(START, START)
        cov = {'crm': {START.isoformat(): {'N': 'PARTIAL'}}}
        expected = c[(c.event_time == START) & (c.district == 'N')].paid_amount.sum()
        self.assertEqual(scope_value(c,'event_time','paid_amount',w,region='N',coverage=cov), expected)
        cov['crm'][START.isoformat()]['N'] = 'MISSING'
        self.assertIsNone(scope_value(c,'event_time','paid_amount',w,region='N',coverage=cov))

    def test_regional_capture_allows_mall_only_shop_and_matches_investigation(self):
        c, m, _ = frames()
        extra = m[(m.business_date == START) & (m.district == 'N')].iloc[[0]].copy()
        extra.store_name = 'Mall-only shop'
        extra.mall_sales = 12345.0
        m = pd.concat([m, extra], ignore_index=True)
        metrics, rich, _ = artifacts(c, m, {})
        expected = c[(c.event_time == START) & (c.district == 'N')].paid_amount.sum() / m[(m.business_date == START) & (m.district == 'N')].mall_sales.sum()
        row = next(r for r in metrics['metrics']['M13'] if r['dimension'] == 'N')
        self.assertAlmostEqual(row['current'], expected)
        result = InvestigationEngine(rich).investigate(dict(query_id='op',anomaly_group_id='g',scope={'dimension_type':'REGION','dimension_key':'N'},focus='BRAND_OPERATION',analysis_items=['OPERATING_STATUS']))['results'][0]
        f = next(f for f in result['facts'] if f['metric_id']=='CAPTURE_RATIO')
        self.assertAlmostEqual(f['values']['current'], expected, places=5)
        self.assertEqual(f['coverage_status'], 'OBSERVED_AGGREGATE')

    def test_zero_mall_denominator_stays_unavailable(self):
        c, m, cov = frames()
        m.loc[(m.business_date == START) & (m.district == 'N'), 'mall_sales'] = 0
        metrics, _, _ = artifacts(c, m, cov)
        self.assertIsNone(next(r for r in metrics['metrics']['M13'] if r['dimension']=='N')['current'])


if __name__ == '__main__':
    unittest.main()
