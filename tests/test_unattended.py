"""Offline behavior checks for cron-driven work; never invoke real Feishu or Codex."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import unattended as m


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='unattended-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.work = self.root / 'work with spaces'
        self.work.mkdir()
        self.project_path = self.root / 'project.json'
        self.binding = {
            'format_version': 2, 'project_key': 'fixture', 'project_number': 'PRJ-0001',
            'base': {'token': 'FixtureBase'},
            'scope': {'mode': 'project_record', 'project_table_id': 'tblProjects',
                      'project_record_id': 'recProject'},
            'tables': {'projects': {'id': 'tblProjects', 'fields': {'title': {'id': 'fldProjectTitle'}}},
                       'work': {'id': 'tblWork', 'fields': {'title': {'id': 'fldTitle'}}}},
            'documents': {'details': {'token': 'FixtureDoc', 'anchor': 'sectionOne'}},
        }
        write_json(self.project_path, self.binding)
        self.config_path = self.root / 'runner.json'
        self.config = {
            'format_version': 1, 'project_config': str(self.project_path),
            'workdir': str(self.work), 'state_dir': str(self.root / 'private-state'),
            'python_bin': sys.executable, 'codex_bin': sys.executable, 'lark_bin': sys.executable,
            'path': str(Path(sys.executable).parent) + ':/usr/bin:/bin',
            'lark_profile': 'fixture-profile', 'codex_profile': None,
            'interval_minutes': 5, 'run_timeout_seconds': 30, 'max_consecutive_runs': 2,
            'reconcile_interval_seconds': 86400,
        }
        write_json(self.config_path, self.config)
        self.snapshot = {'format_version': 1, 'records': {
            'tblWork/recItem': {'role': 'work', 'fields': {'fldTitle': '未完成事项'}}},
            'comments': {}, 'observed_at': 'first scan'}
        self.timestamp = 100000.0
        self.calls = []
        self.session = str(uuid.uuid4())
        self.outcome = 'waiting'
        self.delay = 0

    def state(self):
        return json.loads((Path(self.config['state_dir']) / 'state.json').read_text())

    def save_config(self):
        write_json(self.config_path, self.config)

    def collect(self, binding, profile, *, comments_state_path, lark_bin=None):
        self.assertEqual(binding, self.binding)
        self.assertEqual(profile, 'fixture-profile')
        self.assertEqual(comments_state_path, Path(self.config['state_dir']) / 'comments-state.json')
        if lark_bin is not None:
            self.assertEqual(Path(lark_bin).resolve(), Path(self.config['lark_bin']).resolve())
        return copy.deepcopy(self.snapshot)

    def execute(self, config, state, run_dir, prompt, lock_fd, save_state):
        self.calls.append({'state': copy.deepcopy(state), 'dir': run_dir, 'prompt': prompt})
        inflight = state['inflight']
        return {'run_id': inflight['run_id'], 'snapshot_id': inflight['snapshot_id'],
                'outcome': self.outcome, 'summary': '已查看，等待回复',
                'next_step': '请补充出错页面', 'retry_after_seconds': self.delay}, self.session

    def tick(self, **kwargs):
        # Prompt rendering is tested separately; state transitions need no actual Skill invocation.
        with mock.patch.object(m, 'build_prompt', return_value='isolated test prompt'):
            return m.tick(self.config_path, collector=kwargs.get('collector', self.collect),
                          executor=kwargs.get('executor', self.execute), clock=lambda: self.timestamp)


class SchedulingTests(RuntimeFixture):
    def test_scan_reports_limited_coverage_without_advancing_checkpoint(self):
        self.snapshot['coverage'] = {'feedback': {
            'mode': 'linked_only', 'unassigned_feedback': 'excluded',
            'message': '只读已关联反馈，未归属反馈未扫描。'}}
        result = m.scan_only(self.config_path, collector=self.collect)
        self.assertEqual(result['status'], 'scanned')
        self.assertEqual(result['coverage'], self.snapshot['coverage'])
        self.assertFalse(result['codex_started'])
        self.assertFalse(result['checkpoint_advanced'])
        state_dir = Path(self.config['state_dir'])
        saved = json.loads((state_dir / 'last-scan.json').read_text())
        self.assertEqual(saved['coverage'], result['coverage'])
        self.assertFalse((state_dir / 'state.json').exists())
        self.assertEqual(self.calls, [])

    def test_scan_saves_private_snapshot_without_consuming_first_or_later_work(self):
        result = m.scan_only(self.config_path, collector=self.collect)
        self.assertEqual(result['status'], 'scanned')
        self.assertFalse(result['codex_started'])
        self.assertFalse(result['checkpoint_advanced'])
        state_dir = Path(self.config['state_dir'])
        self.assertEqual(json.loads((state_dir / 'last-scan.json').read_text()), self.snapshot)
        self.assertFalse((state_dir / 'state.json').exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tick()['status'], 'ran')
        previous = self.state()
        self.snapshot['comments']['new-reply'] = {'revision': 'needs handling'}
        m.scan_only(self.config_path, collector=self.collect)
        self.assertEqual(self.state(), previous)
        self.assertEqual(json.loads((state_dir / 'last-scan.json').read_text()), self.snapshot)
        self.assertEqual(self.tick()['status'], 'ran')

    def test_scan_respects_work_directory_lock_and_skips_collection_when_busy(self):
        collector = mock.Mock(side_effect=AssertionError('busy scans must not collect'))
        with m.lock_workspace(self.config):
            self.assertEqual(m.scan_only(self.config_path, collector=collector)['status'], 'busy')
        collector.assert_not_called()
        self.assertFalse((Path(self.config['state_dir']) / 'last-scan.json').exists())

    def test_first_scan_delivers_existing_backlog_then_unchanged_skips(self):
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(len(self.calls), 1)
        delivered = json.loads((self.calls[0]['dir'] / 'snapshot.json').read_text())
        self.assertEqual(delivered['records'], self.snapshot['records'])
        self.snapshot['observed_at'] = 'later scan'
        self.assertEqual(self.tick()['status'], 'unchanged')
        self.assertEqual(len(self.calls), 1)

    def test_new_reply_during_execution_is_not_swallowed_by_checkpoint(self):
        before = m.digest(m.stable_snapshot(self.snapshot))
        def adds_reply(*args):
            result = self.execute(*args)
            self.snapshot['comments']['replyLate'] = {'revision': 'new reply'}
            return result
        self.assertEqual(self.tick(executor=adds_reply)['status'], 'ran')
        self.assertEqual(self.state()['checkpoint'], before)
        self.assertEqual(self.tick()['status'], 'ran')
        delivered = json.loads((self.calls[-1]['dir'] / 'snapshot.json').read_text())
        self.assertIn('replyLate', delivered['comments'])
        self.assertEqual(len(self.calls), 2)

    def test_continue_runs_again_after_delay_without_table_changes(self):
        self.outcome = 'continue'
        self.assertEqual(self.tick()['status'], 'ran')
        self.timestamp += 299
        self.assertEqual(self.tick()['status'], 'unchanged')
        self.timestamp += 1
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]['state']['session_id'], self.session)

    def test_waiting_can_schedule_environment_recheck_and_zero_delay_does_not_spin(self):
        self.delay = 600
        self.tick()
        self.timestamp += 599
        self.assertEqual(self.tick()['status'], 'unchanged')
        self.timestamp += 1
        self.delay = 0
        self.assertEqual(self.tick()['status'], 'ran')
        self.timestamp += 300
        self.assertEqual(self.tick()['status'], 'unchanged')

    def test_daily_reconciliation_does_not_depend_on_new_comments(self):
        self.tick()
        self.timestamp += 86400
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(len(self.calls), 2)

    def test_partial_scan_never_advances_processing_checkpoint_or_starts_codex(self):
        self.tick()
        checkpoint = self.state()['checkpoint']
        original_path = os.environ.get('PATH')
        def failed_scan(*args, **kwargs):
            raise m.OnboardingError('a reply page could not be read')
        result = self.tick(collector=failed_scan)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['checkpoint_advanced'])
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(os.environ.get('PATH'), original_path)
        self.assertFalse((Path(self.config['state_dir']) / 'PAUSED').exists())

    def test_failed_execution_preserves_checkpoint_and_requires_explicit_recovery(self):
        self.tick()
        checkpoint = self.state()['checkpoint']
        self.snapshot['comments']['reply2'] = {'revision': '2'}
        def failure(*args):
            raise m.RunnerError('operation may already have succeeded')
        result = self.tick(executor=failure)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertIn('inflight', self.state())
        self.assertEqual(self.tick()['status'], 'paused')
        with self.assertRaises(m.RunnerError):
            m.control_state(self.config_path, 'resume')
        m.control_state(self.config_path, 'recover')
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertIn('recovery_note', self.state())
        self.assertEqual(self.tick()['status'], 'ran')

    def test_malformed_result_does_not_mark_a_snapshot_processed(self):
        def malformed(*args):
            result, session = self.execute(*args)
            result['snapshot_id'] = 'another-snapshot'
            return result, session
        self.assertEqual(self.tick(executor=malformed)['status'], 'failed')
        self.assertIsNone(self.state()['checkpoint'])
        self.assertIn('inflight', self.state())
        self.assertEqual(self.tick()['status'], 'paused')

    def test_orphaned_inflight_is_not_blindly_restarted(self):
        self.tick()
        state = self.state()
        state['inflight'] = {'run_id': 'unfinished', 'phase': 'running'}
        write_json(Path(self.config['state_dir']) / 'state.json', state)
        self.assertEqual(self.tick()['status'], 'recovery_required')
        self.assertEqual(len(self.calls), 1)

    def test_recovery_reads_session_from_interrupted_run_without_marking_it_processed(self):
        state_dir = Path(self.config['state_dir'])
        run_dir = state_dir / 'runs' / 'interrupted'
        run_dir.mkdir(parents=True)
        session_id = str(uuid.uuid4())
        (run_dir / 'events.jsonl').write_text(json.dumps({
            'type': 'thread.started', 'thread_id': session_id}) + '\n', encoding='utf-8')
        state = m.load_state(state_dir / 'state.json', m.identity(self.config, self.binding))
        state['inflight'] = {'run_id': 'interrupted', 'directory': str(run_dir)}
        write_json(state_dir / 'state.json', state)
        m.control_state(self.config_path, 'recover')
        recovered = self.state()
        self.assertEqual(recovered['session_id'], session_id)
        self.assertIsNone(recovered['checkpoint'])
        self.assertEqual(recovered['recoveries'][-1]['inflight']['run_id'], 'interrupted')
        self.assertNotIn('inflight', recovered)

    def test_recovery_prompt_keeps_unknown_prior_effects_visible(self):
        previous = {'run_id': 'interrupted', 'directory': '/private/fixture/run'}
        state = {'recovery_note': '先检查已执行的操作', 'recoveries': [{'inflight': previous}]}
        with mock.patch.object(Path, 'read_text', return_value='worker instructions'):
            prompt = m.build_prompt(self.config, state, 'this-run', 'this-snapshot',
                                    self.root / 'snapshot.json', 'continuation')
        context = json.loads(prompt[prompt.index('{'):])
        self.assertEqual(context['recovery_note'], state['recovery_note'])
        self.assertEqual(context['previous_interrupted_run'], previous)

    def test_pause_during_execution_allows_current_work_to_finish(self):
        def pause_while_running(*args):
            paused = m.control_state(self.config_path, 'pause')
            self.assertFalse(paused['active_run_terminated'])
            return self.execute(*args)
        self.assertEqual(self.tick(executor=pause_while_running)['status'], 'ran')
        self.assertNotIn('inflight', self.state())
        self.assertEqual(self.tick()['status'], 'paused')
        m.control_state(self.config_path, 'resume')
        self.assertEqual(self.tick()['status'], 'unchanged')

    def test_resume_after_worker_paused_retries_without_waiting_for_base_changes(self):
        self.outcome = 'paused'
        self.delay = 0
        self.assertEqual(self.tick()['status'], 'ran')
        checkpoint = self.state()['checkpoint']
        self.assertEqual(self.tick()['status'], 'paused')
        m.control_state(self.config_path, 'resume')
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.outcome = 'waiting'
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.state()['checkpoint'], checkpoint)

    def test_binding_change_is_rejected_without_rewriting_old_state(self):
        self.tick()
        old = self.state()
        self.binding['scope']['project_record_id'] = 'recDifferentProject'
        write_json(self.project_path, self.binding)
        with self.assertRaises(m.RunnerError):
            self.tick()
        self.assertEqual(self.state(), old)

    def test_profile_change_does_not_resume_previous_accounts_session(self):
        self.tick()
        self.config['lark_profile'] = 'another-user'
        self.save_config()
        with self.assertRaises(m.RunnerError):
            self.tick()
        self.assertEqual(len(self.calls), 1)

    def test_overlapping_workers_sharing_checkout_do_not_launch_twice(self):
        self.config['state_dir'] = str(self.root / 'other-state')
        self.save_config()
        with m.lock_workspace(self.config) as held:
            self.assertIsNotNone(held)
            self.assertEqual(self.tick()['status'], 'busy')
            with self.assertRaises(m.RunnerError):
                m.control_state(self.config_path, 'recover')
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self.tick()['status'], 'ran')

    def test_repeated_empty_self_wakes_pause_without_dropping_next_change(self):
        self.tick()
        for revision in range(2):
            self.snapshot['comments']['own-reply'] = {'revision': revision}
            self.assertEqual(self.tick()['status'], 'ran')
        checkpoint = self.state()['checkpoint']
        self.snapshot['comments']['human-reply'] = {'revision': 'still pending'}
        self.assertEqual(self.tick()['status'], 'recovery_required')
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertNotEqual(checkpoint, m.digest(m.stable_snapshot(self.snapshot)))

    def test_useful_continuations_are_not_counted_as_empty_self_wakes(self):
        self.outcome = 'continue'
        for revision in range(5):
            self.snapshot['records']['tblWork/recItem']['fields']['fldTitle'] = str(revision)
            self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(self.state()['idle_wakes'], 0)


class BrowserRuntimeTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.cli_config = copy.deepcopy(self.config)
        self.config['format_version'] = 2
        self.config.pop('lark_bin')
        self.config.pop('lark_profile')
        self.config['access'] = {'mode': 'browser', 'node_bin': str(Path(sys.executable).resolve()),
                                 'cdp_url': 'http://127.0.0.1:9222', 'user_id': 'web-user-fixture'}
        self.binding['documents'] = {}
        write_json(self.project_path, self.binding)
        self.save_config()
        self.browser_reader = mock.Mock(side_effect=self.read_browser)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(sys.modules, {'browser_watch': SimpleNamespace(collect=self.browser_reader)}).start()
        self.cli_reader = mock.Mock(side_effect=AssertionError('Browser mode must not invoke Lark CLI.'))

    def read_browser(self, binding, access, *, comments_state_path):
        self.assertEqual(binding, self.binding)
        self.assertEqual(access, self.config['access'])
        self.assertEqual(comments_state_path, Path(self.config['state_dir']) / 'browser-comments-state.json')
        return copy.deepcopy(self.snapshot)

    def run_tick(self):
        return m.tick(self.config_path, collector=self.cli_reader, executor=self.execute,
                      clock=lambda: self.timestamp)

    def test_browser_scan_routes_without_cli_or_checkpoint_and_restores_path(self):
        before_path = os.environ.get('PATH')
        directory = Path(self.config['state_dir'])
        directory.mkdir()
        legacy_journal = directory / 'comments-state.json'
        legacy_journal.write_text('{"legacy_cli_observations":"preserved"}')
        before_journal = legacy_journal.read_bytes()
        result = m.scan_only(self.config_path, collector=self.cli_reader)
        self.assertEqual(result['status'], 'scanned')
        self.assertFalse(result['codex_started'])
        self.assertFalse(result['checkpoint_advanced'])
        self.assertEqual(os.environ.get('PATH'), before_path)
        self.assertEqual(legacy_journal.read_bytes(), before_journal)
        self.browser_reader.assert_called_once()
        self.cli_reader.assert_not_called()
        self.assertFalse((Path(self.config['state_dir']) / 'state.json').exists())

    def test_browser_worker_receives_real_access_and_no_fake_lark_profile(self):
        self.assertEqual(self.run_tick()['status'], 'ran')
        context = json.loads(self.calls[-1]['prompt'].split('本轮本地配置与输入位置：\n', 1)[1])
        self.assertEqual(context['access'], self.config['access'])
        self.assertNotIn('lark_profile', context)
        self.assertNotIn('lark_bin', context['access'])
        self.assertEqual(self.state()['checkpoint'], m.digest(m.stable_snapshot(self.snapshot)))
        self.cli_reader.assert_not_called()

    def test_browser_login_failure_preserves_prior_checkpoint_and_session(self):
        self.run_tick()
        before = self.state()
        self.browser_reader.side_effect = m.OnboardingError('Browser login expired.')
        result = self.run_tick()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['checkpoint_advanced'])
        self.assertEqual(self.state()['checkpoint'], before['checkpoint'])
        self.assertEqual(self.state()['session_id'], before['session_id'])
        self.assertEqual(len(self.calls), 1)

    def test_browser_account_or_endpoint_change_cannot_reuse_prior_state(self):
        self.run_tick()
        state_file = Path(self.config['state_dir']) / 'state.json'
        before = state_file.read_bytes()
        original = copy.deepcopy(self.config['access'])
        for key, value in (('user_id', 'another-user'), ('cdp_url', 'http://127.0.0.1:9333')):
            self.config['access'] = {**original, key: value}
            self.save_config()
            with self.subTest(key=key), self.assertRaises(m.RunnerError):
                self.run_tick()
            self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual(self.browser_reader.call_count, 1)

    def test_switching_back_to_cli_preserves_browser_state_and_requires_reconciliation(self):
        self.run_tick()
        state_file = Path(self.config['state_dir']) / 'state.json'
        before = state_file.read_bytes()
        write_json(self.config_path, self.cli_config)
        with self.assertRaises(m.RunnerError):
            self.run_tick()
        self.assertEqual(state_file.read_bytes(), before)
        self.cli_reader.assert_not_called()

    def test_browser_identity_change_during_scan_never_starts_worker(self):
        def changed(*args, **kwargs):
            snapshot = self.read_browser(*args, **kwargs)
            replacement = copy.deepcopy(self.config)
            replacement['access']['user_id'] = 'another-user'
            write_json(self.config_path, replacement)
            return snapshot

        self.browser_reader.side_effect = changed
        result = self.run_tick()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['checkpoint_advanced'])
        self.assertIsNone(self.state()['checkpoint'])
        self.assertNotIn('inflight', self.state())
        self.assertFalse(self.calls)

    def test_browser_check_is_local_only_and_pause_does_not_open_browser(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = m.main(['check', '--config', str(self.config_path)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result['access'], self.config['access'])
        self.assertFalse(result['remote_access_verified'])
        self.assertFalse(result['cron_installed_verified'])
        m.control_state(self.config_path, 'pause')
        self.assertEqual(self.run_tick()['status'], 'paused')
        self.browser_reader.assert_not_called()

    def test_legacy_cli_identity_hash_is_unchanged(self):
        legacy = {'binding': m.comments.binding_identity(self.binding),
                  'tables': self.binding.get('tables'), 'project_number': self.binding.get('project_number'),
                  'workdir': self.cli_config['workdir'], 'lark_profile': self.cli_config['lark_profile'],
                  'codex_profile': self.cli_config.get('codex_profile')}
        self.assertEqual(m.identity(self.cli_config, self.binding), m.digest(legacy))

    def test_init_parser_requires_no_lark_profile_for_explicit_browser_mode(self):
        argv = ['init', '--config', str(self.config_path), '--project-config', str(self.project_path),
                '--workdir', str(self.work), '--access', 'browser',
                '--browser-cdp-url', 'http://127.0.0.1:9222', '--browser-user-id', 'web-user-fixture',
                '--node-bin', sys.executable]
        args = m.parser().parse_args(argv)
        self.assertEqual(args.access, 'browser')
        self.assertIsNone(args.lark_profile)
        with mock.patch.object(m.control, 'init_config', return_value={'prepared': True}) as initialize:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(argv), 0)
        self.assertEqual(initialize.call_args.kwargs['browser_user_id'], 'web-user-fixture')
        self.assertEqual(initialize.call_args.kwargs['access'], 'browser')


class GovernancePromptTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.rules = self.work / 'AGENTS.md'
        self.rules.write_text('仅本地处理；禁止评论、@ 和外部通知。', encoding='utf-8')
        self.binding['governance'] = {
            'rules_reference': 'AGENTS.md', 'delivery_report_channel': '本轮运行结果',
            'ledger_maintainer': 'project owner',
        }
        write_json(self.project_path, self.binding)

    def run_tick(self, **kwargs):
        return m.tick(self.config_path, collector=kwargs.get('collector', self.collect),
                      executor=kwargs.get('executor', self.execute), clock=lambda: self.timestamp)

    def context(self):
        prompt = self.calls[-1]['prompt']
        return json.loads(prompt.split('本轮本地配置与输入位置：\n', 1)[1])

    def test_first_and_resumed_prompts_include_current_rules_and_governance(self):
        self.assertEqual(self.run_tick()['status'], 'ran')
        context = self.context()
        self.assertEqual(context['governance'], self.binding['governance'])
        self.assertEqual(context['project_rules']['source'], str(self.rules.resolve()))
        self.assertEqual(context['project_rules']['content'], self.rules.read_text(encoding='utf-8'))
        self.assertIsNone(self.calls[0]['state']['session_id'])
        self.rules.write_text('更新约定：仅在批准的文档留言，不得 @。', encoding='utf-8')
        self.binding['governance']['delivery_report_channel'] = '指定文档'
        write_json(self.project_path, self.binding)
        self.timestamp += 86400
        self.assertEqual(self.run_tick()['status'], 'ran')
        self.assertEqual(self.calls[-1]['state']['session_id'], self.session)
        self.assertEqual(self.context()['governance'], self.binding['governance'])
        self.assertEqual(self.context()['project_rules']['content'], self.rules.read_text(encoding='utf-8'))

    def test_recovery_prompt_includes_rules_and_prior_effects(self):
        def interrupted(*args):
            self.execute(*args)
            raise m.RunnerError('interrupted fixture')
        self.assertEqual(self.run_tick(executor=interrupted)['status'], 'failed')
        m.control_state(self.config_path, 'recover')
        self.assertEqual(self.run_tick()['status'], 'ran')
        self.assertIsNotNone(self.context()['recovery_note'])
        self.assertIsNotNone(self.context()['previous_interrupted_run'])
        self.assertEqual(self.context()['project_rules']['content'], self.rules.read_text(encoding='utf-8'))

    def test_absolute_rules_path_and_optional_reference(self):
        self.binding['governance']['rules_reference'] = str(self.rules)
        write_json(self.project_path, self.binding)
        self.assertEqual(self.run_tick()['status'], 'ran')
        self.assertEqual(self.context()['project_rules']['source'], str(self.rules))
        del self.binding['governance']['rules_reference']
        write_json(self.project_path, self.binding)
        self.timestamp += 86400
        self.assertEqual(self.run_tick()['status'], 'ran')
        self.assertEqual(self.context()['governance'], self.binding['governance'])
        self.assertIsNone(self.context()['project_rules'])

    def test_missing_rules_never_executes_or_advances_checkpoint(self):
        self.rules.unlink()
        self.assertEqual(self.run_tick()['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.state()['checkpoint'])
        self.assertNotIn('inflight', self.state())
        self.rules.write_text('恢复规则', encoding='utf-8')
        self.assertEqual(self.run_tick()['status'], 'ran')
        checkpoint = self.state()['checkpoint']
        self.rules.unlink()
        self.snapshot['comments']['new'] = {'revision': 'pending'}
        self.assertEqual(self.run_tick()['status'], 'failed')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertNotIn('inflight', self.state())

    def assert_rebinding_during_collection_is_rejected(self, *, previous_run):
        if previous_run:
            self.assertEqual(self.run_tick()['status'], 'ran')
            checkpoint = self.state()['checkpoint']
        else:
            checkpoint = None
        previous_calls = len(self.calls)
        self.snapshot['comments']['new'] = {'revision': 'pending'}
        def rebind_while_collecting(*args, **kwargs):
            snapshot = self.collect(*args, **kwargs)
            changed = copy.deepcopy(self.binding)
            changed['scope']['project_record_id'] = 'recOtherProject'
            del changed['governance']['rules_reference']
            write_json(self.project_path, changed)
            return snapshot
        result = self.run_tick(collector=rebind_while_collecting)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['checkpoint_advanced'])
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertNotIn('inflight', self.state())
        self.assertFalse((Path(self.config['state_dir']) / 'PAUSED').exists())

    def test_initial_collection_rebinding_never_executes_old_snapshot_with_new_rules(self):
        self.assert_rebinding_during_collection_is_rejected(previous_run=False)

    def test_later_collection_rebinding_preserves_checkpoint(self):
        self.assert_rebinding_during_collection_is_rejected(previous_run=True)

    def assert_intake_change_during_collection_is_rejected(self, *, previous_run):
        if previous_run:
            self.assertEqual(self.run_tick()['status'], 'ran')
            checkpoint = self.state()['checkpoint']
        else:
            checkpoint = None
        previous_calls = len(self.calls)
        self.snapshot['comments']['new'] = {'revision': 'pending'}

        def change_intake_while_collecting(*args, **kwargs):
            snapshot = self.collect(*args, **kwargs)
            changed = copy.deepcopy(self.binding)
            changed['feedback_intake'] = {'mode': 'linked_only'}
            write_json(self.project_path, changed)
            return snapshot

        result = self.run_tick(collector=change_intake_while_collecting)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['checkpoint_advanced'])
        self.assertEqual(len(self.calls), previous_calls)
        self.assertEqual(self.state()['checkpoint'], checkpoint)
        self.assertNotIn('inflight', self.state())

    def test_initial_collection_intake_change_does_not_execute_old_coverage(self):
        self.assert_intake_change_during_collection_is_rejected(previous_run=False)

    def test_later_collection_intake_change_preserves_checkpoint(self):
        self.assert_intake_change_during_collection_is_rejected(previous_run=True)

    def test_invalid_or_non_file_reference_fails_closed(self):
        for reference in ['', None, 42, str(self.work), 'https://example.com/rules']:
            with self.subTest(reference=reference):
                self.binding['governance']['rules_reference'] = reference
                write_json(self.project_path, self.binding)
                self.assertEqual(self.run_tick()['status'], 'failed')
                self.assertEqual(self.calls, [])
                self.assertIsNone(self.state()['checkpoint'])
                self.assertNotIn('inflight', self.state())

    def test_unreadable_or_invalid_utf8_rules_fail_without_starting(self):
        original_read = Path.read_text
        def denied(path, *args, **kwargs):
            if path == self.rules:
                raise PermissionError('fixture permission denied')
            return original_read(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', denied):
            result = self.run_tick()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('governance.rules_reference', result['message'])
        self.rules.write_bytes(b'\xff\xfe')
        self.assertEqual(self.run_tick()['status'], 'failed')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(['check', '--config', str(self.config_path)]), 1)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.state()['checkpoint'])
        self.assertNotIn('inflight', self.state())

    def test_malformed_governance_fails_clearly_and_absent_governance_is_optional(self):
        for value in [None, [], 'AGENTS.md']:
            with self.subTest(value=value):
                self.binding['governance'] = value
                write_json(self.project_path, self.binding)
                result = self.run_tick()
                self.assertEqual(result['status'], 'failed')
                self.assertIn('governance', result['message'])
        self.assertEqual(self.calls, [])
        del self.binding['governance']
        write_json(self.project_path, self.binding)
        self.assertEqual(self.run_tick()['status'], 'ran')
        self.assertEqual(self.context()['governance'], {})
        self.assertIsNone(self.context()['project_rules'])

    def test_check_rejects_missing_project_scope_or_table_mapping(self):
        for target, key in [('scope', 'project_table_id'), ('tables', 'projects')]:
            with self.subTest(target=target, key=key):
                incomplete = copy.deepcopy(self.binding)
                del incomplete[target][key]
                write_json(self.project_path, incomplete)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(m.main(['check', '--config', str(self.config_path)]), 1)
        self.assertEqual(self.calls, [])

    def test_check_reads_rules_locally_without_remote_collection(self):
        with mock.patch.object(m, 'collect_snapshot', side_effect=AssertionError('local check only')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(['check', '--config', str(self.config_path)]), 0)
            self.rules.unlink()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(['check', '--config', str(self.config_path)]), 1)
        self.assertEqual(self.calls, [])

    def test_check_reports_linked_only_coverage_without_reading_feishu(self):
        self.binding['feedback_intake'] = {'mode': 'linked_only'}
        self.binding['tables']['feedback'] = {'id': 'tblFeedback', 'fields': {
            'project': {'id': 'fldProject', 'type': 'link', 'target_table_id': 'tblProjects'}}}
        write_json(self.project_path, self.binding)
        output = io.StringIO()
        with mock.patch.object(m, 'collect_snapshot', side_effect=AssertionError('local check only')):
            with contextlib.redirect_stdout(output):
                self.assertEqual(m.main(['check', '--config', str(self.config_path)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['coverage']['feedback']['mode'], 'linked_only')
        self.assertEqual(result['coverage']['feedback']['unassigned_feedback'], 'excluded')
        self.assertTrue(result['coverage']['feedback']['message'])
        self.assertFalse((Path(self.config['state_dir']) / 'state.json').exists())
        self.assertEqual(self.calls, [])


class CodexProcessTests(RuntimeFixture):
    def fake_codex(self, body):
        path = self.root / 'fake-codex.py'
        path.write_text('#!' + sys.executable + '\n' + body, encoding='utf-8')
        path.chmod(0o700)
        self.config['codex_bin'] = str(path)
        self.save_config()
        return path

    def test_command_uses_exact_session_and_keeps_existing_sandbox(self):
        self.config['codex_profile'] = 'bounded-worker'
        command = m.codex_command(self.config, {'session_id': self.session}, self.root)
        self.assertEqual(command[:3], [self.config['codex_bin'], '--profile', 'bounded-worker'])
        self.assertEqual(command[command.index('resume') + 1], self.session)
        self.assertIn('approval_policy="never"', command)
        self.assertNotIn('--last', command)
        self.assertFalse(any('sandbox' in arg or 'bypass' in arg or 'full-auto' in arg for arg in command))
        self.assertEqual(command[-1], '-')

    def test_real_subprocess_jsonl_and_result_roundtrip(self):
        self.fake_codex("""import json, pathlib, sys
prompt = sys.stdin.read()
context = json.loads(prompt)
result_path = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
result = dict(context, outcome='waiting', summary='等待补充页面', next_step='请提供页面地址', retry_after_seconds=0)
result_path.write_text(json.dumps(result))
print(json.dumps({'type': 'thread.started', 'thread_id': '11111111-1111-4111-8111-111111111111'}))
print(json.dumps({'type': 'turn.completed'}))
""")
        def prompt(config, state, run_id, snapshot_id, *args):
            return json.dumps({'run_id': run_id, 'snapshot_id': snapshot_id})
        with mock.patch.object(m, 'build_prompt', side_effect=prompt):
            result = m.tick(self.config_path, collector=self.collect, clock=lambda: self.timestamp)
        self.assertEqual(result['status'], 'ran')
        self.assertEqual(self.state()['session_id'], '11111111-1111-4111-8111-111111111111')
        self.assertNotIn('inflight', self.state())

    def test_failed_first_process_keeps_its_real_session_for_recovery(self):
        self.fake_codex("""import json, sys
sys.stdin.read()
print(json.dumps({'type': 'thread.started', 'thread_id': '22222222-2222-4222-8222-222222222222'}), flush=True)
print(json.dumps({'type': 'turn.failed', 'error': 'fixture'}), flush=True)
raise SystemExit(1)
""")
        self.assertEqual(self.tick(executor=m.execute)['status'], 'failed')
        m.control_state(self.config_path, 'recover')
        self.assertEqual(self.state()['session_id'], '22222222-2222-4222-8222-222222222222')
        self.assertIsNone(self.state()['checkpoint'])

    def test_result_file_without_completed_event_is_not_success(self):
        self.fake_codex("""import json, pathlib, sys
sys.stdin.read()
pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{}')
print(json.dumps({'type': 'thread.started', 'thread_id': '33333333-3333-4333-8333-333333333333'}))
""")
        self.assertEqual(self.tick(executor=m.execute)['status'], 'failed')
        self.assertIsNone(self.state()['checkpoint'])

    def test_failed_event_overrides_completed_event_even_with_zero_exit_code(self):
        self.fake_codex("""import json, sys
sys.stdin.read()
print(json.dumps({'type': 'thread.started', 'thread_id': '33333333-3333-4333-8333-333333333333'}))
print(json.dumps({'type': 'turn.completed'}))
print(json.dumps({'type': 'turn.failed', 'error': 'fixture'}))
""")
        self.assertEqual(self.tick(executor=m.execute)['status'], 'failed')
        self.assertIsNone(self.state()['checkpoint'])

    def test_resumed_process_cannot_switch_to_another_session(self):
        self.tick()
        self.snapshot['comments']['new'] = {'revision': '1'}
        self.fake_codex("""import json, sys
sys.stdin.read()
print(json.dumps({'type': 'thread.started', 'thread_id': '44444444-4444-4444-8444-444444444444'}))
print(json.dumps({'type': 'turn.completed'}))
""")
        checkpoint = self.state()['checkpoint']
        self.assertEqual(self.tick(executor=m.execute)['status'], 'failed')
        self.assertEqual(self.state()['session_id'], self.session)
        self.assertEqual(self.state()['checkpoint'], checkpoint)

    def test_timeout_stops_owned_process_and_does_not_advance_checkpoint(self):
        pidfile = self.root / 'pid'
        self.fake_codex('import os, pathlib, sys, time\n'
                        'sys.stdin.read()\n'
                        f'pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
                        'time.sleep(30)\n')
        self.config['run_timeout_seconds'] = 1
        self.save_config()
        self.assertEqual(self.tick(executor=m.execute)['status'], 'failed')
        self.assertIsNone(self.state()['checkpoint'])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_inherited_lock_blocks_new_worker_after_parent_exits(self):
        # A tiny parent exits while its child keeps the same lock descriptor open.
        code = (
            'import json, subprocess, sys\n'
            f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
            'import unattended as m\n'
            f'config = json.loads({json.dumps(self.config)!r})\n'
            'with m.lock_workspace(config) as fd:\n'
            '    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], '
            'pass_fds=(fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
            '    print(child.pid, flush=True)\n'
        )
        parent = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(parent.returncode, 0, parent.stderr)
        child_pid = int(parent.stdout.strip())
        try:
            with m.lock_workspace(self.config) as fd:
                self.assertIsNone(fd)
            self.assertEqual(self.tick()['status'], 'busy')
        finally:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class ResultContractTests(unittest.TestCase):
    def test_only_complete_well_typed_results_can_advance_a_snapshot(self):
        valid = {'run_id': 'run', 'snapshot_id': 'snapshot', 'outcome': 'waiting',
                 'summary': '等待回复', 'next_step': '请补充页面地址', 'retry_after_seconds': 0}
        self.assertEqual(m.validate_result(valid, 'run', 'snapshot'), valid)
        cases = [dict(valid, run_id='other'), dict(valid, outcome='done'),
                 dict(valid, retry_after_seconds=True), dict(valid, retry_after_seconds=-1),
                 dict(valid, retry_after_seconds=86401), dict(valid, summary=' '),
                 dict(valid, next_step=None), dict(valid, unexpected='field')]
        missing = dict(valid)
        del missing['next_step']
        cases.extend([missing, []])
        for result in cases:
            with self.subTest(result=result):
                with self.assertRaises(m.RunnerError):
                    m.validate_result(result, 'run', 'snapshot')


if __name__ == '__main__':
    unittest.main()
