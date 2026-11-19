"""Exercise the normal runner with the official CLI's limited Stage projection."""
import copy
import json
from pathlib import Path
from unittest import mock
import uuid

from test_native_stage_inspection import ProjectionReader
from test_unattended import RuntimeFixture, write_json
import gate_events
import unattended as runtime
import watch


class PrepareTickTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.reader = ProjectionReader()
        self.binding = self.reader.config
        write_json(self.project_path, self.binding)
        self.config.update(format_version=3, event_tenant_key='tenant_test', native_stage_mode='prepare')
        self.save_config()
        self.directory = Path(self.config['state_dir'])
        self.prepare_dir = self.directory / 'native-stage-prepare'

    def collect(self, binding, profile, *, comments_state_path, lark_bin, inspect_native_stages=False):
        return watch.collect_with_reader(binding, self.reader, comments_state_path=comments_state_path,
                                         inspect_native_stages=inspect_native_stages)

    def dispatch(self, collector=None):
        return runtime.tick(self.config_path, collector=collector or self.collect,
                            executor=self.execute, clock=lambda: self.timestamp)

    def context(self):
        return json.loads(self.calls[-1]['prompt'].split('本轮本地配置与输入位置：\n')[1])

    def test_only_paired_explicit_bot_can_select_prepare(self):
        for changes in ({'native_stage_mode': 'strict'}, {'native_stage_mode': None},
                        {'format_version': 1}, {'format_version': 2}, {'event_tenant_key': ''}):
            with self.subTest(changes=changes), self.assertRaises(runtime.control.ControlError):
                runtime.control._validate(dict(self.config, **changes), self.config_path)
        del self.config['event_tenant_key']
        self.save_config()
        with self.assertRaisesRegex(runtime.control.ControlError, '需要已配置'):
            runtime.control.load_config(self.config_path)

    def test_scan_and_dispatch_use_checked_projection_and_same_existing_executor(self):
        result = runtime.scan_only(self.config_path, collector=self.collect)
        self.assertEqual(result['status'], 'scanned_for_preparation')
        self.assertEqual(Path(result['snapshot_file']), self.prepare_dir / 'last-scan.json')
        self.assertFalse(result['codex_started'] or result['checkpoint_advanced'])
        self.assertFalse((self.directory / 'last-scan.json').exists())
        self.assertTrue((self.directory / gate_events.INDEX).is_file())
        self.assertFalse((self.prepare_dir / 'state.json').exists())
        self.assertEqual(self.dispatch()['status'], 'ran')
        context = self.context()
        run_dir = self.calls[-1]['dir']
        snapshot = runtime.read_json(run_dir / 'snapshot.json')
        packet = runtime.read_json(run_dir / 'work-packet.json')
        self.assertEqual(context['reason'], 'initial')
        self.assertEqual(Path(context['comments_journal_file']), self.prepare_dir / 'comments-state.json')
        self.assertEqual(packet['execution_scope'], context['execution_scope'])
        self.assertEqual(packet['execution_scope'], snapshot['coverage']['execution_scope'])
        self.assertFalse(packet['execution_scope']['native_stage_writes'])
        self.assertFalse(packet['execution_scope']['implementation_or_dispatch'])
        self.assertFalse(snapshot['coverage']['native_stage_inspection']['node_settings_verified'])
        self.assertIn('tblWork/recWork', packet['record_index'])
        self.assertTrue(packet['thread_index'])
        self.assertEqual(self.dispatch()['status'], 'unchanged')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.directory / 'state.json').exists())
        for name in ('state.json', 'checkpoint-snapshot.json', 'comments-state.json'):
            self.assertTrue((self.prepare_dir / name).is_file())

    def test_native_or_other_field_drift_and_switch_during_scan_do_not_start_worker(self):
        self.reader.native['type'] = 3
        self.assertEqual(self.dispatch()['status'], 'failed')
        self.reader.native['type'] = 24
        original = self.reader.fields
        def missing_field(token, table):
            return [item for item in original(token, table) if item['id'] != 'fldPlan']
        with mock.patch.object(self.reader, 'fields', side_effect=missing_field):
            self.assertEqual(self.dispatch()['status'], 'failed')
        def changed_config(*args, **kwargs):
            snap = self.collect(*args, **kwargs)
            self.config.pop('native_stage_mode')
            self.save_config()
            return snap
        self.assertEqual(self.dispatch(changed_config)['status'], 'failed')
        self.assertFalse(self.calls)
        self.assertFalse((self.prepare_dir / 'checkpoint-snapshot.json').exists())

    def test_mode_switch_preserves_sessions_checks_again_and_never_consumes_strict_state(self):
        # Use a full-coverage snapshot to model strict mode on a reader with full native settings.
        def strict(*args, **kw):
            self.assertNotIn('inspect_native_stages', kw)
            return copy.deepcopy(self.snapshot)
        self.config.pop('native_stage_mode')
        self.save_config()
        strict_identity = runtime.identity(self.config, self.binding)
        self.assertEqual(self.dispatch(collector=strict)['status'], 'ran')
        strict_state = (self.directory / 'state.json').read_bytes()
        strict_checkpoint = (self.directory / 'checkpoint-snapshot.json').read_bytes()
        strict_session = self.session
        self.session = str(uuid.uuid4())
        self.config['native_stage_mode'] = 'prepare'
        self.save_config()
        self.assertNotEqual(strict_identity, runtime.identity(self.config, self.binding))
        self.assertEqual(self.dispatch()['status'], 'ran')
        self.assertEqual(self.context()['reason'], 'mode_changed')
        self.assertEqual(Path(self.context()['previous_mode_state_file']), self.directory / 'state.json')
        self.assertIsNone(self.calls[-1]['state']['session_id'])
        self.assertEqual((self.directory / 'state.json').read_bytes(), strict_state)
        self.assertEqual((self.directory / 'checkpoint-snapshot.json').read_bytes(), strict_checkpoint)
        partial_state = (self.prepare_dir / 'state.json').read_bytes()
        self.config.pop('native_stage_mode')
        self.save_config()
        self.assertEqual(self.dispatch(collector=strict)['status'], 'ran')
        self.assertEqual(self.context()['reason'], 'mode_changed')
        self.assertNotIn('execution_scope', self.context())
        self.assertEqual(self.calls[-1]['state']['session_id'], strict_session)
        self.assertEqual((self.prepare_dir / 'state.json').read_bytes(), partial_state)
        self.assertEqual(runtime.identity(self.config, self.binding), strict_identity)

    def test_pause_and_unfinished_writes_apply_across_modes(self):
        runtime.control_state(self.config_path, 'pause')
        self.assertEqual(self.dispatch()['status'], 'paused')
        self.config.pop('native_stage_mode')
        self.save_config()
        self.assertEqual(self.dispatch()['status'], 'paused')
        self.assertEqual(runtime.control_state(self.config_path, 'resume')['status'], 'enabled')
        for other in ({'inflight': {'run_id':'unconfirmed'}}, {'recovery_note':'unknown effects'}):
            write_json(self.prepare_dir / 'state.json', other)
            self.assertIn('未核实的写入', self.dispatch()['message'])
            with self.assertRaisesRegex(runtime.RunnerError, '未核实的写入'):
                runtime.control_state(self.config_path, 'recover')
        self.assertFalse(self.calls)

    def test_standalone_inspection_stays_separate_and_does_not_start_prepare(self):
        result = runtime.scan_only(self.config_path, collector=self.collect, inspect_native_stages=True)
        self.assertEqual(result['status'], 'inspected')
        self.assertNotIn('execution_scope', result['coverage'])
        self.assertEqual(Path(result['snapshot_file']).parent, self.directory / 'native-stage-inspection')
        self.assertFalse((self.prepare_dir / 'state.json').exists())
        self.assertFalse(self.calls)

    def test_event_batches_are_read_from_the_existing_shared_receiver(self):
        with mock.patch.object(gate_events, 'pending', wraps=gate_events.pending) as pending:
            self.assertEqual(self.dispatch()['status'], 'ran')
            path, scope, state = pending.call_args.args
        self.assertEqual(path, self.directory)
        self.assertEqual(scope['tables']['tblWork']['native_stage_field_id'], 'fldStage')
        self.assertEqual(state['identity'], runtime.identity(self.config, self.binding))
