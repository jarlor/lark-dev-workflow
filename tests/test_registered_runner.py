"""The same runner may use explicitly registered AI actions without claiming full native metadata."""
import copy
import json
import sys
from pathlib import Path
from unittest import mock

from test_native_stage_inspection import ProjectionReader
from test_unattended import RuntimeFixture, write_json
import unattended as runtime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))


class RegisteredRunnerTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        reader = ProjectionReader()
        self.binding = reader.config
        for role in ('requirements', 'bugs'):
            table = copy.deepcopy(self.binding['tables']['work'])
            table['id'] = 'tbl' + role.title()
            self.binding['tables'][role] = table
        write_json(self.project_path, self.binding)
        self.config.update(format_version=3, native_stage_mode='registered_requests',
                           event_tenant_key='tenant_test', stage_service_config=str(self.root / 'bridge.json'))
        self.save_config()
        self.policy = dict(native_stage_mode='registered_requests', actions={'work-start': 'fixture'},
                           direct_stage_writes=False, scope_plan_acceptance_by_human=True)
        self.policy_check = mock.patch.object(runtime.registered_requests, 'selected', return_value=self.policy)
        self.policy_check.start()
        self.addCleanup(self.policy_check.stop)
        self.snapshot['binding'] = runtime.comments.binding_identity(self.binding)
        self.snapshot['coverage'] = runtime.watch.scan_coverage(self.binding)
        self.snapshot['coverage']['native_stage_inspection'] = {'node_settings_verified':False, 'dispatch_verified':False}
        self.snapshot['native_stage_inspection'] = {
            t['id']+'/'+t['fields']['status']['id']: {'native_type':24,'ui_type':'Stage','cli_type':'select',
                'options':['待确认计划','可开始','已交付','已取消']}
            for r,t in self.binding['tables'].items() if r in ('work','requirements','bugs')}
        self.snapshot['records'] = {}

    def collect_registered(self, binding, profile, **kwargs):
        self.assertTrue(kwargs['inspect_native_stages'])
        self.assertEqual(binding, self.binding)
        return copy.deepcopy(self.snapshot)

    def registered_tick(self, executor=None):
        return runtime.tick(self.config_path, collector=self.collect_registered,
                            executor=executor or self.execute, clock=lambda: self.timestamp)

    def run_reason(self):
        return json.loads(self.calls[-1]['prompt'].split('本轮本地配置与输入位置：\n')[1])['reason']

    def test_legacy_waiting_run_rechecks_once_after_upgrade(self):
        self.delay = 3600
        self.assertEqual(self.registered_tick()['status'], 'ran')
        self.assertEqual(self.run_reason(), 'initial')
        state_path = Path(self.config['state_dir']) / 'state.json'
        legacy = self.state()
        del legacy['registered_start_check_revision']
        write_json(state_path, legacy)
        self.assertGreater(legacy['next_due_at'], self.timestamp)
        self.assertEqual(self.registered_tick()['status'], 'ran')
        self.assertEqual(self.run_reason(), 'registered_start_check_updated')
        self.assertEqual(self.state()['registered_start_check_revision'], runtime.registered_requests.START_CHECK_REVISION)
        self.assertEqual(self.calls[-1]['state']['session_id'], legacy['session_id'])
        self.assertEqual(self.registered_tick()['status'], 'unchanged')
        self.assertEqual(len(self.calls), 2)

    def test_new_revision_leaves_idle_and_paused_alone_and_preserves_changed_reason(self):
        self.outcome = 'idle'
        with mock.patch.object(runtime.registered_requests, 'START_CHECK_REVISION', 1):
            self.assertEqual(self.registered_tick()['status'], 'ran')
        self.assertEqual(self.registered_tick()['status'], 'unchanged')
        self.assertEqual(self.state()['registered_start_check_revision'], 1)
        self.outcome = 'waiting'
        self.snapshot['comments']['new'] = {'revision': 'a new reply'}
        with mock.patch.object(runtime.registered_requests, 'START_CHECK_REVISION', 1):
            self.assertEqual(self.registered_tick()['status'], 'ran')
        paused = Path(self.config['state_dir']) / 'PAUSED'
        paused.write_text('人员暂停')
        self.assertEqual(self.registered_tick()['status'], 'paused')
        self.assertEqual(self.state()['registered_start_check_revision'], 1)
        paused.unlink()
        self.snapshot['comments']['new']['revision'] = 'another reply'
        self.assertEqual(self.registered_tick()['status'], 'ran')
        self.assertEqual(self.run_reason(), 'changed')
        self.assertEqual(self.registered_tick()['status'], 'unchanged')

    def test_failed_upgrade_recheck_keeps_previous_revision_and_recovery(self):
        with mock.patch.object(runtime.registered_requests, 'START_CHECK_REVISION', 1):
            self.assertEqual(self.registered_tick()['status'], 'ran')
        original = self.state()
        failure = mock.Mock(side_effect=runtime.RunnerError('worker interrupted'))
        self.assertEqual(self.registered_tick(executor=failure)['status'], 'failed')
        failed = self.state()
        self.assertEqual(failed['registered_start_check_revision'], 1)
        self.assertEqual(failed['last_result'], original['last_result'])
        self.assertEqual(failed['checkpoint'], original['checkpoint'])
        self.assertIn('inflight', failed)
        self.assertEqual(self.registered_tick()['status'], 'paused')
        failure.assert_called_once()

    def test_read_only_scan_and_normal_tick_use_same_account_and_selected_action_entry(self):
        scanned = runtime.scan_only(self.config_path, collector=self.collect_registered)
        self.assertEqual(scanned['status'], 'scanned_for_registered_requests')
        self.assertFalse(scanned['codex_started'])
        self.assertEqual(scanned['coverage']['execution_scope'], self.policy)
        self.assertFalse(scanned['coverage']['native_stage_inspection']['node_settings_verified'])
        marker = {'role':'work','record_id':'recWork','status':'verified'}
        with mock.patch.object(runtime.registered_requests, 'retain_clicks', return_value=[marker]) as capture:
            result=runtime.tick(self.config_path, collector=self.collect_registered, executor=self.execute)
        self.assertEqual(result['status'], 'ran')
        capture.assert_called_once()
        ctx=json.loads(self.calls[-1]['prompt'].split('本轮本地配置与输入位置：\n')[1])
        self.assertEqual(ctx['stage_service_config'], self.config['stage_service_config'])
        self.assertEqual(ctx['execution_scope'], self.policy)
        packet=runtime.read_json(self.calls[-1]['dir']/'work-packet.json')
        self.assertEqual(packet['confirmation_checks'], [marker])
        self.assertEqual(runtime.execution_directory(self.config), Path(self.config['state_dir']))

    def test_inspection_alone_or_missing_stage_cannot_start_registered_work(self):
        self.snapshot['native_stage_inspection'].popitem()
        with self.assertRaisesRegex(runtime.RunnerError, '三张事项表'):
            runtime.scan_only(self.config_path, collector=self.collect_registered)
        self.assertFalse(self.calls)
        self.assertFalse((Path(self.config['state_dir'])/'last-scan.json').exists())

    def test_registered_configuration_is_explicit_and_has_separate_identity(self):
        strict=dict(self.config)
        strict.pop('native_stage_mode')
        strict.pop('stage_service_config')
        self.assertNotEqual(runtime.identity(strict,self.binding),runtime.identity(self.config,self.binding))
        different=dict(self.config, stage_service_config=str(self.root/'elsewhere.json'))
        self.assertNotEqual(runtime.identity(self.config,self.binding),runtime.identity(different,self.binding))
        for change in ({'stage_service_config':None},{'stage_service_config':'bridge.json'},
                       {'native_stage_mode':'prepare'},{'format_version':1}):
            with self.subTest(change=change), self.assertRaises(runtime.control.ControlError):
                runtime.control._validate(dict(self.config,**change),self.config_path)

    def test_registry_drift_during_scan_discards_candidate(self):
        with mock.patch.object(runtime.registered_requests, 'selected',
                               side_effect=[self.policy, dict(self.policy, actions={})]):
            with self.assertRaisesRegex(runtime.RunnerError, '助手规则已变'):
                runtime.scan_only(self.config_path, collector=self.collect_registered)
        self.assertFalse((Path(self.config['state_dir']) / 'last-scan.json').exists())

    def test_identity_transfer_compares_prior_native_rules_and_fresh_rule_pins(self):
        import browser_watch
        import identity_handoff as handoff
        from test_browser_watch import stage_field

        old = {'stage_fields': {}}
        snapshot = copy.deepcopy(self.snapshot)
        for role in ('work', 'requirements', 'bugs'):
            table = self.binding['tables'][role]
            fields = table['fields']
            fields['confirmer']['id'] = 'fldOwner'
            key = table['id'] + '/' + fields['status']['id']
            old['stage_fields'][key] = browser_watch.field(stage_field())
        snapshot['binding'] = runtime.comments.binding_identity(self.binding)
        snapshot['coverage'] = dict(runtime.watch.scan_coverage(self.binding),
                                    native_stage_inspection={'node_settings_verified': False, 'dispatch_verified': False},
                                    execution_scope=self.policy)
        with mock.patch.object(runtime.registered_requests, 'selected', return_value=self.policy) as policy:
            handoff.require_registered_scan(old, snapshot, self.config, self.binding)
            policy.assert_called_once_with(self.config, self.binding, verify_live=True)
        for old_copy, current, reason in [
            ({'stage_fields': {}}, snapshot, 'original checkpoint'),
            (old, dict(snapshot, native_stage_inspection={}), 'live stage projection'),
            (old, dict(snapshot, coverage=dict(snapshot['coverage'], execution_scope={})), 'registered workflow'),
            (old, dict(snapshot, binding='different'), 'registered workflow'),
        ]:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                handoff.require_registered_scan(old_copy, current, self.config, self.binding)
        changed = copy.deepcopy(old)
        field = next(iter(changed['stage_fields'].values()))
        field['stage_config']['property']['options'][0]['transitionRule']['permissionFieldIds'] = ['fldOther']
        with self.assertRaisesRegex(ValueError, 'original checkpoint'):
            handoff.require_registered_scan(changed, snapshot, self.config, self.binding)
        with self.assertRaisesRegex(ValueError, 'full project scope'):
            handoff.require_full_scan(snapshot, self.binding)
