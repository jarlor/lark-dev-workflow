"""End-to-end scheduler shortcuts with synthetic collectors and executors only."""
import copy
import json
from pathlib import Path
from unittest import mock

from test_unattended import RuntimeFixture, write_json, m


class EfficiencyTests(RuntimeFixture):
    def sidecar(self, run_dir, result, *, active=None):
        write_json(run_dir / 'handoff.json', {
            'format_version': 1, 'run_id': result['run_id'], 'snapshot_id': result['snapshot_id'],
            'can_start_fresh': True, 'summary': 'Waiting for feedback confirmation.',
            'open_items': [{'source': 'tblWork/recItem', 'next_step': 'Check exact scope confirmation.'}],
            'constraints': [{'source': 'rules.md', 'rule': 'Production actions remain stopped.'}],
            'active_operations': active or [],
        })

    def own_write(self, *args):
        result, session = self.execute(*args)
        run_dir = args[2]
        old = self.snapshot['records']['tblWork/recItem']['fields']['fldTitle']
        self.snapshot['records']['tblWork/recItem']['fields']['fldTitle'] = '已核对的更新'
        write_json(run_dir / 'effects.json', {
            'format_version': 1, 'run_id': result['run_id'], 'snapshot_id': result['snapshot_id'],
            'record_fields': [{'record_key': 'tblWork/recItem', 'field_id': 'fldTitle',
                               'before': old, 'after': '已核对的更新',
                               'readback_evidence': 'tool-result:verified-specific-field'}],
            'comments': [],
        })
        return result, session

    def test_delta_uses_checkpoint_not_latest_scan_and_retains_original(self):
        self.tick()
        self.snapshot['records']['tblWork/recItem']['fields']['fldTitle'] = 'New title'
        m.scan_only(self.config_path, collector=self.collect)
        self.tick()
        run_dir = self.calls[-1]['dir']
        packet = json.loads((run_dir / 'work-packet.json').read_text())
        self.assertEqual(packet['mode'], 'delta')
        changed = packet['records']['modified']['tblWork/recItem']
        self.assertEqual(changed[0]['before'], '未完成事项')
        self.assertEqual(changed[0]['after'], 'New title')
        self.assertTrue((run_dir / 'previous-snapshot.json').is_file())

    def test_verified_own_fields_do_not_wake_model_or_repeat_consumption(self):
        self.tick(executor=self.own_write)
        old = self.state()['checkpoint']
        self.assertEqual(old, m.digest(m.stable_snapshot(json.loads(
            (self.calls[0]['dir'] / 'snapshot.json').read_text()))))
        result = self.tick()
        self.assertEqual(result['status'], 'unchanged')
        self.assertTrue(result['own_writes_verified'])
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.state()['checkpoint'], old)
        self.assertFalse(self.tick()['own_writes_verified'])

    def test_human_reply_arriving_with_own_write_still_runs(self):
        self.tick(executor=self.own_write)
        self.snapshot['comments']['humanReply'] = {'revision': 'new human reply; same logged-in account'}
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(len(self.calls), 2)
        self.assertIn('humanReply', json.loads((self.calls[-1]['dir'] / 'snapshot.json').read_text())['comments'])

    def test_concurrent_change_to_same_field_is_not_swallowed(self):
        self.tick(executor=self.own_write)
        self.snapshot['records']['tblWork/recItem']['fields']['fldTitle'] = 'Human corrected title'
        self.assertEqual(self.tick()['status'], 'ran')

    def test_self_only_never_suppresses_due_continuation_or_daily_review(self):
        for reason in ('continue', 'reconcile'):
            with self.subTest(reason=reason):
                # Distinct private state for each independent scenario.
                self.config['state_dir'] = str(self.root / reason)
                self.save_config()
                self.snapshot['records']['tblWork/recItem']['fields']['fldTitle'] = 'before'
                self.outcome = 'continue' if reason == 'continue' else 'waiting'
                self.tick(executor=self.own_write)
                self.timestamp += 300 if reason == 'continue' else 86400
                self.assertEqual(self.tick()['status'], 'ran')

    def test_bad_optional_receipt_does_not_pause_or_skip_business(self):
        self.tick(executor=self.own_write)
        (self.calls[0]['dir'] / 'effects.json').write_text('{broken')
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertFalse((Path(self.config['state_dir']) / 'PAUSED').exists())

    def test_failed_run_never_consumes_its_effects(self):
        def failed(*args):
            self.own_write(*args)
            raise m.RunnerError('uncertain final result')
        self.assertEqual(self.tick(executor=failed)['status'], 'failed')
        self.assertIsNone(self.state()['checkpoint'])
        self.assertEqual(self.tick()['status'], 'paused')

    def test_rotation_requires_successful_handoff_and_keeps_prior_session(self):
        self.config['session_max_runs'] = 1
        self.save_config()
        self.outcome = 'continue'
        def finish(*args):
            result, session = self.execute(*args)
            self.sidecar(args[2], result)
            return result, session
        self.tick(executor=finish)
        self.assertEqual(self.state()['session_runs'], 1)
        checkpoint = self.state()['checkpoint']
        self.timestamp += 300
        fresh_id = '33333333-3333-4333-8333-333333333333'
        def new_session(*args):
            self.assertIsNone(args[1]['session_id'])
            self.assertEqual(args[1]['checkpoint'], checkpoint)
            self.assertEqual(args[1]['session_handoff']['content']['open_items'][0]['source'], 'tblWork/recItem')
            result, _ = self.execute(*args)
            return result, fresh_id
        self.assertEqual(self.tick(executor=new_session)['status'], 'ran')
        self.assertEqual(self.state()['session_id'], fresh_id)
        self.assertEqual(self.state()['session_runs'], 1)
        self.assertEqual(self.state()['session_history'][0]['session_id'], self.session)

    def test_failed_fresh_session_recovers_it_not_old_writer(self):
        self.config['session_max_runs'] = 1
        self.save_config()
        self.outcome = 'continue'
        def first(*args):
            result, session = self.execute(*args)
            self.sidecar(args[2], result)
            return result, session
        self.tick(executor=first)
        self.timestamp += 300
        fresh = '44444444-4444-4444-8444-444444444444'
        def interrupted(config, state, run_dir, prompt, lock_fd, save):
            self.assertIsNone(state['session_id'])
            (run_dir / 'events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': fresh})+'\n')
            raise m.RunnerError('new worker timed out after starting')
        self.assertEqual(self.tick(executor=interrupted)['status'], 'failed')
        before = self.state()['checkpoint']
        m.control_state(self.config_path, 'recover')
        self.assertEqual(self.state()['session_id'], fresh)
        self.assertEqual(self.state()['checkpoint'], before)
        self.assertIn('recovery_note', self.state())
        self.session = fresh
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(self.calls[-1]['state']['session_id'], fresh)
        self.assertEqual(self.state()['session_runs'], 1)

    def test_no_handoff_keeps_old_session_even_past_limit(self):
        self.config['session_max_runs'] = 1
        self.save_config()
        self.outcome = 'continue'
        self.tick()
        self.timestamp += 300
        self.tick()
        self.assertEqual(self.calls[-1]['state']['session_id'], self.session)
        self.assertNotIn('session_history', self.state())

    def test_legacy_config_default_and_invalid_limits(self):
        loaded = m.control.load_config(self.config_path)
        self.assertEqual(loaded['session_max_runs'], 4)
        for value in (True, 0, -1, '4'):
            self.config['session_max_runs'] = value
            self.save_config()
            with self.assertRaises(m.control.ControlError):
                m.control.load_config(self.config_path)

    def test_optional_stats_or_checkpoint_cache_failure_does_not_pause_completed_work(self):
        real_write = m.atomic_json
        for name in ('input-stats.json', 'checkpoint-snapshot.json', 'work-packet.json'):
            with self.subTest(name=name):
                self.config['state_dir'] = str(self.root / ('without-' + name))
                self.save_config()
                def fail_optional(path, value):
                    if Path(path).name == name:
                        raise OSError('optional artifact cannot be saved')
                    return real_write(path, value)
                with mock.patch.object(m, 'atomic_json', side_effect=fail_optional):
                    self.assertEqual(self.tick()['status'], 'ran')
                state = self.state()
                self.assertNotIn('inflight', state)
                self.assertEqual(state['checkpoint'], m.digest(m.stable_snapshot(self.snapshot)))
                self.assertFalse((Path(self.config['state_dir']) / 'PAUSED').exists())
                self.assertEqual(self.tick()['status'], 'unchanged')

    def test_missing_previous_cache_uses_baseline_without_stopping_changed_work(self):
        self.tick()
        self.snapshot['records']['tblWork/recItem']['fields']['fldTitle'] = 'Changed'
        real_write = m.atomic_json
        def fail_cache(path, value):
            if Path(path).name == 'previous-snapshot.json':
                raise OSError('optional old snapshot unavailable')
            return real_write(path, value)
        with mock.patch.object(m, 'atomic_json', side_effect=fail_cache):
            self.assertEqual(self.tick()['status'], 'ran')
        packet = json.loads((self.calls[-1]['dir'] / 'work-packet.json').read_text())
        self.assertEqual(packet['mode'], 'baseline')
        self.assertEqual(packet['records']['current'], self.snapshot['records'])
