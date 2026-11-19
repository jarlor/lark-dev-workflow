import copy
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import plan_notice as notice
import deferred_notices


class PlanNoticeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = mock.Mock(directory=Path(temp.name), token='BaseFixture', project='recProject',
                                 open_id='ou_assistant', path=Path(temp.name) / 'bridge',
                                 signature='bound', config={'app_id': 'cli_demo'})
        self.view = {'id': 'vewPlan', 'name': '待确认计划', 'type': 'grid'}
        self.project.tables = {'work': {'id': 'tblWork', 'views': [self.view],
                                        'fields': {'plan': {'name': '研发计划'}, 'status': {'id': 'fldStage'},
                                                   'project': {'id': 'fldProject'}}}}
        self.now = 1700000000
        clock = mock.patch.object(notice.time, 'time', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        p = self.project
        p.config.update(tenant_key='org', state_dir=temp.name, table_ids=['tblWork'], base_token=p.token,
                        project_record_id=p.project, bot_open_id=p.open_id, lark_profile='dedicated',
                        runner_config=str(p.directory / 'runner.json'))
        p.binding = dict(base={'token': p.token}, tables=p.tables, scope={'project_record_id': p.project},
                         cli_actor={'identity': 'bot', 'open_id': p.open_id})
        p.paths = tuple(str(p.directory / name) for name in ('workdir', 'runner-state', 'binding.json'))
        Path(p.paths[1]).mkdir()
        with mock.patch.object(notice.plan.bridge, 'runner_pins', return_value=p.paths):
            self.journal = notice.plan.bridge.Journal(p.config)
        self.index = dict(signature=notice.gate_events.signature(p.binding, 'org'), started_at=self.now-5,
                          observed_at=self.now-4, tables={'tblWork': dict(records=['recWork'],
                          project_field='fldProject', native_stage_field_id='fldStage', fields={'fldStage':'status'},
                          since={'recWork': self.now-4})})
        self.scope_file = Path(p.paths[1]) / notice.gate_events.INDEX
        self.scope_file.write_text(json.dumps(self.index))
        self.receiver = (p.directory / 'receiver.lock').open('w+b')
        self.addCleanup(self.receiver.close)
        fcntl.flock(self.receiver, fcntl.LOCK_EX)
        self.receiver.write(str(self.now-10).encode())
        self.receiver.flush()
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

    def test_wait_for_new_row_then_send_original_notice_once(self):
        scope = copy.deepcopy(self.index)
        scope['tables']['tblWork']['records'] = []
        self.scope_file.write_text(json.dumps(scope))
        self.assertEqual(self.invoke()['status'], 'pending')
        request_file, = (self.project.directory / 'pending-plan-notices').glob('*.json')
        request = json.loads(request_file.read_text())
        self.assertEqual(request_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((request['confirmer'], request['anchor']), ('ou_person', self.anchor))
        self.assertEqual(self.sends, 0)
        self.scope_file.write_text(json.dumps(self.index))
        self.assertEqual(notice.run(self.project.path, 'recWork', 'ou_person', 1,
                                    expected_request=dict(request, contents='other'))['status'], 'pending')
        self.assertEqual(self.sends, 0)
        result = notice.run(self.project.path, 'recWork', 'ou_person', 1, expected_request=request)
        self.assertEqual(result['status'], 'notified', result)
        self.assertFalse(request_file.exists())
        self.assertEqual(self.sends, 1)
        fcntl.flock(self.receiver, fcntl.LOCK_UN)
        self.assertEqual(self.invoke(), result)
        self.assertEqual(self.sends, 1)

    def test_wrong_or_stale_scope_and_same_second_never_send(self):
        for mutate in (lambda s: s.update(signature='other'),
                       lambda s: s.update(started_at=self.now-100000, observed_at=self.now-90000),
                       lambda s: s['tables']['tblWork'].update(native_stage_field_id='other'),
                       lambda s: s['tables']['tblWork'].update(project_field='other'),
                       lambda s: s['tables']['tblWork'].update(fields={'fldStage':'revision'}),
                       lambda s: (s.update(observed_at=self.now), s['tables']['tblWork']['since'].update(recWork=self.now))):
            scope = copy.deepcopy(self.index)
            mutate(scope)
            self.scope_file.write_text(json.dumps(scope))
            self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.sends, 0)

    def test_barriers_must_allow_an_immediate_click(self):
        for table, record, closed, opened in (('', '', self.now-3, None), ('tblWork', 'recWork', self.now-3, None),
                                             ('', '', self.now-2, self.now)):
            with self.journal.connect() as db:
                db.execute('DELETE FROM membership_barriers')
                db.execute('INSERT INTO membership_barriers VALUES (?,?,?,?)', (table, record, closed, opened))
            self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.sends, 0)
        with self.journal.connect() as db:
            db.execute('UPDATE membership_barriers SET closed_at=?, reopened_at=NULL', (self.now-6,))
        self.assertEqual(self.invoke()['status'], 'notified')

    def test_only_current_locked_receiver_and_bound_journal_can_trigger_invitation(self):
        self.receiver.seek(0)
        self.receiver.truncate()
        self.receiver.write(str(self.now).encode())
        self.receiver.flush()
        self.assertEqual(self.invoke()['status'], 'pending')
        fcntl.flock(self.receiver, fcntl.LOCK_UN)
        self.receiver.seek(0)
        self.receiver.write(str(self.now-3).encode())
        self.receiver.flush()
        self.assertEqual(self.invoke()['status'], 'pending')
        fcntl.flock(self.receiver, fcntl.LOCK_EX)
        with self.journal.connect() as db:
            db.execute("UPDATE writer SET binding='foreign'")
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.sends, 0)

    def test_deferred_send_preserves_original_project_notification_rules(self):
        self.project.binding['governance'] = {'rules_reference': 'rules.md'}
        self.index['signature'] = notice.gate_events.signature(self.project.binding, 'org')
        self.scope_file.write_text(json.dumps(self.index))
        rules = Path(self.project.directory / 'rules.md')
        rules.write_text('本测试允许在计划下提醒负责人。')
        self.project.runner = {'workdir': str(self.project.directory)}
        with mock.patch.object(notice.unattended.control, 'actor_for_runner'):
            fcntl.flock(self.receiver, fcntl.LOCK_UN)
            self.assertEqual(self.invoke()['status'], 'pending')
            request_file, = (self.project.directory / 'pending-plan-notices').glob('*.json')
            request = json.loads(request_file.read_text())
            rules.write_text('暂停所有新通知。')
            fcntl.flock(self.receiver, fcntl.LOCK_EX)
            result = notice.run(self.project.path, 'recWork', 'ou_person', 1, expected_request=request)
            self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.sends, 0)
        self.assertEqual(json.loads(request_file.read_text()), request)

    def test_next_scan_delivers_previously_requested_notice_without_a_model_or_duplicate(self):
        self.scope_file.unlink()
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(self.sends, 0)
        p = self.project
        p.config['mode'] = 'dispatch'
        p.runner = dict(state_dir=p.paths[1], workdir=p.paths[0], native_stage_mode='prepare')
        runtime = notice.unattended.execution_directory(p.runner)
        runtime.mkdir()
        (runtime / 'state.json').write_text(json.dumps(dict(format_version=1, identity='test', checkpoint='scan')))
        pin = deferred_notices.binding_pin(p.config, p.paths)
        self.scope_file.write_text(json.dumps(self.index))
        with mock.patch.object(notice.plan.bridge, 'config_from', return_value=p.config), \
                mock.patch.object(notice.plan.bridge, 'runner_pins', return_value=p.paths), \
                mock.patch.object(notice.unattended, 'identity', return_value='test'):
            with notice.unattended.lock_workspace(p.runner):
                self.assertEqual(deferred_notices.retry_one(p.path, pin), 'busy')
            self.assertEqual(deferred_notices.retry_one(p.path, 'wrong'), 'binding_changed')
            self.assertEqual(self.sends, 0)
            self.assertEqual(deferred_notices.retry_one(p.path, pin), 'notified')
            self.assertEqual(deferred_notices.retry_one(p.path, pin), 'empty')
        self.assertEqual((self.sends, self.readbacks), (1, 1))

    def test_stale_queued_invitation_cannot_be_retargeted(self):
        fcntl.flock(self.receiver, fcntl.LOCK_UN)
        self.assertEqual(self.invoke()['status'], 'pending')
        request_file, = (self.project.directory / 'pending-plan-notices').glob('*.json')
        original = request_file.read_bytes()
        fcntl.flock(self.receiver, fcntl.LOCK_EX)
        self.work['plan'] = self.prepared['values']['plan'] = '读回的是别的内容'
        self.assertEqual(self.invoke()['status'], 'pending')
        self.assertEqual(request_file.read_bytes(), original)
        self.assertEqual(self.sends, 0)

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
