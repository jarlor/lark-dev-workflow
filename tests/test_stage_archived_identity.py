import copy
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'service'))
import archived_identity
import stage_action
import stage_business
import stage_source
from prepare_plan import digest
from test_stage_business import Project


class OldWorkerDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Project(pathlib.Path(self.tmp.name), 'requirements')
        p = self.p
        self.anchor = p.rows[0]
        self.row = p.row
        self.sources = {k: digest(k) for k in archived_identity.SOURCE_KEYS}
        self.material = dict(method='archived_runner_history', old_operator_id='12345', old_confirmer_id='12345',
                             old_reply_author_id='12345', original_worker_verified_person=True,
                             old_person_name='负责的同事', original_event_captured=False, operator_open_id='ou_colleague',
                             record_id='recRow', table_id=p.table, project_record_id=p.project,
                             stage_field_id=p.fields['status']['id'], confirmer_field_id=p.fields['confirmer']['id'],
                             history=copy.deepcopy(self.anchor), business_revision=2,
                             report_sha256=digest('report'), source_hashes=self.sources,
                             reply=dict(comment_id='123', reply_id='456', create_time=15, update_time=16,
                                        original_text_sha256=digest('收到'), live_text_sha256=digest('收到')))
        self.reply = dict(reply_id='456', user_id='ou_colleague', create_time=15, update_time=16,
                          content={'elements': [{'type': 'text_run', 'text_run': {'text': '收到'}}]})
        self.page = dict(file_token=p.token, file_type='bitable', comment_id='123', items=[self.reply], has_more=False)
        self.p.reader.read.return_value = self.page

    def check(self):
        with mock.patch.object(stage_source, 'query', return_value=self.material), \
             mock.patch.object(stage_business.confirmation, 'confirm_directory_user', side_effect=AssertionError):
            return stage_business.archived_click(self.p, 'requirements', 'recRow', self.row, {})

    def test_original_native_click_without_directory_or_synthetic_event(self):
        proof = self.check()['proof']
        self.assertEqual(proof['check'], 'archived_runner_history')
        self.assertIsNone(proof['event_id'])
        self.assertFalse(proof['original_event_captured'])
        self.assertEqual(proof['table_revision'], self.anchor['rev'])

    def test_link_across_distinct_archives_and_real_same_reply_is_required(self):
        for key, value in [('old_confirmer_id', '54321'), ('old_reply_author_id', '54321'),
                ('original_worker_verified_person', False), ('confirmer_field_id', 'other'),
                ('stage_field_id', 'other'), ('record_id', 'recOther'), ('table_id', 'tableOther'),
                ('old_person_name', '另一位同事'), ('business_revision', 1), ('operator_open_id', 'ou_other')]:
            with self.subTest(key=key), mock.patch.dict(self.material, {key: value}), self.assertRaises(stage_action.Pending):
                self.check()
        for key, value in [('reply_id', '999'), ('user_id', 'ou_other'), ('update_time', 17),
                           ('content', {'elements': [{'type': 'text_run', 'text_run': {'text': '别的回复'}}]})]:
            with self.subTest(key=key), mock.patch.dict(self.reply, {key: value}), self.assertRaises(stage_action.Pending):
                self.check()

    def test_one_comment_is_never_approval(self):
        self.p.rows[0]['field_changes'][0]['after'] = '已验收'
        with self.assertRaises(stage_action.Pending):
            self.check()

    def test_hashes_must_cover_each_different_original(self):
        hashes = set(self.sources.values()) | {self.material['report_sha256']}
        archived_identity.source_hashes(self.material, hashes)
        with self.assertRaises(stage_action.Pending):
            archived_identity.source_hashes(self.material, hashes - {self.sources['official_reply']})
        with mock.patch.dict(self.sources, {'original_person_field': self.sources['original_history']}), self.assertRaises(stage_action.Pending):
            archived_identity.source_hashes(self.material, hashes)
