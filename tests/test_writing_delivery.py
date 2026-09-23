import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from test_regression import artifacts, default_plan, build_ledgers, InvestigationEngine, SKILLS, select_issues
from test_pipeline import make_draft
from common import ContractError
from validate_insights import _validate_narrative_numbers, validate_insight_bundle
from writing_delivery import assemble_draft, write_workspace, validate_action_delivery
from run_pipeline import _synthesis_context
from v3_exporter import _output_insight_text


class WritingDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics, cls.rich, cls.anomalies = artifacts()
        cls.anomalies = select_issues(cls.anomalies, cls.metrics, cls.rich)
        cls.plan = default_plan(cls.anomalies, None)
        results = []
        for c in cls.plan['selected_cases']:
            for q in c['queries']:
                for result in InvestigationEngine(cls.rich).investigate(q)['results']:
                    result['insight_case_id'] = c['insight_case_id']
                    results.append(result)
        payload = {k: cls.plan[k] for k in ('schema_version', 'week_id', 'input_signature', 'implementation_version')}
        groups = {g for c in cls.plan['selected_cases'] for g in c['anomaly_group_ids']}
        payload.update(results=results, coverage=[{'anomaly_group_id': g, 'status': 'INVESTIGATED'} for g in groups])
        cls.evidence = build_ledgers(cls.anomalies, cls.plan, [payload])
        cls.bundle = make_draft(cls.plan, cls.evidence)

    def test_missing_action_and_internal_only_action_are_rejected(self):
        item = deepcopy(self.bundle['insights'][0])
        item['narrative'] = item['narrative'].rsplit('\n\n', 1)[0]
        with self.assertRaises(ContractError): validate_action_delivery(item)
        with self.assertRaises(ValueError): _output_insight_text(item)
        item['narrative'] += '\n\n' + item['action_text']
        item['recommendation'] = {'applicable': True, 'action': '建议VIC团队邀约已有名单内的星钻卡会员。'}
        with self.assertRaises(ContractError): validate_action_delivery(item)

    def test_compiler_keeps_prose_and_generates_identity(self):
        draft = deepcopy(self.bundle)
        for item in draft['insights']:
            item['analysis_text'] = item.pop('narrative').rsplit('\n\n', 1)[0]
            for k in ('issue_id', 'issue_type', 'primary_scope', 'comparison_basis', 'independent_issue_reason', 'anomaly_group_ids', 'summary', 'claims'):
                item.pop(k)
        result = assemble_draft(draft, self.plan, self.evidence)
        self.assertTrue(validate_insight_bundle(self.anomalies, self.plan, self.evidence, result)['ok'])
        self.assertEqual(result['insights'][0]['narrative'], self.bundle['insights'][0]['narrative'])
        draft['input_signature'] = 'stale'
        with self.assertRaises(ContractError): assemble_draft(draft, self.plan, self.evidence)

    def test_low_confidence_conditional_cooperation_is_not_campaign(self):
        bundle = deepcopy(self.bundle)
        item = bundle['insights'][0]
        item['action_kind'] = 'CONDITIONAL'
        item['action_condition'] = '若门店确认客流偏少且有新品展示'
        item['action_text'] = item['action_condition'] + '，建议讨论会员到店预览，随后观察到店与消费变化。'
        item['narrative'] = item['narrative'].rsplit('\n\n', 1)[0] + '\n\n' + item['action_text']
        item['recommendation'] = dict(applicable=True, target='相关门店', action=item['action_text'], channel='会员体验活动', timing='门店确认后', fact_ids=item['fact_ids'][:1])
        self.assertEqual(item['confidence'], 'LOW')
        self.assertTrue(validate_insight_bundle(self.anomalies, self.plan, self.evidence, bundle)['ok'])
        item['action_kind'] = 'CAMPAIGN'
        with self.assertRaises(ContractError): validate_insight_bundle(self.anomalies, self.plan, self.evidence, bundle)

    def test_condition_is_visible_not_internal_only(self):
        item = deepcopy(self.bundle['insights'][0])
        item.update(action_kind='CONDITIONAL', action_condition='门店确认上新档期后')
        with self.assertRaises(ContractError): validate_action_delivery(item)

    def test_natural_numbers_normalize_but_values_units_and_signs_stay_strict(self):
        for text in ('减少 339 人，提高 4.91 个百分点', '减少３３９人，提高４．９１０个百分点'):
            _validate_narrative_numbers({'narrative': text}, {'-339人', '+4.91个百分点'}, 'test')
        _validate_narrative_numbers({'narrative': '销售减少 10.0 万元'}, {'-10.00万'}, 'test')
        for text in ('增加339人', '增加-339人', '减少339笔', '提高4.91%', '提高49.1个百分点', '减少338人'):
            with self.assertRaises(ContractError):
                _validate_narrative_numbers({'narrative': text}, {'-339人', '+4.91个百分点'}, 'test')

    def test_partitioned_context_preserves_all_formal_facts_and_limits(self):
        context = _synthesis_context(self.anomalies, self.plan, self.evidence)
        with tempfile.TemporaryDirectory() as td:
            write_workspace(Path(td), context, self.plan, self.evidence)
            index = json.loads((Path(td)/'writing_context.json').read_text())
            for case, ledger in zip(index['cases'], context['evidence_ledgers']):
                data = json.loads((Path(td)/case['facts_file']).read_text())
                decoded = [dict(zip(data['fact_columns'], row)) for row in data['fact_rows']]
                for original, recovered in zip(ledger['facts'], decoded):
                    self.assertEqual(original, {k: recovered[k] for k in original})
                self.assertEqual(len(decoded), len(ledger['facts']))
                self.assertEqual(data['unavailable_evidence'], ledger['unavailable_evidence'])
                self.assertNotIn('member_id', json.dumps(data))


if __name__ == '__main__': unittest.main()
