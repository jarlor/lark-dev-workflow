import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import plan_action


class PlanActionTests(unittest.TestCase):
    def setUp(self):
        self.config = Path('/private/test-bridge.json')
        self.args = (self.config, 'recRequirement', 4, 'ou_manager')
        self.draft = dict(plan='在详情页显示计划。', scope='这次只改详情。', deliverable='可查看的计划。',
                          verification_plan='打开条目并阅读计划。')
        self.plan = dict(status='prepared', record_id='recNew', plan_revision=1, reason='计划已建。')
        def replace(module, name, value):
            patch = mock.patch.object(module, name, return_value=value)
            self.addCleanup(patch.stop)
            return patch.start()
        self.create = replace(plan_action.prepare_plan, 'prepare', self.plan)
        self.get = replace(plan_action.prepare_plan, 'get_prepared',
                           dict(self.plan, requirement_revision=4, confirmer='ou_manager'))
        self.notify = replace(plan_action.plan_notice, 'run', dict(status='notified', comment_id='c', reply_id='r'))

    def test_uses_real_plan_id_and_revision_and_verified_notification(self):
        result = plan_action.run(*self.args, draft=self.draft)
        self.assertEqual((result['status'], result['notice'], result['comment_id']), ('prepared','notified','c'))
        self.create.assert_called_once_with(*self.args, self.draft, confirm=None)
        self.get.assert_not_called()
        self.notify.assert_called_once_with(self.config, 'recNew', 'ou_manager', 1)

    def test_missing_confirmation_or_conflict_never_notifies(self):
        for before, after in [('pending', 'waiting'), ('conflict', 'conflict')]:
            self.create.return_value = dict(status=before, record_id='recExisting', plan_revision=None, reason='需核对')
            out = plan_action.run(*self.args, draft=self.draft)
            self.assertEqual((out['status'], out['record_id'], out['reason']), (after, 'recExisting', '需核对'))
        self.notify.assert_not_called()

    def test_reconcile_missing_mismatched_and_success_never_create(self):
        original = self.get.return_value
        for changes, status in [({'status':'pending'}, 'waiting'), ({'requirement_revision':2}, 'conflict'),
                                ({'confirmer':'ou_other'}, 'conflict')]:
            self.get.return_value = dict(original, **changes)
            self.assertEqual(plan_action.run(*self.args, reconcile=True)['status'], status)
        self.notify.assert_not_called()
        self.get.return_value = original
        self.notify.return_value = dict(status='pending', reason='上一次提醒的发送结果仍待核实。')
        result = plan_action.run(*self.args, reconcile=True)
        self.assertEqual((result['status'], result['notice'], result['record_id']), ('prepared', 'pending', 'recNew'))
        self.assertEqual(result['reason'], self.notify.return_value['reason'])
        self.create.assert_not_called()

    def test_cli_reconciliation_does_not_read_stdin_and_rejects_invalid_normal_input(self):
        argv = ['plan_action', '--config', str(self.config), '--requirement', self.args[1],
                '--revision', '4', '--confirmer', self.args[3]]
        with mock.patch.object(sys, 'argv', argv+['--reconcile']), mock.patch.object(sys, 'stdin') as stdin, \
                mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as out:
            self.assertEqual(plan_action.main(), 0)
            stdin.read.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())['notice'], 'notified')
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(sys, 'stdin', io.StringIO('not-json')), \
                mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as out:
            self.assertEqual(plan_action.main(), 1)
            self.assertEqual(json.loads(out.getvalue())['status'], 'conflict')
        self.create.assert_not_called()
