import copy
import json
from pathlib import Path
import subprocess
from unittest import mock

from test_unattended import RuntimeFixture, write_json
from test_watch import FakeCli
import gate_events
import onboard
import unattended as runtime
import watch


class ProjectionReader(FakeCli):
    def __init__(self):
        super().__init__()
        work = self.config['tables']['work']['fields']
        work['status'] = {'id': 'fldStage', 'type': 'stage',
                          'options': [{'name': name} for name in ('待确认计划', '可开始', '已交付', '已取消')],
                          'stage_contract': {'steps': [
                              {'name': '待确认计划', 'actor': 'human', 'person_field': 'confirmer',
                               'required_fields': ['plan']},
                              {'name': '可开始', 'actor': 'ai', 'person_field': None, 'required_fields': []}],
                              'done': '已交付', 'cancelled': '已取消'}}
        work.update(confirmer={'id': 'fldConfirmer', 'type': 'user'}, plan={'id': 'fldPlan', 'type': 'text'})
        self.data['tblWork'][0]['values'].update(fldStage=['待确认计划'], fldConfirmer=[], fldPlan='')
        self.options = copy.deepcopy(work['status']['options'])
        self.native = {'field_id': 'fldStage', 'type': 24, 'ui_type': 'Stage', 'property': None}
        self.config['cli_actor'] = {'identity': 'bot', 'app_id': 'cli_demo', 'open_id': 'ou_assistant',
                                    'profile': self.profile}
        self.base = mock.Mock(spec=onboard.Cli, operator='bot')
        self.base._envelope.side_effect = lambda r, k: onboard.Cli._envelope(self.base, r, k)
        self.raw_calls = []
        self.pages = None

    def fields(self, token, table):
        values = super().fields(token, table)
        if table == 'tblWork':
            values = [dict(id='fldStage', type='select', multiple=False, options=self.options)
                      if v['id'] == 'fldStage' else v for v in values]
        return values

    def runner(self, argv, **kwargs):
        self.raw_calls.append(argv)
        self.assert_target(argv)
        page = (self.pages.pop(0) if self.pages is not None else
                {'items': [self.native], 'has_more': False, 'total': 1})
        return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'identity': 'bot', 'data': page}), '')

    def assert_target(self, argv):
        assert argv[1:6] == ['api', 'GET', '/open-apis/bitable/v1/apps/BaseFixture/tables/tblWork/fields',
                              '--as', 'bot']
        assert argv[argv.index('--profile') + 1] == self.profile

    def records(self, token, table, fields, predicate):
        if table == 'tblWork':
            assert next(f for f in fields if f['id'] == 'fldStage')['type'] == 'select'
        return super().records(token, table, fields, predicate)


class InspectionTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.reader = ProjectionReader()
        self.binding = self.reader.config
        write_json(self.project_path, self.binding)
        self.config.update(format_version=3, event_tenant_key='tenant_test')
        self.save_config()
        self.directory = Path(self.config['state_dir'])
        self.directory.mkdir(exist_ok=True)

    def collect(self, binding, profile, *, comments_state_path, lark_bin, inspect_native_stages=False):
        return watch.collect_with_reader(binding, self.reader, comments_state_path=comments_state_path,
                                         inspect_native_stages=inspect_native_stages)

    def inspect(self):
        return runtime.scan_only(self.config_path, collector=self.collect, inspect_native_stages=True)

    def test_complete_separate_scan_registers_only_live_options_without_touching_strict_state(self):
        names = ['last-scan.json', 'comments-state.json', 'state.json', 'checkpoint-snapshot.json']
        for name in names:
            (self.directory / name).write_bytes(b'unchanged')
        result = self.inspect()
        self.assertEqual(result['status'], 'inspected')
        self.assertFalse(result['codex_started'] or result['checkpoint_advanced'])
        self.assertTrue(result['event_scope_registered'])
        self.assertEqual([result[k] for k in ('records','comments','inspected_stage_fields')], [9,1,1])
        snapshot = json.loads(Path(result['snapshot_file']).read_text())
        self.assertNotIn('stage_fields', snapshot)
        self.assertFalse(snapshot['coverage']['native_stage_inspection']['node_settings_verified'])
        self.assertTrue((self.directory / 'native-stage-inspection/comments-state.json').is_file())
        self.assertTrue(all((self.directory / name).read_bytes() == b'unchanged' for name in names))
        table = json.loads((self.directory / gate_events.INDEX).read_text())['tables']['tblWork']
        self.assertEqual(table['records'], ['recWork'])
        self.assertEqual(table['options'], {x['name']: x['name'] for x in self.reader.options})

    def test_strict_tick_and_scan_still_require_current_node_rules(self):
        self.inspect()
        with self.assertRaisesRegex(watch.WatchError, '流程节点'):
            runtime.scan_only(self.config_path, collector=self.collect)
        worker = mock.Mock()
        result = runtime.tick(self.config_path, collector=self.collect, executor=worker)
        self.assertEqual(result['status'], 'failed')
        worker.assert_not_called()
        self.assertFalse((self.directory / 'last-scan.json').exists())

    def test_inspection_snapshot_cannot_be_replayed_as_full_scan(self):
        result = self.inspect()
        snapshot = json.loads(Path(result['snapshot_file']).read_text())
        with self.assertRaisesRegex(runtime.RunnerError, '不能混用'):
            runtime.scan_only(self.config_path, collector=lambda *a, **kw: snapshot)
        self.assertFalse((self.directory / 'last-scan.json').exists())

    def test_unrelated_bound_fields_and_person_alias_are_still_verified(self):
        actual = self.reader.fields
        def missing_plan(token, table):
            return [f for f in actual(token, table) if f['id'] != 'fldPlan']
        with mock.patch.object(self.reader, 'fields', side_effect=missing_plan):
            with self.assertRaisesRegex(onboard.OnboardingError, 'work.plan'):
                self.inspect()
        self.binding['tables']['work']['fields']['confirmer']['type'] = 'text'
        write_json(self.project_path, self.binding)
        with self.assertRaisesRegex(onboard.OnboardingError, '人员字段'):
            self.inspect()

    def test_regular_single_select_or_wrong_ui_type_cannot_impersonate_stage(self):
        for kind, ui_type in [(3, 'SingleSelect'), (24, 'SingleSelect'), (3, 'Stage')]:
            with self.subTest(kind=kind, ui_type=ui_type):
                self.reader.native.update(type=kind, ui_type=ui_type)
                with self.assertRaisesRegex(onboard.OnboardingError, '24 / Stage'):
                    self.inspect()
        self.assertFalse((self.directory / gate_events.INDEX).exists())

    def test_missing_contract_or_drift_rejected_without_guessing(self):
        self.reader.options.reverse()
        with self.assertRaisesRegex(onboard.OnboardingError, '顺序'):
            self.inspect()
        self.reader.options.reverse()
        del self.binding['tables']['work']['fields']['status']['stage_contract']
        write_json(self.project_path, self.binding)
        with self.assertRaisesRegex(onboard.OnboardingError, '流程约定'):
            self.inspect()

    def test_out_of_project_or_unknown_cell_does_not_replace_prior_evidence(self):
        self.inspect()
        saved = {p: p.read_bytes() for p in (self.directory / gate_events.INDEX,
                 self.directory / 'native-stage-inspection/comments-state.json',
                 self.directory / 'native-stage-inspection/last-scan.json')}
        row = self.reader.data['tblWork'][0]['values']
        row['fldProject'] = [{'id':'other'}]
        with self.assertRaisesRegex(onboard.OnboardingError, 'outside this project'):
            self.inspect()
        row.update(fldProject=[{'id':'recA'}], fldStage=['未知状态'])
        with self.assertRaisesRegex(onboard.OnboardingError, '状态选项'):
            self.inspect()
        self.assertTrue(all(p.read_bytes() == value for p, value in saved.items()))

    def test_all_raw_pages_required_using_bound_identity(self):
        page = {'items': [dict(field_id='fldTitle')], 'has_more': True, 'total': 2, 'page_token': 'next'}
        self.reader.pages = [page, {'items':[self.reader.native], 'total':2, 'has_more':False}]
        self.assertEqual(self.inspect()['status'], 'inspected')
        self.assertEqual(json.loads(self.reader.raw_calls[-1][-1])['page_token'], 'next')
        self.reader.pages = [page, dict(page, items=[])]
        with self.assertRaisesRegex(onboard.OnboardingError, '分页'):
            self.inspect()
