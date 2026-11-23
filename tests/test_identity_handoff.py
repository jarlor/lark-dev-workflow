import copy
import functools
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from test_unattended import RuntimeFixture, write_json
from test_bot_identity import ACTOR
from test_browser_watch import browser, fixture as browser_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service'))
import identity_handoff as transfer
from identity_handoff import runtime


class Handoff(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.old_dir = Path(self.config['state_dir'])
        self.snapshot['binding'] = runtime.comments.binding_identity(self.binding)
        self.snapshot['coverage'] = runtime.watch.scan_coverage(self.binding)
        runtime.tick(self.config_path, collector=self.collect, executor=self.execute)
        state = self.state()
        last = state['last_result']
        write_json(self.old_dir / 'runs' / last['run_id'] / 'result.json', last)
        self.bot_binding = dict(self.binding, cli_actor=ACTOR)
        self.bot_dir = self.root / 'new-state'
        self.bot_dir.mkdir()
        self.bot_config = self.root / 'candidate.json'
        bot_project = self.root / 'bot-binding.json'
        write_json(bot_project, self.bot_binding)
        write_json(self.bot_config, dict(self.config, format_version=3, project_config=str(bot_project),
                                         state_dir=str(self.bot_dir), lark_profile=ACTOR['profile']))
        self.bot_snapshot = dict(self.snapshot, binding=runtime.comments.binding_identity(self.bot_binding))
        write_json(self.bot_dir / 'last-scan.json', self.bot_snapshot)
        write_json(self.bot_dir / 'comments-state.json', dict(runtime.comments.empty_state(self.bot_binding),
                   last_full_reconciliation='candidate-scan'))

    def test_requires_preview_then_freezes_both_and_keeps_original_session(self):
        previous = (self.old_dir / 'state.json').read_bytes()
        review = transfer.handoff(self.config_path, self.bot_config)
        self.assertFalse((self.old_dir / 'PAUSED').exists())
        self.assertFalse((self.bot_dir / 'state.json').exists())
        with self.assertRaises(ValueError):
            transfer.handoff(self.config_path, self.bot_config, 'stale')
        result = transfer.handoff(self.config_path, self.bot_config, review['review_sha256'])
        self.assertEqual(result['status'], 'prepared_both_paused')
        self.assertTrue(all((directory / 'PAUSED').is_file() for directory in (self.old_dir, self.bot_dir)))
        self.assertEqual((self.old_dir / 'state.json').read_bytes(), previous)
        state = runtime.read_json(self.bot_dir / 'state.json')
        self.assertEqual((state['session_id'], state['last_result']), (self.session, self.state()['last_result']))
        self.assertEqual(state['continuation_queue'], self.state()['continuation_queue'])
        self.assertIsNone(state['checkpoint'])
        self.assertEqual(state['actor_handoff']['previous_state_dir'], str(self.old_dir))
        self.assertFalse((self.bot_dir / 'checkpoint-snapshot.json').exists())

    def test_completed_browser_checkpoint_to_strict_assistant_scan(self):
        binding, access, web_payload = browser_fixture()
        access['node_bin'] = sys.executable
        browser_config = dict(self.config, format_version=2, access=access, state_dir=str(self.root / 'browser-state'))
        browser_config.pop('lark_profile')
        browser_config.pop('lark_bin')
        write_json(self.config_path, browser_config)
        write_json(self.project_path, binding)
        target_binding = dict(binding, cli_actor=ACTOR)
        write_json(self.root / 'bot-binding.json', target_binding)
        response = subprocess.CompletedProcess([], 0, json.dumps(web_payload), '')
        collect = functools.partial(browser.collect, runner=mock.Mock(return_value=response))
        with mock.patch.object(browser, 'collect', collect):
            self.assertEqual(runtime.tick(self.config_path, executor=self.execute)['status'], 'ran')
        source_dir = Path(browser_config['state_dir'])
        state = runtime.read_json(source_dir / 'state.json')
        last = state['last_result']
        write_json(source_dir / 'runs' / last['run_id'] / 'result.json', last)
        observed = runtime.run_context.checkpoint_snapshot(source_dir, state)
        self.assertTrue(observed['comments'])
        target = dict(observed, coverage=runtime.watch.scan_coverage(target_binding),
                      binding=runtime.comments.binding_identity(target_binding))
        write_json(self.bot_dir / 'last-scan.json', target)
        journal = runtime.comments.empty_state(target_binding)
        write_json(self.bot_dir / 'comments-state.json', journal)
        with self.assertRaisesRegex(ValueError, 'full comment scan'):
            self.review()
        write_json(self.bot_dir / 'comments-state.json', dict(journal, last_full_reconciliation='new-scan'))
        self.assertTrue(self.review())
        write_json(self.bot_dir / 'last-scan.json', dict(target, comments={}))
        with self.assertRaisesRegex(ValueError, 'comments are missing'):
            self.review()

        for key in ('access', 'comments', 'consistency'):
            for missing in (True, False):
                with self.subTest(key=key, missing=missing):
                    coverage = dict(observed['coverage'])
                    if missing:
                        del coverage[key]
                    else:
                        coverage[key] = 'unknown'
                    with self.assertRaisesRegex(ValueError, 'scope'):
                        transfer.require_full_scan(dict(observed, coverage=coverage), binding, browser=True)

    def test_partial_unknown_or_mismatched_assistant_scan_rejected(self):
        coverage = self.bot_snapshot['coverage']
        for updates in ({'format_version': 2}, {'binding': self.snapshot['binding']},
                        {'coverage': dict(coverage, access='personal_browser')},
                        {'coverage': dict(coverage, feedback={'mode': 'linked_only'})},
                        {'coverage': dict(coverage, execution_scope='preparation')},
                        {'coverage': dict(coverage, native_stage_inspection={'dispatch_verified': False})},
                        {'native_stage_inspection': {}}):
            with self.subTest(updates=updates):
                write_json(self.bot_dir / 'last-scan.json', dict(self.bot_snapshot, **updates))
                with self.assertRaisesRegex(ValueError, 'scope'):
                    self.review()
        write_json(self.bot_dir / 'last-scan.json', self.bot_snapshot)
        config = runtime.read_json(self.bot_config)
        write_json(self.bot_config, dict(config, native_stage_mode='prepare', event_tenant_key='tenant'))
        with self.assertRaisesRegex(ValueError, 'Preparation'):
            self.review()

    def test_switching_a_used_prepare_runner_to_strict_does_not_make_it_unused(self):
        nested = self.bot_dir / 'native-stage-prepare'
        nested.mkdir()
        write_json(nested / 'last-scan.json', {'coverage': 'inspection'})
        self.assertTrue(self.review())  # An earlier read-only check is compatible with a later full scan.
        write_json(nested / 'state.json', {'inflight': {'run_id': 'unfinished'}})
        with self.assertRaisesRegex(ValueError, 'never have run'):
            self.review()

    def review(self):
        return transfer.handoff(self.config_path, self.bot_config)['review_sha256']

    def apply(self, digest):
        return transfer.handoff(self.config_path, self.bot_config, digest)

    def interrupt_before_move(self, paused_path):
        original = transfer.move_pause

        def interrupt(staged, destination):
            if destination == paused_path:
                raise OSError('simulated stop')
            return original(staged, destination)
        return mock.patch.object(transfer, 'move_pause', side_effect=interrupt)

    def test_retry_each_boundary_without_rewriting_state_or_pause(self):
        digest = self.review()
        original_state = (self.old_dir / 'state.json').read_bytes()
        for directory in (self.bot_dir, self.old_dir):
            with self.interrupt_before_move(directory / 'PAUSED'), self.assertRaisesRegex(OSError, 'simulated'):
                self.apply(digest)
            self.assertEqual(self.review(), digest)
            self.assertFalse((directory / 'PAUSED').exists())
        original_write = runtime.control._new_json

        def write_then_stop(path, data):
            original_write(path, data)
            raise OSError('simulated stop')

        with mock.patch.object(runtime.control, '_new_json', side_effect=OSError('simulated stop')):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.apply(digest)
        state_file = self.bot_dir / 'state.json'
        self.assertFalse(state_file.exists())
        self.assertTrue(all((d / 'PAUSED').exists() for d in (self.bot_dir, self.old_dir)))
        with mock.patch.object(runtime.control, '_new_json', side_effect=write_then_stop):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.apply(digest)
        paths = [state_file, self.old_dir / 'PAUSED', self.bot_dir / 'PAUSED']
        before = [(p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in paths]
        with mock.patch.object(transfer.time, 'time', return_value=transfer.time.time() + 3600):
            self.assertEqual(self.apply(digest)['status'], 'prepared_both_paused')
        self.assertEqual(before, [(p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in paths])
        self.assertEqual(original_state, (self.old_dir / 'state.json').read_bytes())
        self.assertEqual((self.bot_dir / 'identity-handoff.intent.json').stat().st_mode & 0o777, 0o600)

    def test_interrupted_before_intent_leaves_runners_enabled_and_retryable(self):
        digest = self.review()
        with mock.patch.object(runtime.control, '_new_json', side_effect=OSError('no space')):
            with self.assertRaisesRegex(OSError, 'no space'):
                self.apply(digest)
        self.assertFalse(any((d / 'PAUSED').exists() for d in (self.old_dir, self.bot_dir)))
        self.assertFalse((self.bot_dir / 'state.json').exists())
        self.assertEqual(self.apply(digest)['status'], 'prepared_both_paused')

    def test_resumed_candidate_before_state_install_is_never_frozen_again(self):
        digest = self.review()
        with self.interrupt_before_move(self.old_dir / 'PAUSED'), self.assertRaises(OSError):
            self.apply(digest)
        runtime.control_state(self.bot_config, 'resume')
        with self.assertRaisesRegex(ValueError, 'operator'):
            self.apply(digest)
        self.assertFalse(any((d / 'PAUSED').exists() for d in (self.old_dir, self.bot_dir)))
        self.assertFalse((self.bot_dir / 'state.json').exists())

    def test_resumed_completed_candidate_is_left_alone(self):
        digest = self.review()
        self.apply(digest)
        runtime.control_state(self.bot_config, 'resume')
        before = (self.bot_dir / 'state.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'operator'):
            self.apply(digest)
        self.assertFalse((self.bot_dir / 'PAUSED').exists())
        self.assertEqual(before, (self.bot_dir / 'state.json').read_bytes())

    def test_manual_pause_during_no_replace_install_is_preserved(self):
        digest = self.review()
        original_move = transfer.move_pause
        def stop(staged, dest):
            runtime.control_state(self.bot_config, 'pause')
            original_move(staged, dest)

        with mock.patch.object(transfer, 'move_pause', side_effect=stop), self.assertRaises(FileExistsError):
            self.apply(digest)
        reason = (self.bot_dir / 'PAUSED').read_bytes()
        with self.assertRaisesRegex(ValueError, 'operator'):
            self.apply(digest)
        self.assertEqual(reason, (self.bot_dir / 'PAUSED').read_bytes())
        self.assertFalse((self.old_dir / 'PAUSED').exists())

    def test_rechecks_request_and_snapshot_after_interruption(self):
        digest = self.review()
        with self.interrupt_before_move(self.old_dir / 'PAUSED'), self.assertRaises(OSError):
            self.apply(digest)
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.apply('wrong')
        other = self.root / 'other-config.json'
        other.write_bytes(self.bot_config.read_bytes())
        with self.assertRaisesRegex(ValueError, 'Another handoff'):
            transfer.handoff(self.config_path, other, digest)
        write_json(self.bot_dir / 'last-scan.json', dict(self.bot_snapshot, observed_at='changed scan'))
        with self.assertRaises(ValueError):
            self.apply(digest)
        self.assertFalse((self.old_dir / 'PAUSED').exists())
        self.assertFalse((self.bot_dir / 'state.json').exists())

    def test_missing_record_or_pending_execution_blocks_handoff(self):
        write_json(self.bot_dir / 'last-scan.json', dict(self.bot_snapshot, records={}))
        with self.assertRaisesRegex(ValueError, 'records'):
            transfer.handoff(self.config_path, self.bot_config)
        write_json(self.bot_dir / 'last-scan.json', self.bot_snapshot)
        write_json(self.old_dir / 'state.json', dict(self.state(), inflight={'run_id': 'pending'}))
        with self.assertRaisesRegex(ValueError, 'progress'):
            transfer.handoff(self.config_path, self.bot_config)
        self.assertFalse((self.old_dir / 'PAUSED').exists())

    def test_manual_stop_and_competing_worker_are_preserved(self):
        with runtime.lock_workspace(self.config) as lock:
            self.assertIsNotNone(lock)
            with self.assertRaisesRegex(ValueError, 'busy'):
                transfer.handoff(self.config_path, self.bot_config)
        (self.old_dir / 'PAUSED').write_text('operator stop')
        with self.assertRaisesRegex(ValueError, 'paused'):
            transfer.handoff(self.config_path, self.bot_config)
        self.assertEqual((self.old_dir / 'PAUSED').read_text(), 'operator stop')

    def test_scope_or_rules_change_blocks_identity_only_handoff(self):
        for key, other in [('scope', {'mode': 'whole_base'}), ('governance', {'allow_write': True})]:
            with self.subTest(key=key):
                altered = copy.deepcopy(self.bot_binding)
                altered[key] = other
                write_json(self.root / 'bot-binding.json', altered)
                with self.assertRaisesRegex(ValueError, 'same|keep'):
                    transfer.handoff(self.config_path, self.bot_config)
