"""Recovery boundaries for optional fresh sessions; no live Codex or Feishu."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'))
import run_context as c


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'runs' / 'run-one'
        self.run.mkdir(parents=True)
        self.snapshot = {'format_version': 1, 'records': {}, 'comments': {}}
        self.result = {'run_id': 'run-one', 'snapshot_id': c.snapshot_digest(self.snapshot),
                       'outcome': 'waiting'}
        self.state = {'session_id': 'old-session', 'session_runs': 4,
                      'checkpoint': self.result['snapshot_id'], 'last_result': self.result,
                      'next_due_at': 123, 'idle_wakes': 1, 'recoveries': [{'old': 'evidence'}]}
        self.handoff = {'format_version': 1, **{k: self.result[k] for k in ('run_id', 'snapshot_id')},
                        'can_start_fresh': True, 'summary': 'Waiting for exact scope confirmation.',
                        'open_items': [{'source': 'tblFeedback/recOne', 'next_step': 'Check human reply.'}],
                        'constraints': [{'source': 'rules.md', 'rule': 'Production deploy is stopped.'}],
                        'active_operations': []}

    def write(self, name, value):
        path = self.run / name
        path.write_text(json.dumps(value))
        return path

    def test_old_install_recovers_only_matching_successful_start_snapshot(self):
        self.write('snapshot.json', self.snapshot)
        self.assertEqual(c.checkpoint_snapshot(self.root, self.state), self.snapshot)
        self.state['checkpoint'] = 'different'
        self.assertIsNone(c.checkpoint_snapshot(self.root, self.state))

    def test_last_scan_cannot_replace_a_processing_checkpoint(self):
        (self.root / 'last-scan.json').write_text(json.dumps(self.snapshot))
        self.assertIsNone(c.checkpoint_snapshot(self.root, self.state))

    def test_changed_observation_time_does_not_invalidate_baseline(self):
        self.write('snapshot.json', dict(self.snapshot, observed_at='later'))
        self.assertIsNotNone(c.checkpoint_snapshot(self.root, self.state))

    def test_legacy_session_requests_handoff_before_resetting(self):
        del self.state['session_runs']
        before = copy.deepcopy(self.state)
        self.assertEqual(c.session_runs(self.state, 4), 4)
        self.assertEqual(c.prepare_session(self.state, self.root, 4)['mode'], 'resume')
        self.assertEqual(self.state, before)

    def test_safe_handoff_preserves_pending_scope_stops_and_scheduler_evidence(self):
        path = self.write('handoff.json', self.handoff)
        prior = copy.deepcopy(self.state)
        plan = c.prepare_session(self.state, self.root, 4)
        self.assertEqual(plan['mode'], 'fresh_from_handoff')
        self.assertIsNone(self.state['session_id'])
        self.assertEqual(self.state['session_runs'], 0)
        self.assertEqual(plan['handoff']['content'], self.handoff)
        self.assertEqual(self.state['session_history'][0]['handoff_file'], str(path))
        for key in ('checkpoint', 'last_result', 'next_due_at', 'idle_wakes', 'recoveries'):
            self.assertEqual(self.state[key], prior[key])

    def test_unknown_execution_and_inflight_never_rotate(self):
        self.write('handoff.json', self.handoff)
        for extra in ({'recovery_note': 'Unknown previous write'}, {'inflight': {'run_id': 'pending'}}):
            state = {**copy.deepcopy(self.state), **extra}
            prior = copy.deepcopy(state)
            self.assertEqual(c.prepare_session(state, self.root, 4)['mode'], 'resume')
            self.assertEqual(state, prior)

    def test_running_external_operation_blocks_rotation(self):
        self.handoff['active_operations'] = ['deployment already running; result unknown']
        self.write('handoff.json', self.handoff)
        self.assertEqual(c.prepare_session(self.state, self.root, 4)['mode'], 'resume')
        self.assertEqual(self.state['session_id'], 'old-session')

    def test_stale_or_malformed_handoff_is_ignored_without_losing_state(self):
        for value in ({}, dict(self.handoff, run_id='other'), dict(self.handoff, snapshot_id='other'),
                      dict(self.handoff, open_items=[{'source': '', 'next_step': 'do work'}])):
            with self.subTest(value=value):
                self.write('handoff.json', value)
                before = copy.deepcopy(self.state)
                self.assertEqual(c.prepare_session(self.state, self.root, 4)['mode'], 'resume')
                self.assertEqual(self.state, before)

    def test_not_due_or_worker_declines_keeps_existing_session(self):
        self.write('handoff.json', self.handoff)
        self.state['session_runs'] = 1
        self.assertEqual(c.prepare_session(self.state, self.root, 4)['mode'], 'resume')
        self.state['session_runs'] = 4
        self.handoff['can_start_fresh'] = False
        self.write('handoff.json', self.handoff)
        self.assertEqual(c.prepare_session(self.state, self.root, 4)['mode'], 'resume')

    def test_no_cross_directory_handoff_or_baseline_lookup(self):
        self.state['last_result'] = dict(self.result, run_id='../outside')
        self.assertEqual(c.prepare_session(self.state, self.root, 4)['mode'], 'resume')
        self.assertIsNone(c.checkpoint_snapshot(self.root, self.state))


if __name__ == '__main__':
    unittest.main()
