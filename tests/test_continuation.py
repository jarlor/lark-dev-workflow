"""Real tick scheduling with synthetic receipts; no project state, messaging or remote work."""
import json
from pathlib import Path

from test_unattended import RuntimeFixture, m, write_json


def item(key, kind, check_at=None, evidence='fixture:checked-source'):
    return dict(key=key, state=kind, check_at=check_at, evidence=evidence, next_step=key)


class ItemScheduling(RuntimeFixture):
    def report(self, items, outcome='waiting', delay=0):
        def executor(*args):
            result, session = self.execute(*args)
            result.update(outcome=outcome, retry_after_seconds=delay, continuation_items=items)
            return result, session
        return self.tick(executor=executor)

    def test_late_queue_review_does_not_delay_authorized_test_or_wake_after_closure(self):
        start = self.timestamp
        review = item('queue/review', 'timer', int(start) + 29350)
        test = item('overview/real-clock-test', 'ready')
        decision = self.report([review, test, item('scope/approval', 'human')], delay=29350)
        self.assertEqual((decision['outcome'], decision['next_due_at']), ('continue', start + 300))
        self.assertEqual(self.state()['last_result']['outcome'], 'waiting')
        self.assertEqual(self.state()['scheduled_outcome'], 'continue')
        self.timestamp += 299
        self.assertEqual(self.tick()['status'], 'unchanged')
        self.timestamp += 1
        decision = self.report([dict(test, state='closed'), dict(review, state='closed', check_at=None)])
        self.assertEqual((decision['outcome'], decision['next_due_at']), ('waiting', None))
        self.timestamp = start + 29350
        self.assertEqual(self.tick()['status'], 'unchanged')

    def test_unrelated_event_does_not_drop_ready_work_or_slide_absolute_timer(self):
        now = int(self.timestamp)
        review = item('external-job/poll', 'timer', now + 500)
        ready = item('test/readback', 'ready')
        self.report([ready, review])
        self.timestamp += 120
        self.snapshot['comments']['unrelated'] = {'revision': 'new'}
        decision = self.report([dict(review, check_at=now + 900, next_step='同一动作的新措辞')], outcome='idle')
        self.assertEqual(decision['next_due_at'], now + 300)
        self.timestamp += 180
        decision = self.report([dict(ready, state='closed')])
        self.assertEqual((decision['outcome'], decision['next_due_at']), ('waiting', now + 500))
        self.timestamp = now + 500
        self.assertEqual(self.report([dict(review, state='closed', check_at=None)], outcome='idle')['next_due_at'], None)

    def test_stalled_work_backs_off_per_action_without_hiding_new_work(self):
        stale = item('stale/poll', 'timer', int(self.timestamp) - 1)
        for delay in (300, 300, 600, 1200, 2400, 3600, 3600):
            decision = self.report([stale])
            self.assertEqual(decision['next_due_at'], self.timestamp + delay)
            self.assertEqual(self.tick()['status'], 'unchanged')
            self.timestamp += delay
        decision = self.report([stale, item('independent/test', 'ready')])
        self.assertEqual(decision['next_due_at'], self.timestamp + 300)

    def test_dependency_human_waits_and_global_pause_never_auto_execute(self):
        self.report([item('deps/build', 'dependency'), item('scope/click', 'human')])
        self.timestamp += 300
        self.assertEqual(self.tick()['status'], 'unchanged')
        self.snapshot['comments']['stop'] = {'revision': 'pause'}
        decision = self.report([item('verify/test', 'ready')], 'paused')
        self.assertEqual((decision['outcome'], decision['next_due_at']), ('paused', None))
        self.timestamp += 86400
        self.assertEqual(self.tick()['status'], 'paused')
        m.control_state(self.config_path, 'resume')
        self.assertEqual(self.report([], outcome='idle')['outcome'], 'continue')

    def test_far_future_appointment_preserves_absolute_date_and_maximum_sleep(self):
        deadline = int(self.timestamp) + 10 * 86400
        decision = self.report([item('appointment/recheck', 'timer', deadline)])
        self.assertEqual(decision['next_due_at'], self.timestamp + 86400)
        self.assertEqual(self.state()['continuation_queue']['appointment/recheck']['item']['check_at'], deadline)

    def test_missing_or_invalid_new_item_receipt_preserves_checkpoint_and_queue(self):
        for invalid in (None, [], [item('a', 'timer')], [item('a', 'ready', 1)], [item('a', 'timer', True)],
                        [item('a', 'unknown')], [item('a', 'human', evidence=' ')], [item('a', 'ready')] * 2):
            with self.subTest(invalid=invalid):
                self.config['state_dir'] = str(self.root / ('case-' + str(len(self.calls))))
                self.save_config()
                self.assertEqual(self.report(invalid)['status'], 'failed')
                self.assertIsNone(self.state()['checkpoint'])
                self.assertNotIn('continuation_queue', self.state())

    def test_legacy_saved_receipt_rechecked_once_without_new_input_or_duplicate_wakes(self):
        self.report([item('scope/approval', 'human')])
        state = self.state()
        state.pop('continuation_queue')
        state.pop('scheduled_outcome')
        state['last_result'].pop('continuation_items')
        state['next_due_at'] = self.timestamp + 29350
        write_json(Path(self.config['state_dir']) / 'state.json', state)
        result = m.tick(self.config_path, collector=self.collect, executor=self.execute, clock=lambda: self.timestamp)
        self.assertEqual(result['outcome'], 'waiting')
        context = json.loads(self.calls[-1]['prompt'].split('本轮本地配置与输入位置：\n')[1])
        self.assertEqual(context['reason'], 'continuation_review')
        self.assertEqual(self.state()['session_id'], state['session_id'])
        self.assertEqual(self.tick()['status'], 'unchanged')

    def test_new_worker_cannot_omit_item_protocol_and_archive_remains_readable(self):
        def legacy(*args):
            result, session = self.execute(*args)
            del result['continuation_items']
            self.assertEqual(m.validate_result(result, result['run_id'], result['snapshot_id']), result)
            return result, session
        self.assertEqual(self.tick(executor=legacy)['status'], 'failed')
        self.assertIsNone(self.state()['checkpoint'])
