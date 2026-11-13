import copy
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import browser_watch
import stage_contract
from test_browser_watch import stage_field


class StageContractTests(unittest.TestCase):
    def setUp(self):
        self.field = browser_watch.field(stage_field())
        self.fields = {'plan_confirmer': {'id': 'fldOwner', 'type': 'user'},
                       'plan': {'id': 'fldPlan', 'type': 'text'}}
        self.contract = {'steps': [
            {'name': '待确认计划', 'actor': 'human', 'person_field': 'plan_confirmer', 'required_fields': ['plan']},
            {'name': '可开始', 'actor': 'ai', 'person_field': None, 'required_fields': []}],
            'done': '已交付', 'cancelled': '已取消'}

    def problems(self):
        return stage_contract.problems(self.field, self.contract, self.fields)

    def test_logical_aliases_resolve_to_observed_ids(self):
        self.assertEqual(self.problems(), [])

    def test_reordered_steps_and_active_terminal_label_are_rejected(self):
        self.contract['steps'].reverse()
        self.assertTrue(any('顺序' in x for x in self.problems()))
        self.contract['steps'].reverse()
        self.field['stage_config']['property']['options'][2]['type'] = 'Default'
        self.assertTrue(any('完成或取消' in x for x in self.problems()))

    def test_required_plan_cannot_be_optional_or_combined_with_or(self):
        node = self.field['stage_config']['property']['options'][0]
        node['transitionRule']['condition']['conjunction'] = 'or'
        self.assertTrue(any('必填检查' in x for x in self.problems()))
        node['transitionRule']['condition']['conjunction'] = 'and'
        node['transitionRule']['condition']['conditions'] = []
        self.assertTrue(any('必填检查' in x for x in self.problems()))

    def test_different_person_field_and_plain_text_person_are_rejected(self):
        node = self.field['stage_config']['property']['options'][0]
        node['transitionRule']['permissionFieldIds'] = ['fldSomebodyElse']
        self.assertTrue(any('谁能完成' in x for x in self.problems()))
        node['transitionRule']['permissionFieldIds'] = ['fldOwner']
        self.fields['plan_confirmer']['type'] = 'text'
        self.assertTrue(any('人员字段' in x for x in self.problems()))

    def test_required_fields_must_be_displayed_and_mapped(self):
        self.field['stage_config']['property']['options'][0]['fieldsConfigInfo'] = []
        self.assertTrue(any('展示' in x for x in self.problems()))
        del self.fields['plan']
        self.assertTrue(any('映射' in x for x in self.problems()))

    def test_malformed_or_duplicate_contract_is_rejected_without_guessing(self):
        for change in ('missing-step-name', 'duplicate-terminal', 'unknown-required-alias'):
            contract = copy.deepcopy(self.contract)
            if change == 'missing-step-name': del contract['steps'][0]['name']
            elif change == 'duplicate-terminal': contract['cancelled'] = contract['done']
            else: contract['steps'][0]['required_fields'] = ['missing']
            with self.subTest(change=change):
                self.assertTrue(stage_contract.problems(self.field, contract, self.fields))


if __name__ == '__main__':
    unittest.main()
