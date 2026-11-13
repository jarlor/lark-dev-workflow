"""A schema migration keeps evidence and never starts or approves business work."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import unattended as runtime
from test_watch import config as binding_fixture


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.binding = binding_fixture()
        self.binding['documents'] = {}
        self.binding['governance'] = {}
        self.project = self.work / 'project.json'
        write(self.project, self.binding)
        self.candidate = copy.deepcopy(self.binding)
        self.candidate['tables']['work']['fields']['plan'] = {
            'id': 'fldPlan', 'name': '研发计划', 'type': 'text'}
        self.candidate_path = self.root / 'candidate.json'
        write(self.candidate_path, self.candidate)
        self.config_path = self.root / 'runner.json'
        self.directory = self.root / 'private-state'
        self.config = {'format_version': 1, 'project_config': str(self.project),
            'workdir': str(self.work), 'state_dir': str(self.directory),
            'python_bin': sys.executable, 'codex_bin': sys.executable, 'lark_bin': sys.executable,
            'path': str(Path(sys.executable).parent) + ':/usr/bin:/bin',
            'lark_profile': 'fixture', 'codex_profile': None,
            'interval_minutes': 5, 'run_timeout_seconds': 60, 'max_consecutive_runs': 2,
            'reconcile_interval_seconds': 86400}
        write(self.config_path, self.config)
        self.config = runtime.control.load_config(self.config_path)
        self.state_path = self.directory / 'state.json'
        self.state = {'format_version': 1, 'identity': runtime.identity(self.config, self.binding),
            'checkpoint': 'old-checkpoint', 'session_id': 'old-session', 'session_runs': 2,
            'last_result': {'run_id': 'last-run', 'snapshot_id': 'old-checkpoint', 'outcome': 'waiting'},
            'session_history': [{'session_id': 'older-session'}], 'idle_wakes': 3}
        write(self.state_path, self.state)
        self.paused = self.directory / 'PAUSED'
        self.paused.write_text('迁移期间暂停')
        self.comments = self.directory / 'comments-state.json'
        write(self.comments, {'preserved': 'comment evidence'})
        write(self.directory / 'checkpoint-snapshot.json', {'preserved': 'old observation'})
        self.handoff_path = self.directory / 'runs/last-run/handoff.json'
        self.handoff = {'format_version': 1, 'run_id': 'last-run', 'snapshot_id': 'old-checkpoint',
            'can_start_fresh': True, 'summary': '已核对，等待反馈人确认', 'active_operations': [],
            'open_items': [{'source': 'table/record', 'next_step': '等确认后再开始'}],
            'constraints': [{'source': 'project rules', 'rule': '不能替人确认'}]}
        write(self.handoff_path, self.handoff)
        self.old_binding = self.project.read_bytes()
        self.old_state = self.state_path.read_bytes()
        self.old_comments = self.comments.read_bytes()
        self.old_sha, self.candidate_sha = sha(self.project), sha(self.candidate_path)
        self.scans = []

    def collect(self, config, binding, directory, collector):
        self.scans.append((config, binding, directory))
        self.assertEqual(binding, self.candidate)
        self.assertNotEqual(directory, self.directory)
        self.assertEqual((directory / 'comments-state.json').read_bytes(), self.old_comments)
        write(directory / 'comments-state.json', {'new': 'observation only'})
        return {'format_version': 1, 'binding': runtime.comments.binding_identity(binding),
                'records': {}, 'comments': {}, 'notices': {}}

    def migrate(self, **changes):
        args = {'config_path': self.config_path, 'candidate': self.candidate_path,
                'expected_binding_sha256': self.old_sha,
                'expected_candidate_sha256': self.candidate_sha, 'handoff': self.handoff_path}
        args.update(changes)
        with mock.patch.object(runtime, 'collect_snapshot', side_effect=self.collect):
            return runtime.migrate_binding(**args)

    def assert_unchanged(self):
        self.assertEqual(self.project.read_bytes(), self.old_binding)
        self.assertEqual(self.state_path.read_bytes(), self.old_state)
        self.assertEqual(self.comments.read_bytes(), self.old_comments)
        self.assertTrue(self.paused.is_file())

    def test_success_keeps_checkpoint_comments_history_and_pause(self):
        result = self.migrate()
        self.assertEqual(result['status'], 'migrated_paused')
        self.assertFalse(result['checkpoint_advanced'])
        self.assertFalse(result['codex_started'])
        state = runtime.read_json(self.state_path)
        self.assertEqual(state['identity'], runtime.identity(self.config, self.candidate))
        self.assertEqual(state['checkpoint'], 'old-checkpoint')
        self.assertEqual(state['next_due_at'], 0)
        self.assertIsNone(state['session_id'])
        self.assertEqual([s['session_id'] for s in state['session_history']], ['older-session', 'old-session'])
        self.assertEqual(state['session_handoff']['content'], self.handoff)
        self.assertEqual(self.comments.read_bytes(), self.old_comments)
        self.assertTrue(self.paused.is_file())
        archive = Path(result['backup_path'])
        self.assertEqual((archive / 'binding-before.json').read_bytes(), self.old_binding)
        self.assertEqual((archive / 'state-before.json').read_bytes(), self.old_state)
        self.assertEqual((archive / 'comments-state.json').read_bytes(), self.old_comments)
        self.assertEqual(runtime.read_json(archive / 'migration.json')['status'], 'complete')

    def test_wrong_old_or_candidate_hash_never_reads_remote(self):
        for key in ('expected_binding_sha256', 'expected_candidate_sha256'):
            with self.subTest(key=key), self.assertRaises(runtime.RunnerError):
                self.migrate(**{key: 'wrong'})
            self.assert_unchanged()
        self.assertEqual(self.scans, [])

    def test_running_unpaused_or_unknown_previous_run_is_rejected(self):
        with runtime.lock_workspace(self.config):
            with self.assertRaisesRegex(runtime.RunnerError, '仍在运行'):
                self.migrate()
        for key in ('inflight', 'recovery_note'):
            write(self.state_path, {**self.state, key: {'unknown': True}})
            with self.assertRaises(runtime.RunnerError):
                self.migrate()
        self.state_path.write_bytes(self.old_state)
        self.paused.unlink()
        with self.assertRaisesRegex(runtime.RunnerError, '先暂停'):
            self.migrate()
        self.assertEqual(self.scans, [])

    def test_account_and_changed_binding_identity_are_rejected(self):
        write(self.config_path, {**self.config, 'lark_profile': 'different-account'})
        with self.assertRaisesRegex(runtime.RunnerError, '账号'):
            self.migrate()
        write(self.config_path, self.config)
        for change in ('base', 'project', 'table', 'field', 'removed_field'):
            candidate = copy.deepcopy(self.candidate)
            if change == 'base': candidate['base']['token'] = 'OtherBase'
            elif change == 'project': candidate['scope']['project_record_id'] = 'OtherProject'
            elif change == 'table': candidate['tables']['work']['id'] = 'OtherTable'
            elif change == 'field': candidate['tables']['work']['fields']['title']['id'] = 'OtherField'
            else: del candidate['tables']['work']['fields']['title']
            write(self.candidate_path, candidate)
            with self.subTest(change=change), self.assertRaises(runtime.RunnerError):
                self.migrate(expected_candidate_sha256=sha(self.candidate_path))
        self.assert_unchanged()
        self.assertEqual(self.scans, [])

    def test_uncertain_or_unrelated_handoff_preserves_old_session(self):
        for change in ('missing', 'active', 'wrong-run', 'cannot-start', 'foreign-file'):
            handoff = copy.deepcopy(self.handoff)
            path = self.handoff_path
            if change == 'active': handoff['active_operations'] = ['deployment still running']
            elif change == 'wrong-run': handoff['run_id'] = 'another-run'
            elif change == 'cannot-start': handoff['can_start_fresh'] = False
            elif change == 'foreign-file': path = self.root / 'guessed.json'
            write(path, handoff)
            with self.subTest(change=change), self.assertRaises((runtime.RunnerError, ValueError)):
                self.migrate(handoff=None if change == 'missing' else path)
            self.assert_unchanged()
        self.assertEqual(self.scans, [])

    def test_read_failure_keeps_active_journal_and_archives_evidence(self):
        self.collect = mock.Mock(side_effect=runtime.watch.WatchError('unknown field type'))
        with self.assertRaisesRegex(runtime.RunnerError, '迁移未完成'):
            self.migrate()
        self.assert_unchanged()
        archives = list((self.directory / 'binding-migrations').iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(runtime.read_json(archives[0] / 'migration.json')['status'], 'failed')

    def test_stage_candidate_requires_matching_live_configuration(self):
        from test_browser_watch import stage_field
        import browser_watch
        stage = browser_watch.field(stage_field())
        self.candidate['tables']['work']['fields']['status'] = stage
        write(self.candidate_path, self.candidate)
        self.candidate_sha = sha(self.candidate_path)
        collect = self.collect
        def stage_scan(*args):
            snapshot = collect(*args)
            changed = copy.deepcopy(stage)
            changed['stage_config']['property']['options'][0]['fieldsConfigInfo'] = [{'changed': True}]
            snapshot['stage_fields'] = {'tblWork/fldStage': changed}
            return snapshot
        self.collect = stage_scan
        with self.assertRaises(runtime.RunnerError):
            self.migrate()
        self.assert_unchanged()
        archive = next((self.directory / 'binding-migrations').iterdir())
        self.assertIn('流程配置', runtime.read_json(archive / 'migration.json')['failure_message'])

    def test_concurrent_binding_change_is_not_overwritten(self):
        collect = self.collect
        def changed(*args):
            result = collect(*args)
            self.project.write_text('{"other":"edit"}')
            return result
        self.collect = changed
        with self.assertRaises(runtime.RunnerError):
            self.migrate()
        self.assertEqual(self.project.read_text(), '{"other":"edit"}')
        self.assertEqual(self.state_path.read_bytes(), self.old_state)
        self.assertTrue(self.paused.is_file())

    def test_removed_pause_during_scan_is_restored_with_original_evidence(self):
        before = self.paused.read_bytes()
        collect = self.collect
        def removed(*args):
            result = collect(*args)
            self.paused.unlink()
            return result
        self.collect = removed
        with self.assertRaisesRegex(runtime.RunnerError, '保持暂停'):
            self.migrate()
        self.assert_unchanged()
        archive = next((self.directory / 'binding-migrations').iterdir())
        self.assertEqual((archive / 'pause-before.txt').read_bytes(), before)
        journal = runtime.read_json(archive / 'migration.json')
        self.assertFalse(journal['pause_present_after_failure'])
        self.assertTrue(journal['pause_verified'])
        self.assertIn('迁移未完成', self.paused.read_text())

    def test_interrupted_commit_keeps_pause_and_both_before_after_files(self):
        atomic = runtime.atomic_json
        def fail_state(path, value):
            if path.resolve() == self.state_path.resolve():
                raise OSError('simulated write failure')
            return atomic(path, value)
        with mock.patch.object(runtime, 'atomic_json', side_effect=fail_state):
            with self.assertRaises(runtime.RunnerError):
                self.migrate()
        self.assertEqual(self.state_path.read_bytes(), self.old_state)
        self.assertEqual(self.project.read_bytes(), self.candidate_path.read_bytes())
        self.assertTrue(self.paused.is_file())
        archive = next((self.directory / 'binding-migrations').iterdir())
        self.assertTrue((archive / 'state-after.json').is_file())
        self.assertEqual((archive / 'binding-before.json').read_bytes(), self.old_binding)


if __name__ == '__main__':
    unittest.main()
