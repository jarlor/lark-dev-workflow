import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SERVICE = Path(__file__).resolve().parents[1] / 'service'
sys.path.insert(0, str(SERVICE))
import event_bridge as bridge


def idle_pair_child(pins, sender, timeout):
    time.sleep(timeout + 20)


def good_pair_child(pins, sender, timeout):
    sender.send_bytes(b'tenant_from_event')
    time.sleep(timeout + 20)


class EventBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config_path = self.root / 'events.json'
        self.runner = self.root / 'runner.json'
        self.binding = self.root / 'binding.json'
        self.config = dict(app_id='cli_app', tenant_key='tenant', bot_open_id='ou_assistant',
                           base_token='base', project_record_id='recProject', lark_profile='assistant',
                           table_ids=['tblA'], state_dir=str(self.root / 'private'))
        self.project = dict(cli_actor={'identity': 'bot', 'profile': 'assistant', 'app_id': 'cli_app',
                                       'open_id': 'ou_assistant'}, base={'token': 'base'},
                            scope={'project_record_id': 'recProject'}, tables={'projects': {'id': 'tblA'}})
        self.binding.write_text(json.dumps(self.project))
        self.runner_config = dict(format_version=3, lark_profile='assistant', workdir=str(self.root),
                                  state_dir=str(self.root / 'work-state'), project_config=str(self.binding))
        self.runner.write_text(json.dumps(self.runner_config))
        self.config_path.write_text(json.dumps(self.config))
        self.journal = bridge.Journal(self.config)

    def record(self, identifier='evt1'):
        return dict(schema='2.0', header=dict(app_id='cli_app', tenant_key='tenant',
                    event_type=bridge.RECORD, event_id=identifier), event=dict(file_token='base',
                    table_id='tblA', file_type='bitable', operator_id={'open_id': 'ou_human'},
                    action_list=[{'record_id': 'recOther', 'action': 'record_edited',
                                  'after_value': [{'field_id': 'description',
                                                   'field_value': 'private detail; do not persist'}]}]))

    def comment(self):
        envelope = self.record('evt2')
        envelope['header']['event_type'] = bridge.COMMENT
        envelope['event'] = dict(comment_id='c', reply_id='r', is_mentioned=False, notice_meta=dict(
            file_type='bitable', file_token='base', from_user_id={'open_id': 'ou_person'},
            to_user_id={'open_id': 'ou_assistant'}, notice_type='add_reply'))
        return envelope

    def test_receiver_publishes_its_own_epoch_under_the_active_lock(self):
        path = Path(self.config['state_dir']) / 'receiver.lock'
        path.write_text('12345678901234567890\n')
        def check_lease(config, journal):
            self.assertEqual(path.read_text(), f'{journal.started_at}\n')
            with self.assertRaises(ValueError), bridge.exclusive(path):
                self.fail('Multiple receivers acquired the same lease')
        with mock.patch.object(sys, 'argv', ['bridge', '_receive', '--config', str(self.config_path)]), \
                mock.patch.object(bridge, 'socket_client', side_effect=check_lease) as client:
            previous = os.umask(0o077)
            try:
                self.assertEqual(bridge.main(), 0)
            finally:
                os.umask(previous)
        client.assert_called_once()
        with bridge.exclusive(path):
            pass

    def test_deferred_delivery_runs_after_success_only_and_reuses_installed_package(self):
        listener = mock.Mock()
        listener.poll.return_value = None
        pins = (str(self.root), str(self.root / 'runner-state'), str(self.binding))
        def after(status):
            return bridge.retry_after_tick(self.config_path, self.config, pins, status, listener)
        with mock.patch.object(bridge.subprocess, 'Popen') as start, \
                mock.patch.object(bridge, '__file__', str(self.root / 'custom-entry.py')), \
                mock.patch.dict(os.environ, {'FEISHU_APP_SECRET': 'test-only'}):
            self.assertIsNone(after('unchanged'))
            queue = Path(self.config['state_dir']) / 'pending-plan-notices'
            queue.mkdir(mode=0o700)
            (queue / ('a' * 64 + '.json')).touch(mode=0o600)
            for status in ('paused', 'recovery_required', 'busy', 'failed'):
                self.assertIsNone(after(status))
            listener.poll.return_value = 1
            self.assertIsNone(after('ran'))
            start.assert_not_called()
            listener.poll.return_value = None
            self.assertIs(after('unchanged'), start.return_value)
            command, = start.call_args.args
            self.assertEqual(command[1], str(SERVICE / 'deferred_notices.py'))
            self.assertNotIn('FEISHU_APP_SECRET', start.call_args.kwargs['env'])

    def test_target_other_app_table_tenant_assistant_and_false_notices_are_filtered(self):
        changes = [(('header', 'app_id'), 'other'), (('header', 'tenant_key'), 'other'),
                   (('header', 'event_type'), 'im.message.receive_v1'), (('event', 'file_token'), 'other'),
                   (('event', 'table_id'), 'tblB'), (('event', 'file_type'), 'docx'),
                   (('event', 'operator_id'), {'open_id': 'ou_assistant'})]
        for (outer, inner), new in changes:
            with self.subTest(key=inner):
                record = self.record()
                record[outer][inner] = new
                self.assertFalse(bridge.receive(record, self.config, self.journal))
        for key, value in (('to_user_id', {'open_id': 'ou_person'}),
                           ('from_user_id', {'open_id': 'ou_assistant'}), ('notice_type', 'other'),
                           ('file_token', 'foreign'), ('file_type', 'docx')):
            envelope = self.comment()
            envelope['event']['notice_meta'][key] = value
            self.assertFalse(bridge.receive(envelope, self.config, self.journal))
        self.assertEqual(self.journal.status()['received'], 0)

    def test_crash_replay_and_later_events_are_preserved(self):
        self.assertTrue(bridge.receive(self.record(), self.config, self.journal))
        before_tick = self.journal.status()['received']
        restarted = bridge.Journal(self.config)
        self.assertFalse(bridge.receive(self.record(), self.config, restarted))
        self.assertTrue(bridge.receive(self.comment(), self.config, restarted))
        restarted.finish(before_tick, 'busy')
        self.assertEqual(restarted.status()['acknowledged'], 0)
        restarted.finish(before_tick, 'ran')
        self.assertEqual(restarted.status()['received'] - restarted.status()['acknowledged'], 1)
        self.assertEqual(restarted.status()['notices'][bridge.COMMENT]['received'], 1)
        self.assertNotIn(b'private detail', restarted.path.read_bytes())
        for key in ('tenant_key', 'bot_open_id', 'project_record_id', 'base_token', 'app_id', 'lark_profile'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.Journal(dict(self.config, **{key: 'different'}))

    def test_cap_retention_without_dropping_pending_reconcile(self):
        with mock.patch.object(bridge, 'RETENTION', 3):
            for idx in range(20):
                self.journal.mark(str(idx))
            self.assertFalse(self.journal.mark('19'))
            with self.journal.connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM seen').fetchone()[0], 3)
            self.assertEqual(self.journal.status()['received'], 20)

    def test_observe_never_ticks_and_dispatch_reconciles_without_events(self):
        loaded = bridge.config_from(self.config_path)
        clean = dict(received=12, acknowledged=12)
        dirty = dict(received=13, acknowledged=12)
        self.assertFalse(bridge.Schedule(loaded).due(dirty, 999999))
        schedule = bridge.Schedule(dict(loaded, mode='dispatch'))
        self.assertTrue(schedule.due(clean, 100))
        schedule.finished('unchanged', 100)
        self.assertFalse(schedule.due(dirty, 101))
        self.assertTrue(schedule.due(dirty, 106))
        self.assertTrue(schedule.due(clean, 701))
        schedule.finished('failed', 702)
        self.assertFalse(schedule.due(dirty, 705))
        self.assertTrue(schedule.due(clean, 763))

    def set_dispatch(self, cmd=None):
        argv = cmd or [sys.executable, 'sample-tick', '{runner_config}']
        self.config.update(mode='dispatch', runner_config=str(self.runner), tick_argv=argv)
        self.config_path.write_text(json.dumps(self.config))

    def test_refuses_other_runner_project_or_personal_identity(self):
        self.set_dispatch()
        self.assertEqual(bridge.config_from(self.config_path)['mode'], 'dispatch')
        for key, value in [('scope', {'project_record_id': 'other'}), ('base', {'token': 'other'}),
                           ('cli_actor', {'identity': 'user'}),
                           ('cli_actor', dict(self.project['cli_actor'], profile='shared')),
                           ('tables', {'projects': {'id': 'tblOther'}})]:
            self.binding.write_text(json.dumps(dict(self.project, **{key: value})))
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.config_from(self.config_path)
        self.binding.write_text(json.dumps(self.project))
        self.runner.write_text(json.dumps(dict(self.runner_config, format_version=2)))
        with self.assertRaises(ValueError):
            bridge.config_from(self.config_path)

    def test_digest_is_opt_in_and_dispatch_only(self):
        self.assertFalse(bridge.config_from(self.config_path).get('human_digest'))
        self.config_path.write_text(json.dumps(dict(self.config, human_digest=True)))
        with self.assertRaises(ValueError):
            bridge.config_from(self.config_path)
        self.set_dispatch()
        for value in (True, 'yes'):
            self.config_path.write_text(json.dumps(dict(self.config, human_digest=value)))
            if value is True:
                self.assertTrue(bridge.config_from(self.config_path)['human_digest'])
            else:
                with self.assertRaises(ValueError):
                    bridge.config_from(self.config_path)

    def test_digest_does_not_overlap_and_retries_only_unsettled_slots(self):
        schedule = bridge.DigestSchedule(self.config_path, self.config, (str(self.root),))
        with mock.patch.object(bridge.subprocess, 'Popen') as start:
            child = start.return_value
            child.poll.return_value = None
            schedule.poll(3600, 10)
            schedule.poll(7200, 12)
            self.assertEqual(start.call_count, 1)
            child.poll.return_value = 0
            schedule.poll(7201, 13)
            self.assertEqual(start.call_count, 2)
            child.poll.return_value = 1
            schedule.poll(7202, 14)
            schedule.poll(7240, 52)
            self.assertEqual(start.call_count, 2)
            schedule.poll(7502, 314)
            child.poll.return_value = 0
            schedule.poll(7503, 315)
            schedule.poll(10799, 1811)
            self.assertEqual(start.call_count, 3)
            schedule.poll(10800, 1812)
            self.assertEqual(start.call_count, 4)

    def test_digest_checks_latest_work_at_nine_and_skips_the_night(self):
        schedule = bridge.DigestSchedule(self.config_path, self.config, (str(self.root),))
        with mock.patch.object(bridge.subprocess, 'Popen') as start:
            start.return_value.poll.return_value = 0
            for ts in (3599, 3600, 3601, 13 * 3600, 14 * 3600 - 1, 14 * 3600, 16 * 3600,
                       86400 + 3599, 86400 + 3600):
                schedule.poll(ts, ts)
            self.assertEqual(start.call_count, 3)
        schedule = bridge.DigestSchedule(self.config_path, self.config, (str(self.root),))
        with mock.patch.object(bridge.subprocess, 'Popen') as start:
            schedule.poll(3600 + 240, 0)
            start.assert_not_called()
            schedule.poll(7200, 3360)
            start.assert_called_once()

    def test_existing_service_state_cannot_change_worker_checkout_after_restart(self):
        self.set_dispatch()
        bridge.Journal(self.config)
        self.runner.write_text(json.dumps(dict(self.runner_config, workdir=str(self.root / 'other-worker'))))
        with self.assertRaisesRegex(ValueError, 'another runner'):
            bridge.Journal(self.config)

    def test_results_only_ack_successful_read(self):
        for payload, code, status in [('{"status":"busy"}', 0, 'busy'), ('{"status":"unknownvalue"}', 0, 'unknown'),
                                      ('{"status":"ran"}', 1, 'failed'), ('not JSON', 0, 'unknown'),
                                      ('{"status":"unchanged"}', 0, 'unchanged')]:
            stream = io.BytesIO(payload.encode())
            stream.seek(0, 2)
            self.assertEqual(bridge.tick_status(stream, code), status)

    def test_tick_messages_are_clear_for_service_watchers(self):
        expected = {
            'ran': 'Project check: completed one pass.',
            'unchanged': 'Project check: no new items.',
            'busy': 'Project check: the previous run is still active; skipping a duplicate.',
            'paused': 'Project check: paused; no action taken.',
            'recovery_required': 'Project check: resume unfinished work before continuing.',
            'failed': 'Project check failed: this run did not finish; will retry.',
            'unknown': 'Project check: result unclear; inspect nearby log lines.',
        }
        for status, message in expected.items():
            with self.subTest(status=status):
                self.assertEqual(bridge.tick_message(status), message)

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'optional service SDK is not installed')
    def test_pair_only_yields_tenant_from_matching_sdk_record_action(self):
        import lark_oapi as sdk
        observed = []
        pins = {'app_id': 'cli_app', 'table_id': 'tblA', 'base_token': 'base'}
        handler = bridge.pair_dispatcher(sdk, pins, observed.append)
        for outer, key, value in [('header', 'app_id', 'foreign'), ('header', 'event_id', ''),
                                  ('header', 'tenant_key', ''), ('header', 'tenant_key', 'invalid\n'),
                                  ('event', 'file_type', 'docx'), ('event', 'file_token', 'foreign'),
                                  ('event', 'table_id', 'unknown'), ('event', 'action_list', []),
                                  ('event', 'action_list', [{'record_id': 'recX', 'action': 'unknown'}])]:
            envelope = self.record()
            envelope[outer][key] = value
            handler._do_without_validation(json.dumps(envelope).encode())
        self.assertEqual(observed, [])
        handler._do_without_validation(json.dumps(self.record()).encode())
        self.assertEqual(observed, ['tenant'])

    def test_pair_timeout_success_and_subprocess_cleanup(self):
        pins = {'app_id': 'cli_app', 'table_id': 'tblA', 'base_token': 'base'}
        original = {proc.pid for proc in multiprocessing.active_children()}
        for timeout in (0, 901):
            with self.assertRaises(ValueError):
                bridge.pair(pins, timeout, good_pair_child)
        self.assertEqual(bridge.pair(pins, 1, idle_pair_child), {'status': 'timed_out'})
        self.assertEqual(bridge.pair(pins, 5, good_pair_child),
                         {'status': 'paired', 'tenant_key': 'tenant_from_event'})
        self.assertEqual({proc.pid for proc in multiprocessing.active_children()} - original, set())

    def test_pair_cli_needs_no_journal_or_worker_and_fails_without_receiver_credential(self):
        workdir = self.root / 'pair-test'
        workdir.mkdir()
        result = subprocess.run([sys.executable, str(SERVICE / 'event_bridge.py'), 'pair',
                                '--app-id', 'cli_app', '--base-token', 'base', '--table-id', 'tblA',
                                '--timeout', '12'], cwd=workdir, capture_output=True, timeout=15,
                                env=dict(os.environ, CREDENTIALS_DIRECTORY=str(self.root / 'not-configured')))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'status': 'credential_unavailable'})
        self.assertEqual(list(workdir.iterdir()), [])

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'optional service SDK is not installed')
    def test_official_sdk_event_decode_without_network(self):
        import lark_oapi as sdk
        handler = bridge.dispatcher(sdk, self.config, self.journal)
        top_level = self.comment()
        top_level['header']['event_id'] = 'evt3'
        for key in ('file_token', 'file_type'):
            top_level['event'][key] = top_level['event']['notice_meta'].pop(key)
        for envelope in (self.record(), self.comment(), top_level, top_level):
            self.assertIsNone(handler._do_without_validation(json.dumps(envelope).encode()))
        self.assertEqual(self.journal.status()['received'], 3)
        for override in ('file_token', 'file_type'):
            conflicted = self.comment()
            conflicted['header']['event_id'] = 'evt4'
            conflicted['event'][override] = 'foreign'
            handler._do_without_validation(json.dumps(conflicted).encode())
        wrong_recipient = self.comment()
        wrong_recipient['header']['event_id'] = 'evt5'
        wrong_recipient['event']['notice_meta']['to_user_id'] = {'open_id': 'other'}
        own = self.record('evt6')
        own['event']['operator_id'] = {'open_id': 'ou_assistant'}
        wrong_tenant = self.record('evt7')
        wrong_tenant['header']['tenant_key'] = 'foreign'
        wrong_app = self.record('evt8')
        wrong_app['header']['app_id'] = 'foreign'
        for envelope in (own, wrong_tenant, wrong_app, wrong_recipient):
            handler._do_without_validation(json.dumps(envelope).encode())
        self.assertEqual(self.journal.status()['received'], 3)

    def test_supervisor_keeps_configured_worker_home(self):
        supplied_home = self.root / 'service-home'
        supplied_home.mkdir()
        self.assert_worker_environment(str(supplied_home), str(supplied_home))

    def test_supervisor_sets_missing_worker_home_from_its_effective_uid(self):
        self.assert_worker_environment(None, pwd.getpwuid(os.geteuid()).pw_dir)

    def test_missing_home_resolves_effective_uid_even_with_other_login_names(self):
        with mock.patch.dict(os.environ, {'USER': 'someone-else', 'LOGNAME': 'someone-else'}, clear=True), \
                mock.patch.object(os, 'geteuid', return_value=54321), \
                mock.patch.object(pwd, 'getpwuid', return_value=mock.Mock(pw_dir='/home/service')) as lookup:
            self.assertEqual(bridge.worker_environment()['HOME'], '/home/service')
            lookup.assert_called_once_with(54321)

    def assert_worker_environment(self, supplied_home, expected_home):
        worker = self.root / 'test_tick.py'
        worker.write_text("import os,sys, pathlib, json\n"
                          "assert 'FEISHU_APP_SECRET' not in os.environ and 'CREDENTIALS_DIRECTORY' not in os.environ\n"
                          "pathlib.Path('tick-log').write_text(json.dumps([sys.argv[1], os.environ['HOME']]))\n"
                          "print(json.dumps({'status':'unchanged'}))\n")
        self.set_dispatch([sys.executable, str(worker), '{runner_config}'])
        env = dict(os.environ, FEISHU_APP_SECRET='fixture', CREDENTIALS_DIRECTORY=str(self.root / 'missing'))
        if supplied_home is None:
            env.pop('HOME', None)
        else:
            env['HOME'] = supplied_home
        with subprocess.Popen([sys.executable, str(SERVICE / 'event_bridge.py'), 'listen',
                              '--config', str(self.config_path)], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as supervisor:
            try:
                deadline = time.monotonic() + 12
                while self.journal.status()['last_tick_status'] == 'never' and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertEqual(self.journal.status()['last_tick_status'], 'unchanged')
                self.assertEqual(json.loads((self.root / 'tick-log').read_text()), [str(self.runner), expected_home])
                self.assertIsNone(supervisor.poll())
            finally:
                supervisor.terminate()
                supervisor.wait(timeout=15)
        self.assertEqual(supervisor.returncode, 0)


if __name__ == '__main__':
    unittest.main()
