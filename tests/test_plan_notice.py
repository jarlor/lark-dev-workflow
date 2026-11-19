import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import plan_notice as notice


class PlanNoticeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = mock.Mock(directory=Path(temp.name), token='BaseFixture', project='recProject',
                                 open_id='ou_assistant', path=Path(temp.name) / 'bridge',
                                 signature='bound', config={'app_id': 'cli_demo'})
        self.view = {'id': 'vewPlan', 'name': '待确认计划', 'type': 'grid'}
        self.project.tables = {'work': {'id': 'tblWork', 'views': [self.view],
                                        'fields': {'plan': {'name': '研发计划'}}}}
        self.project.reader.base.pages.return_value = [self.view.copy()]
        self.project.reader.profile = 'dedicated'
        self.work = dict(requirements=[{'id':'recRequirement'}], project=[{'id':'recProject'}],
                         status=['待确认计划'], nature=['当前工作'], plan_confirmer=[{'id':'ou_person'}],
                         plan_revision=1, plan='显示缺货原因', scope='商品页', deliverable='一条提示', verification_plan='打开缺货商品查看原因')
        self.project.row.side_effect = lambda role, rid: copy.deepcopy(self.work)
        self.prepared = dict(status='prepared', record_id='recWork', plan_revision=1, confirmer='ou_person',
                             values={k: self.work[k] for k in notice.plan.TEXT})
        for method, result in [('Project', self.project), ('get_prepared', self.prepared)]:
            patch = mock.patch.object(notice.plan, method, return_value=result)
            setattr(self, method, patch.start())
            self.addCleanup(patch.stop)
        patch = mock.patch.object(notice.plan.confirmation, 'confirm_directory_user')
        self.directory = patch.start()
        self.addCleanup(patch.stop)
        self.project.reader.runner.side_effect = self.cli
        self.sends = self.previews = self.readbacks = 0
        self.preview_edit = self.result_edit = self.readback_edit = lambda data: None
        self.anchor = dict(block_id='tblWork', base_view_id='vewPlan', base_record_id='recWork')

    def cli(self, cmd, **kwargs):
        self.assertEqual(cmd[0:2], ['lark-cli','drive'])
        self.assertEqual(cmd[cmd.index('--as')+1], 'bot')
        self.assertEqual(cmd[cmd.index('--profile')+1], 'dedicated')
        dry = '--dry-run' in cmd
        if cmd[2] == '+add-comment':
            self.assertEqual(cmd[cmd.index('--block-id')+1], 'tblWork!recWork!vewPlan')
            elements = json.loads(cmd[cmd.index('--content')+1])
            self.assertEqual(elements[0], {'type':'mention_user', 'text':'ou_person'})
            self.message = elements[1]['text']
            if dry:
                self.previews += 1
                data = {'file_token':'BaseFixture', 'context':{'app_id':'cli_demo'}, 'api':[{
                    'method':'POST', 'url':'/open-apis/drive/v1/files/BaseFixture/new_comments',
                    'body':{'file_type':'bitable', 'anchor':self.anchor.copy(), 'reply_elements':[
                        {'type':'mention_user', 'mention_user':'ou_person'}, elements[1]]}}]}
                self.preview_edit(data)
            else:
                self.sends += 1
                self.assertEqual(len(list(self.project.directory.glob('*.intent'))), 1)
                data = dict(file_token='BaseFixture', file_type='bitable', comment_mode='base_record',
                            base_block_id='tblWork', base_view_id='vewPlan', base_record_id='recWork',
                            comment_id='comment1', reply_id='reply1')
                self.result_edit(data)
        else:
            self.assertEqual(cmd[2], '+batch-query-comments')
            self.assertEqual(cmd[cmd.index('--comment-ids')+1], 'comment1')
            self.readbacks += 1
            reply = {'user_id':'ou_assistant', 'reply_id':'reply1',
                     'extra':{'notify_extra':{'record':'recWork','table':'tblWork','view':'vewPlan'}},
                     'content':{'elements':[{'type':'person','person':{'user_id':'ou_person'}},
                                            {'type':'text_run','text_run':{'text':self.message}}]}}
            data = {'file_token':'BaseFixture','file_type':'bitable','items':[{
                'is_whole':False,'user_id':'ou_assistant','comment_id':'comment1','reply_list':{'replies':[reply]}}]}
            self.readback_edit(reply)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'ok':True,'identity':'bot','dry_run':dry,'data':data}), '')

    def invoke(self):
        return notice.run(self.project.path, 'recWork', 'ou_person', 1)

    def test_post_and_read_back_only_once_for_prepared_plan(self):
        result = self.invoke()
        self.assertEqual(result['status'], 'notified', result)
        self.assertIn('点「完成节点」', self.message)
        self.assertEqual(self.invoke(), result)
        self.assertEqual((self.previews,self.sends,self.readbacks), (1,1,2))
        self.get_prepared.assert_called_with(self.project.path, 'recRequirement', project=self.project)
        self.directory.assert_called_with(self.project.reader, 'ou_person')
        self.assertEqual(len(list(self.project.directory.glob('*.verified'))), 1)

    def test_rejects_changed_work_or_missing_prepared_before_preview(self):
        for k, new in [('project',[{'id':'recOther'}]), ('requirements', []), ('plan_confirmer',[{'id':'ou_other'}]),
                       ('plan_revision', 2), ('status',['可开始']), ('nature',['历史记录']), ('scope','已改范围')]:
            with self.subTest(k=k), mock.patch.dict(self.work, {k:new}):
                self.assertEqual(self.invoke()['status'], 'pending')
        self.prepared['status'] = 'pending'
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual((self.previews,self.sends), (0,0))

    def test_missing_pinned_view_or_person_blocks_send(self):
        self.project.reader.base.pages.return_value = [dict(self.view, id='vewOther')]
        self.assertEqual(self.invoke()['status'], 'pending')
        self.project.reader.base.pages.return_value = [self.view]
        self.directory.side_effect = notice.plan.confirmation.Pending('通讯录未能核实')
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.previews, 0)

    def test_preview_rejects_top_level_or_other_row(self):
        for anchor in ({}, dict(self.anchor, base_record_id='recOther')):
            self.preview_edit = lambda d: d['api'][0]['body'].update(anchor=anchor)
            self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.sends, 0)
        self.assertFalse(list(self.project.directory.glob('*.intent')))

    def test_row_edit_between_preview_and_send(self):
        self.preview_edit = lambda d: self.work.update(plan='全新的方案')
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.sends, 0)

    def test_timeout_after_send_never_resends(self):
        def lose_response(data):
            raise subprocess.TimeoutExpired('drive', 45)
        self.result_edit = lose_response
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual((self.sends,self.readbacks), (1,0))

    def test_resume_readback_only_after_wrong_anchor_person_author_or_text(self):
        for edit in (lambda r: r['extra']['notify_extra'].update(record='recOther'),
                     lambda r: r.update(user_id='ou_other'),
                     lambda r: r['content']['elements'][0]['person'].update(user_id='ou_other'),
                     lambda r: r['content']['elements'][1]['text_run'].update(text='别的内容')):
            self.readback_edit = edit
            self.assertEqual(self.invoke()['status'], 'pending')
            self.assertFalse(list(self.project.directory.glob('*.verified')))
        self.readback_edit = lambda r: None
        self.assertEqual(self.invoke()['status'], 'notified')
        self.assertEqual((self.sends,self.readbacks), (1,5))

    def test_duplicate_invocation_while_sending_cannot_send(self):
        self.result_edit = lambda d: self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.invoke()['status'], 'notified')
        self.assertEqual(self.sends, 1)
