import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import stage_action as action
import stage_business


def entry(rev, who, field=None, kind=None, before=None, after=None):
    changes = [] if not field else [dict(field_id=field, field_type=kind, before=before, after=after)]
    return dict(rev=rev, create_time=rev+1000, operator=who, activity_type='update', field_changes=changes)


class Project:
    def __init__(self, directory, key='work-start'):
        self.directory = directory
        self.path = '/project/service.json'
        self.table, self.project, self.bot = 'tblWork', 'recProject', 'ou_assistant'
        self.token, self.identity = 'base', 'bound-project'
        self.config = dict(app_id='cli_app', tenant_key='tenant')
        self.bot_name = '研发助手'
        self.set_rule(key)
        self.marker, self.state = '', self.rule['source']['name']
        self.rows = [entry(31, '项目负责人')]
        self.sends = []
        self.enabled = True
        self.mode = 'normal'

    def set_rule(self, key):
        self.key = key
        _, text, source, target = action.rules.STAGE_REQUESTS[key]
        self.rule = dict(source={'name': source}, target={'name': target}, marker_field_id='fldMarker',
                         stage_field_id='fldStage', workflow_id='wkf' + key.replace('-', ''),
                         marker_text=text, definition_sha256='a' * 64)

    def check_rule(self, *, sending=False):
        action.require(not sending or self.enabled, '规则已停用。')

    def row(self, record):
        return self.state, self.marker

    def history(self, record, anchor=None):
        if anchor is None:
            return copy.deepcopy(self.rows[:1])
        ix = next((i for i, h in enumerate(self.rows) if h['rev'] == anchor), None)
        action.require(ix is not None, '看不到原版本。')
        return copy.deepcopy(self.rows[:ix + 1])

    def send_marker(self, record):
        self.sends.append((record, self.rule['marker_text']))
        if self.mode == 'timeout_before_write':
            raise subprocess.TimeoutExpired('lark-cli', 45)
        rev = self.rows[0]['rev'] + 3  # Feishu revisions belong to the table, not this row alone.
        new_marker = self.rule['marker_text']
        self.rows.insert(0, entry(rev, self.bot_name, 'fldMarker', 'Text', self.marker, new_marker))
        self.marker = new_marker
        if self.mode != 'delayed':
            self.finish()
        if self.mode == 'timeout_after_write':
            raise subprocess.TimeoutExpired('lark-cli', 45)

    def finish(self):
        target = self.rule['target']['name']
        self.rows.insert(0, entry(self.rows[0]['rev'] + 2, '多维表格助手', 'fldStage', 'Stage', self.state, target))
        self.state = target


class Business:
    def __init__(self, project):
        self.project = project
        self.revision = 4
        self.sha = '1' * 64
        self.receipt = None

    def verify(self, config_path, key, rid):
        assert config_path == self.project.path
        return action.BusinessEvidence(self.project.project, self.project.table, rid, key, self.revision, self.sha)

    def marker_receipt(self, *args):
        return self.receipt


class StageActionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Project(Path(tmp.name))
        self.business = Business(self.project)
        patch = mock.patch.object(action, 'Project', return_value=self.project)
        self.addCleanup(patch.stop)
        # Keep the real adapter type available to test the exact CLI boundary.
        self.project_type = action.Project
        self.factory = patch.start()
        def original_approval(config, role, record):
            return dict(status='verified', business_revision=self.business.revision, record_id=record,
                        table_id=self.project.table, project_record_id=self.project.project,
                        confirmation_sha256='3' * 64)
        approvals = mock.patch.object(stage_business, 'confirmation_for', side_effect=original_approval)
        self.addCleanup(approvals.stop)
        self.approval = approvals.start()

    def run_action(self, key=None, record='recWork'):
        return action._run(self.project.path, key or self.project.key, record, business=self.business)

    def test_six_actions_reuse_the_same_transaction(self):
        for i, key in enumerate(action.rules.STAGE_REQUESTS):
            with self.subTest(key=key):
                self.project.set_rule(key)
                self.project.state, self.project.marker = self.project.rule['source']['name'], ''
                rid = 'recWork' + str(i)
                result = self.run_action(record=rid)
                self.assertEqual(result['status'], 'completed')
                self.assertLess(result['marker_revision'], result['transition_revision'])
                self.assertEqual(self.run_action(record=rid)['status'], 'already_completed')
        self.assertEqual(len(self.project.sends), 6)

    def test_next_job_may_replace_only_a_checked_previous_marker(self):
        self.assertEqual(self.run_action()['status'], 'completed')
        self.project.set_rule('work-deliver')
        self.assertEqual(self.run_action()['status'], 'completed')
        self.assertEqual(len(self.project.sends), 2)
        self.project.state = self.project.rule['source']['name']
        self.assertEqual(self.run_action()['status'], 'pending')
        self.assertEqual(len(self.project.sends), 2)

    def test_unknown_initial_marker_and_wrong_stage_never_dispatch(self):
        self.project.marker = '另一事项'
        self.assertEqual(self.run_action()['status'], 'pending')
        self.project.marker = ''
        self.project.state = self.project.rule['target']['name']
        self.assertEqual(self.run_action()['status'], 'pending')
        self.assertFalse(self.project.sends)

    def test_schema_change_or_modified_evidence_never_dispatches(self):
        original = self.business.verify
        different = replace(original(self.project.path, self.project.key, 'recWork'), evidence_sha256='2' * 64)
        for change in ('business', 'version', 'rule', 'table'):
            with self.subTest(change=change):
                self.project.enabled = change != 'rule'
                results = [original(self.project.path, self.project.key, 'recWork'), different]
                if change == 'version':
                    results = [replace(different, revision=True)] * 2
                if change == 'table':
                    results = [replace(different, table_id='tblOther')] * 2
                if change == 'rule':
                    results = [different] * 2
                with mock.patch.object(self.business, 'verify', side_effect=results):
                    self.assertEqual(self.run_action()['status'], 'pending')
        self.assertFalse(self.project.sends)

    def test_delayed_history_and_rule_disabled_after_send_still_reconcile(self):
        self.project.mode = 'delayed'
        self.assertEqual(self.run_action()['status'], 'pending')
        self.project.enabled = False
        self.assertEqual(self.run_action()['status'], 'pending')
        self.project.finish()
        self.assertEqual(self.run_action()['status'], 'completed')
        self.assertEqual(len(self.project.sends), 1)

    def test_timeout_without_a_matching_event_never_retries_the_write(self):
        self.project.mode = 'timeout_after_write'
        self.assertEqual(self.run_action()['status'], 'pending')
        self.assertEqual(self.run_action()['status'], 'pending')
        self.business.receipt = dict(app_id='cli_app', tenant_key='tenant', base_token='base', table_id='tblWork',
                                     record_id='recWork', field_id='fldMarker', operator_open_id='ou_other',
                                     revision=34, update_time=1034, before='', after='研发项开始执行', event_id='evt_1')
        self.assertEqual(self.run_action()['status'], 'pending')
        self.business.receipt.update(operator_open_id='ou_assistant', revision=35)
        self.assertEqual(self.run_action()['status'], 'pending')
        self.business.receipt['revision'] = 34
        self.assertEqual(self.run_action()['status'], 'completed')
        self.assertEqual(len(self.project.sends), 1)

    def test_interrupted_intent_blocks_even_a_different_action_on_this_row(self):
        self.project.mode = 'timeout_before_write'
        self.assertEqual(self.run_action()['status'], 'pending')
        self.project.set_rule('work-deliver')
        self.assertEqual(self.run_action()['status'], 'pending')
        self.assertEqual(len(self.project.sends), 1)

    def test_wrong_author_extra_write_unrelated_edit_or_changed_evidence_blocks_outcome(self):
        for i, change in enumerate(('marker_author', 'stage_author', 'extra', 'other', 'head', 'evidence')):
            with self.subTest(change=change):
                self.project.mode = 'delayed'
                self.project.rows = [entry(31, '项目负责人')]
                self.project.state, self.project.marker = '可开始', ''
                self.business.sha = '1' * 64
                rid = 'recWork' + str(i)
                self.assertEqual(self.run_action(record=rid)['status'], 'pending')
                self.project.finish()
                if change == 'marker_author':
                    self.project.rows[1]['operator'] = '另一人'
                if change == 'stage_author':
                    self.project.rows[0]['operator'] = '反馈人'
                if change == 'extra':
                    self.project.rows.insert(0, entry(40, '人', 'fldOther', 'Text', '', '改过'))
                if change == 'other':
                    self.project.rows[1]['field_changes'].append({'field_id': 'fldOther'})
                if change == 'head':
                    self.project.rows[-1]['operator'] = '另一人'
                if change == 'evidence':
                    self.business.sha = '2' * 64
                self.assertEqual(self.run_action(record=rid)['status'], 'pending')
        self.assertEqual(len(self.project.sends), 6)

    def test_intent_survives_two_concurrent_callers(self):
        barrier = threading.Barrier(2)
        results = []
        def run():
            barrier.wait()
            results.append(self.run_action()['status'])
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertCountEqual(results, ['completed', 'already_completed'])
        self.assertEqual(len(self.project.sends), 1)

    def test_writer_uses_only_pinned_row_and_single_marker_and_actual_success_shape(self):
        adapter = self.project_type.__new__(self.project_type)
        adapter.token, adapter.table, adapter.rule = 'base', 'tblWork', self.project.rule
        adapter.actor = {'profile': 'bound'}
        data = {'updated': True, 'record': {'update': {'助手请求': '研发项开始执行'}}}
        reader = adapter.reader = mock.Mock()
        reader.base._envelope.return_value = data
        reader.runner.return_value.stdout = json.dumps({'identity': 'bot', 'data': data})
        adapter.send_marker('recWork')
        args = reader.runner.call_args.args[0]
        self.assertEqual(args[:-1], ['lark-cli', 'base', '+record-upsert', '--base-token', 'base', '--table-id',
                                    'tblWork', '--record-id', 'recWork', '--as', 'bot', '--profile', 'bound', '--json'])
        self.assertEqual(json.loads(args[-1]), {'fldMarker': '研发项开始执行'})
        data['record']['update']['状态'] = '进行中'
        with self.assertRaises(action.Pending):
            adapter.send_marker('recWork')

    def test_invalid_input_or_state_symlink_does_not_write(self):
        self.assertEqual(self.run_action(record='../../unsafe')['status'], 'pending')
        self.factory.assert_not_called()
        path = self.project.directory / 'stage-actions'
        path.symlink_to(self.project.directory)
        self.assertEqual(self.run_action()['status'], 'pending')
        self.assertFalse(self.project.sends)


if __name__ == '__main__':
    unittest.main()
