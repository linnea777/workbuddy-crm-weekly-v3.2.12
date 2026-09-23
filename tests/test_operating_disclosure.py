import unittest
from copy import deepcopy
from test_regression import ContractError, default_plan
from validate_insights import _validate_operating_disclosure, _validate_unreported_region_operations


class OperatingDisclosure(unittest.TestCase):
    def fixture(self):
        facts=[dict(fact_id=m,scope='REGION:S',metric_id=m,comparison_basis='WOW',display_fields={'current':'10','baseline':'20'}) for m in ['CRM_SALES','MALL_SALES','CAPTURE_RATIO']]
        statement='经营销售与积分记录占比均下降，共同体现为CRM回落。'
        i=dict(primary_scope={'dimension_type':'REGION','dimension_key':'S'},comparison_basis='WOW',fact_ids=[f['fact_id'] for f in facts],narrative='Mall销售与Capture Ratio下降。'+statement,operating_assessment={'statement':statement,'fact_ids':[f['fact_id'] for f in facts]})
        return i,{'facts':facts}

    def test_crm_and_card_only_narrative_is_rejected(self):
        i,l=self.fixture();i['narrative']='南区销售下降，金卡消费人数减少。'
        with self.assertRaises(ContractError):_validate_operating_disclosure(i,l)

    def test_scoped_trio_and_judgment_pass(self):
        i,l=self.fixture();_validate_operating_disclosure(i,l)

    def test_brand_fact_cannot_replace_region_capture(self):
        i,l=self.fixture();l['facts'].append(dict(l['facts'][-1],fact_id='brand',scope='BRAND:A@S'))
        i['operating_assessment']['fact_ids']=['CRM_SALES','MALL_SALES','brand'];i['fact_ids'].append('brand')
        with self.assertRaises(ContractError):_validate_operating_disclosure(i,l)

    def test_missing_evidence_requires_visible_reason(self):
        i,l=self.fixture();l['facts'].pop()
        with self.assertRaises(ContractError):_validate_operating_disclosure(i,l)
        i['operating_assessment']={'unavailable_reason':'Capture分母不可用。'};i['narrative']+='Capture分母不可用。'
        _validate_operating_disclosure(i,l)

    def test_total_plan_requests_operations_for_all_three_regions(self):
        a=dict(schema_version='3.0',week_id='w',input_signature='s',implementation_version='3.2.2',issues=[dict(issue_id='i',anomaly_group_ids=['g'],primary_scope={'dimension_type':'TOTAL','dimension_key':'TOTAL'},issue_type='INDEPENDENT_RISK',independent_issue_reason='material')])
        queries=default_plan(a,None)['selected_cases'][0]['queries']
        regions={q['scope']['dimension_key'] for q in queries if q['scope']['dimension_type']=='REGION' and q['focus']=='BRAND_OPERATION'}
        self.assertEqual(regions,{'N','S','W'})

    def test_region_without_own_story_still_needs_operating_judgment(self):
        i,l=self.fixture()
        for f in l['facts']:f['scope']='REGION:W'
        op=deepcopy(i['operating_assessment'])
        i['primary_scope']={'dimension_type':'TOTAL','dimension_key':'TOTAL'}
        with self.assertRaises(ContractError):_validate_unreported_region_operations(i,l,{'N','S'})
        i['operating_assessment']['regions']={'W':op}
        _validate_unreported_region_operations(i,l,{'N','S'})

if __name__=='__main__':unittest.main()
