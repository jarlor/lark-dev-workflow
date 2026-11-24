import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import acceptor_enrollment as enroll
import stage_action
import stage_business as business
from test_stage_business import Project
from test_stage_graph import Graph


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.p = Project(Path(folder.name), 'requirements')
        self.p.key = 'req-start'
        self.p.runner = {'lark_bin': '/private/lark-cli'}
        self.person = self.p.row['confirmer'][0]
        self.p.row['acceptor'] = []
        self.p.row['feedback'] = [{'id': 'recFeedback'}]
        self.adapter = mock.Mock(p=self.p)
        self.adapter.tables = {'feedback': {'id': 'tblFeedback', 'fields': {'author': {'id': 'fldOriginal', 'type': 'text'}}}}
        self.feedback = {'author': self.person['name']}
        self.adapter.row.side_effect = lambda role, rid: self.feedback if role == 'feedback' else self.p.row
        self.adapter.feedback.return_value = ['recFeedback']
        self.proof = self.p.proof()
        self.confirmation_for = business.confirmation_for
        self.original = business.protected_fields('requirements', self.p.fields, self.p.row)
        self.seal = {'proof': self.proof, 'anchor': copy.deepcopy(self.p.rows[0]), 'fields': copy.deepcopy(self.original)}
        self.origins = [{'activity_type': 'create', 'operator': self.person['name'], 'create_time': 1000, 'rev': 12}]
        self.native = {'record_id': 'recFeedback', 'created_time': 1000000, 'created_by': copy.deepcopy(self.person)}
        self.reply = {'ok': True, 'identity': 'bot', 'data': {'record': self.native}}
        self.p.reader.profile = 'dedicated'
        self.p.reader.runner.side_effect = lambda *args, **kwargs: mock.Mock(returncode=0, stdout=json.dumps(self.reply))
        for owner, key, val in [(business, 'Business', self.adapter),
                                (business, 'confirmation_for', self.proof),
                                (business.confirmation, 'history', self.origins)]:
            patched = mock.patch.object(owner, key, return_value=val)
            self.addCleanup(patched.stop)
            patched.start()

    def preview(self):
        return enroll.check('service.json', 'requirements', 'recRow')

    def history(self):
        current = business.protected_fields('requirements', self.p.fields, self.p.row)
        return business.record_history(self.p, 'requirements', 'recRow', self.seal, self.p.row, current)[1]

    def fill(self):
        self.p.row['acceptor'] = [copy.deepcopy(self.person)]
        self.p.add_change('acceptor', '', self.person['name'], '研发助手')
        self.p.rows[0]['field_changes'][0]['field_type'] = 'User'

    def assignment(self):
        return dict(method='project_test_acceptor', project_record_id=self.p.project, table_id=self.p.table,
                    record_id='recRow', field_id=self.p.fields['acceptor']['id'], role='requirements', revision=self.proof['business_revision'],
                    confirmation_revision=self.proof['table_revision'], feedback_ids=['recFeedback'],
                    authorizer_open_id=self.person['id'], confirmer_open_id=self.person['id'], acceptor_open_id='ou_qa')

    def test_enrolled_human_can_test_without_changing_the_original_approval(self):
        with mock.patch.object(enroll.stage_source, 'query', return_value=self.assignment()) as grant, \
             mock.patch.object(enroll.confirmation, 'confirm_directory_user') as directory:
            info = enroll.check('service.json', 'requirements', 'recRow', assigned=True)
            self.assertEqual((info['status'], info['person_open_id']), ('ready_to_fill', 'ou_qa'))
            self.p.row['acceptor'] = [dict(id='ou_qa', name='测试同事')]
            self.p.add_change('acceptor', None, '测试同事', '研发助手')
            self.p.rows[0]['field_changes'][0]['field_type'] = 'User'
            self.assertEqual(self.original, self.history())
            self.assertEqual(enroll.check('service.json', 'requirements', 'recRow', assigned=True)['status'], 'already_assigned')
            self.assertTrue(all(call.args[-1]=='ou_qa' for call in directory.call_args_list))
            grant.assert_called_with(self.p, 'acceptor_assignment', 'requirements', 'recRow')
            self.p.rows[0]['field_changes'][0]['before'] = '之前的验收人'
            with self.assertRaises(stage_action.Pending): self.history()

    def test_delegation_never_retargets_other_rows_revisions_people_or_feedback(self):
        for key, value in [('project_record_id','elsewhere'),('table_id','other'),('record_id','recElse'),
                           ('field_id','fldOther'),('role','bugs'),('revision',True),('revision',self.proof['business_revision']+1),
                           ('confirmation_revision',0),('authorizer_open_id','ou_unrelated'),
                           ('confirmer_open_id','ou_unrelated'),('feedback_ids',[]),('method','from_comment'),
                           ('acceptor_open_id',self.p.bot),('acceptor_open_id',self.person['id'])]:
            grant = dict(self.assignment(), **{key:value})
            with self.subTest(key=key,value=value), mock.patch.object(enroll.stage_source,'query',return_value=grant), self.assertRaises(stage_action.Pending):
                enroll.check('service.json','requirements','recRow',assigned=True)
        with mock.patch.object(enroll.stage_source,'query',return_value=self.assignment()), \
             mock.patch.object(enroll.confirmation,'confirm_directory_user',side_effect=enroll.confirmation.Pending('not a person')), \
             self.assertRaises(enroll.confirmation.Pending):
            enroll.check('service.json','requirements','recRow',assigned=True)

    def test_preview_and_retry_then_exact_fill_preserves_original(self):
        first = self.preview()
        self.assertEqual((first['status'], first['person_open_id'], first['feedback_ids']),
                         ('ready_to_fill', self.person['id'], ['recFeedback']))
        self.assertEqual(first, self.preview())
        self.fill()
        self.assertEqual(self.original, self.history())
        self.assertEqual(self.preview()['status'], 'already_assigned')
        calls = self.p.reader.runner.call_args_list
        self.assertTrue(all(c.args[0][1:3] == ['api', 'GET'] and c.args[0][-4:] == ['--as', 'bot', '--profile', 'dedicated']
                            for c in calls))

    def test_actual_seal_and_digest_remain_stable_after_fill_and_retry(self):
        def verify(*args, receipt_reader):
            receipt_reader()
            return self.proof

        with mock.patch.object(stage_action, 'Project', return_value=self.p), \
             mock.patch.object(business.watch, '_fields', return_value={}), \
             mock.patch.object(business.confirmation, 'verify', side_effect=verify) as native, \
             mock.patch.object(business.confirmation, 'receipt', return_value=self.p.event()):
            first = self.confirmation_for(self.p.path, 'requirements', 'recRow')
            self.assertEqual(first['status'], 'verified')
            self.fill()
            for _ in range(2):
                retry = self.confirmation_for(self.p.path, 'requirements', 'recRow')
                self.assertEqual(retry, first)
            native.assert_called_once()
            ledger = next((self.p.directory / 'stage-confirmations').glob('*.json'))
            self.assertEqual(len(json.loads(ledger.read_text())['confirmations']), 1)

    def test_replacement_repeat_rollback_and_after_acceptance_are_rejected(self):
        self.fill()
        self.p.rows[0]['field_changes'][0]['before'] = '之前的验收人'
        with self.assertRaises(stage_action.Pending):
            self.history()
        self.p.rows[0]['field_changes'][0]['before'] = ''
        self.p.rows.insert(0, dict(self.p.rows[0], rev=self.p.rows[0]['rev'] + 1))
        with self.assertRaises(stage_action.Pending):
            self.history()
        self.p.rows.pop(0)
        self.p.rows[0]['field_changes'][0].update(before=self.person['name'], after='')
        self.p.row['acceptor'] = []
        with self.assertRaises(stage_action.Pending):
            self.history()
        self.p.rows.pop(0)
        self.p.add_change('status', '可研发', '研发中', '多维表格助手')
        self.p.add_change('status', '研发中', '待验收', '多维表格助手')
        self.p.row['status'] = ['待验收']
        self.fill()
        with self.assertRaises(stage_action.Pending):
            self.history()

    def test_changed_scope_confirmer_or_two_different_reporters_remain_pending(self):
        self.fill()
        self.p.add_change('scope', '原范围', '现范围', '同事')
        with self.assertRaises(stage_action.Pending):
            self.history()
        self.p.rows.pop(0)
        with mock.patch.dict(self.p.row, {'confirmer': [{'id': 'ou_other', 'name': self.person['name']}]}), self.assertRaises(stage_action.Pending):
            self.history()
        self.adapter.feedback.return_value = ['recFeedback', 'recOther']
        other = dict(self.native, record_id='recOther', created_by={'id': 'ou_other', 'name': self.person['name']})
        self.p.reader.runner.side_effect = lambda args, **kwargs: mock.Mock(returncode=0, stdout=json.dumps({
            **self.reply, 'data': {'record': other if args[3].endswith('/recOther') else self.native}}))
        with self.assertRaises(stage_action.Pending):
            self.history()

    def test_reject_delegated_or_reassigned_source_wrong_record_and_wrong_scope(self):
        for field, value in [('created_by', {'id': 'ou_other', 'name': self.person['name']}),
                             ('created_time', 0), ('record_id', 'recOther')]:
            with self.subTest(field=field), mock.patch.dict(self.native, {field: value}), self.assertRaises(stage_action.Pending):
                self.preview()
        with mock.patch.dict(self.feedback, {'author': '另一位同事'}), self.assertRaises(stage_action.Pending):
            self.preview()
        with mock.patch.dict(self.proof, {'project_record_id': 'recElsewhere'}), self.assertRaises(stage_action.Pending):
            self.preview()
        self.origins.insert(0, {'activity_type': 'update', 'create_time': 1001, 'rev': 13, 'field_changes': [
            {'field_id': 'fldOriginal', 'field_type': 'Text', 'before': '原作者', 'after': self.person['name']}]})
        with self.assertRaises(stage_action.Pending):
            self.preview()
        self.origins[0]['field_changes'][0]['before'] = ''
        self.assertEqual(self.preview()['status'], 'ready_to_fill')

    def test_incomplete_feedback_history_unknown_proof_or_api_denial_stays_pending(self):
        for origin_patch in [{'rev': None}, {'create_time': 999}]:
            with self.subTest(origin_patch=origin_patch), mock.patch.dict(self.origins[0], origin_patch), self.assertRaises(stage_action.Pending):
                self.preview()
        with mock.patch.dict(self.origins[0], {'on_behalf_of': 'other'}), self.assertRaises(business.confirmation.Pending):
            self.preview()
        with mock.patch.dict(self.proof, {'check': 'same_name'}), self.assertRaises(stage_action.Pending):
            self.preview()
        self.adapter.feedback.return_value = []
        with self.assertRaises(stage_action.Pending):
            self.preview()
        self.adapter.feedback.return_value = ['recFeedback']
        self.p.reader.runner.side_effect = lambda *args, **kwargs: mock.Mock(returncode=1, stdout='')
        with self.assertRaisesRegex(stage_action.Pending, '原始创建人'):
            self.preview()

    def test_do_not_fill_when_row_changed_during_check_or_has_multiple_people(self):
        head = self.p.history('recRow')[0]
        with mock.patch.object(self.p, 'history', side_effect=[[head], [{**head, 'rev': head['rev'] + 1}]]):
            with self.assertRaisesRegex(stage_action.Pending, '发生了变化'):
                self.preview()
        self.p.row['acceptor'] = [self.person, {'id': 'ou_other', 'name': '另一位同事'}]
        with self.assertRaisesRegex(stage_action.Pending, '其他验收人'):
            self.preview()

    def test_bug_uses_its_own_confirmation_field_and_revision(self):
        requirement = self.p
        self.p = Project(requirement.directory, 'bugs')
        self.p.key, self.p.runner = 'bug-start', requirement.runner
        self.p.reader.profile = requirement.reader.profile
        self.p.reader.runner.side_effect = requirement.reader.runner.side_effect
        self.adapter.p = self.p
        self.proof.update(self.p.proof())
        proof = enroll.check('service.json', 'bugs', 'recRow')
        self.assertEqual((proof['status'], proof['field_id']), ('ready_to_fill', self.p.fields['acceptor']['id']))
        self.fill()
        self.assertEqual(enroll.check('service.json', 'bugs', 'recRow')['status'], 'already_assigned')


class ManualIdentityTests(unittest.TestCase):
    def test_same_bound_original_human_and_directory_fallback(self):
        graph = Graph('req-accept')
        graph.data['requirements', 'recReq']['confirmer'] = [{'id': 'ou_human'}]
        graph.confirmed_people = {'recReq': 'ou_human'}
        with mock.patch.object(business.confirmation, 'confirm_directory_user',
                               side_effect=business.confirmation.Pending('目录未核实')) as directory:
            graph.apply()
            directory.assert_not_called()
            graph.confirmed_people['recReq'] = 'ou_other'
            with self.assertRaises(business.confirmation.Pending):
                graph.apply()
            directory.assert_called_once()

    def test_never_reuse_some_other_rows_or_roles_approval(self):
        adapter = business.Business.__new__(business.Business)
        adapter.p = mock.Mock(project='recProject', path='service')
        adapter.tables = {'requirements': {'id': 'tblReq', 'fields': {'status': {'id': 'fldStage'}}}}
        proof = dict(status='verified', check='archived_runner_history', role='requirements', record_id='recReq',
                     project_record_id='recProject', table_id='tblReq', field_id='fldStage', operator_open_id='ou_person',
                     business_revision=1, confirmation_sha256='a' * 64)
        for key, value in [('role', 'bugs'), ('project_record_id', 'recOther'), ('table_id', 'tblOther'),
                           ('record_id', 'recOther'), ('field_id', 'fldOther'), ('check', 'status_only')]:
            adapter.approvals, adapter.confirmed_people = {}, {}
            with self.subTest(key=key), mock.patch.object(business, 'confirmation_for', return_value={**proof, key: value}):
                adapter.approved('requirements', 'recReq')
                self.assertFalse(adapter.confirmed_people)
        with mock.patch.object(business, 'confirmation_for', return_value=proof):
            adapter.approvals = {}
            adapter.approved('requirements', 'recReq')
        self.assertEqual(adapter.confirmed_people, {'recReq': 'ou_person'})
