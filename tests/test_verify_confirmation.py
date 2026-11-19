import copy
import json
from pathlib import Path
import sys
import subprocess
import time
from unittest import mock

from test_native_stage_inspection import ProjectionReader
from test_unattended import RuntimeFixture, write_json
import gate_events
import watch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import event_bridge as bridge
import verify_confirmation as verifier


class ConfirmationTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.reader = ProjectionReader()
        self.binding = self.reader.config
        self.role = 'work'
        self.table = self.binding['tables'][self.role]
        self.fields = self.table['fields']
        self.fields['plan_confirmer'] = self.fields.pop('confirmer')
        self.steps = self.fields['status']['stage_contract']['steps']
        self.steps[0]['person_field'] = 'plan_confirmer'
        for key, kind in [('plan_revision', 'number'), ('scope', 'text'), ('deliverable', 'text'),
                          ('verification_plan', 'text'), ('next_step', 'text')]:
            self.fields[key] = {'id': 'fld' + key, 'type': kind}
        self.row = self.reader.data['tblWork'][0]['values']
        self.row.update(fldConfirmer=[{'id':'ou_person', 'name':'小吴'}], fldPlan='展示预计送达日期',
                        fldplan_revision=2, fldscope='订单详情', flddeliverable='可见日期',
                        fldverification_plan='核对两种配送方式', fldnext_step='等待确认', fldStage=['可开始'])
        self.config.update(format_version=3, event_tenant_key='tenant_demo')
        self.save_config()
        write_json(self.project_path, self.binding)
        self.service = dict(mode='observe', lark_profile=self.reader.profile, app_id='cli_demo',
                            tenant_key='tenant_demo', bot_open_id='ou_assistant', base_token='BaseFixture',
                            project_record_id='recA', table_ids=['tblWork'], runner_config=str(self.config_path),
                            state_dir=str(self.root / 'event-state'))
        self.service_path = self.root / 'service.json'
        write_json(self.service_path, self.service)
        self.now = int(time.time())
        with mock.patch.object(gate_events.time, 'time', return_value=self.now-20):
            self.journal = bridge.Journal(self.service)
            self.directory = Path(self.config['state_dir'])
            snapshot = watch.collect_with_reader(self.binding, self.reader, inspect_native_stages=True,
                                                 comments_state_path=self.directory/'inspect'/'comments.json')
            gate_events.publish_scope(self.config, self.binding, snapshot, self.directory)
        self.change = dict(field_id='fldStage', field_type='Stage', before='待确认计划', after='可开始')
        self.candidate = {'activity_type':'update', 'rev':19, 'create_time':self.now-5,
                          'operator':'小吴', 'field_changes':[self.change]}
        self.entries = [self.candidate, dict(activity_type='create', rev=3, create_time=self.now-10)]
        self.reader.base.read.side_effect = self.history_page
        raw_fields = self.reader.runner
        self.directory_people = {'ou_person'}
        def read_only(argv, **kwargs):
            if '/open-apis/contact/v3/users/' not in argv[3]:
                return raw_fields(argv, **kwargs)
            self.assertEqual((argv[2], argv[4:8]), ('GET', ['--as','bot','--profile',self.reader.profile]))
            key = argv[3].rsplit('/',1)[1]
            found = key in self.directory_people
            body = dict(ok=found, identity='bot', data={'user':{'open_id':key}} if found else {})
            return subprocess.CompletedProcess(argv, 0 if found else 1, json.dumps(body),'')
        self.reader.runner = read_only
        self.event()

    def history_page(self, command, *args):
        self.assertEqual(command, '+record-history-list')
        self.assertEqual(args[:6], ('--base-token','BaseFixture','--table-id',self.table['id'],
                                    '--record-id','recWork'))
        if args[-1] == '1':
            return dict(items=copy.deepcopy(self.entries[:1]), has_more=len(self.entries)>1)
        return dict(items=copy.deepcopy(self.entries), has_more=False)

    def event(self, *, event_id='evt19', rev=19, actor='ou_person', before='待确认计划', after='可开始'):
        envelope = dict(header=dict(app_id='cli_demo', tenant_key='tenant_demo', event_type=bridge.RECORD,
                                    event_id=event_id),
                        event=dict(file_type='bitable',file_token='BaseFixture',table_id='tblWork',
                                   revision=rev,update_time=self.now-5,operator_id={'open_id':actor},
                                   action_list=[dict(record_id='recWork',action='record_edited',
                                             before_value=[dict(field_id='fldStage',field_value=json.dumps(before))],
                                             after_value=[dict(field_id='fldStage',field_value=json.dumps(after))])]))
        bridge.receive(envelope, self.service, self.journal)

    def verify(self):
        return verifier.verify(self.service_path, self.role, 'recWork', reader_factory=lambda *a, **k: self.reader)

    def test_retained_original_event_is_reread_and_must_match_row_and_current_history(self):
        with self.journal.connect() as db:
            event = json.loads(db.execute('SELECT data FROM receipts').fetchone()[0])
            db.execute('DELETE FROM receipts')
        def check(source):
            return verifier.verify(self.service_path, self.role, 'recWork',
                                   reader_factory=lambda *a, **k: self.reader, receipt_reader=source)
        source = mock.Mock(return_value=event)
        self.assertEqual(self.verify()['status'], 'pending')
        self.assertEqual(check(source)['status'], 'verified')
        self.assertEqual(source.call_count, 2)
        self.assertEqual(check(lambda *args: dict(event, record_id='recOther'))['status'], 'pending')
        self.assertEqual(check(mock.Mock(side_effect=[event,dict(event, event_id='other')]))['status'], 'pending')
        self.change['after'] = '已撤回'
        self.assertEqual(check(source)['status'], 'pending')

    def test_unique_human_transition_and_stable_version_from_real_receiver(self):
        self.entries.insert(0, dict(activity_type='update', rev=21, create_time=self.now-4,
                                    operator='其他人', field_changes=[dict(field_id='fldnext_step')]))
        before = self.journal.path.read_bytes()
        result = self.verify()
        self.assertEqual((result['status'],result.get('event_id'),result.get('business_revision')),
                         ('verified','evt19',2), result)
        self.assertEqual(before, self.journal.path.read_bytes())

    def test_same_name_is_not_identity_and_automation_history_is_not_a_click(self):
        self.row['fldConfirmer'] = [{'id':'ou_other','name':'小吴'}]
        self.assertEqual(self.verify()['status'], 'pending')
        self.row['fldConfirmer'] = [{'id':'ou_person','name':'小吴'}]
        self.candidate['operator'] = '多维表格助手'
        self.assertEqual(self.verify()['status'], 'pending')

    def test_person_with_native_automation_name_or_delegated_source_never_verifies(self):
        self.row['fldConfirmer'][0]['name'] = '多维表格助手'
        self.candidate['operator'] = '多维表格助手'
        self.assertEqual(self.verify()['status'], 'pending')
        self.row['fldConfirmer'][0]['name'] = self.candidate['operator'] = '小吴'
        self.candidate['source_type'] = 'automation'
        self.assertEqual(self.verify()['status'], 'pending')

    def test_link_only_plan_does_not_prove_the_document_contents(self):
        for pointer in ('详见：https://example.test/docs/plan', '详细方案请打开 https://example.test/plan',
                        '[测试计划](https://example.test/plan)（见附件）', '见附件'):
            with self.subTest(pointer=pointer):
                self.row['fldPlan'] = pointer
                self.assertEqual(self.verify()['status'], 'pending')
        self.row['fldPlan'] = '打开 https://example.test 搜索酒店，结果应显示两条可订日期。'
        self.assertEqual(self.verify()['status'], 'verified')

    def test_another_robot_in_people_field_and_event_is_not_a_directory_user(self):
        with self.journal.connect() as conn:
            row = json.loads(conn.execute('SELECT data FROM receipts').fetchone()[0])
            row['operator_open_id'] = 'ou_another_app'
            conn.execute('UPDATE receipts SET data=?',(json.dumps(row),))
        self.row['fldConfirmer'] = [{'id':'ou_another_app', 'name':'小吴'}]
        self.assertEqual(self.verify()['status'], 'pending')
        self.directory_people.clear()
        self.row['fldConfirmer'] = [{'id':'ou_person', 'name':'小吴'}]
        self.assertEqual(self.verify()['status'], 'pending')

    def test_ambiguous_second_even_with_different_actor_and_revision(self):
        self.event(event_id='evt20', rev=20, actor='ou_other')
        self.assertEqual(self.verify()['status'], 'pending')

    def test_wrong_revision_and_time_never_substitute_for_each_other(self):
        self.candidate['rev'] = 18
        self.assertEqual(self.verify()['status'], 'pending')
        self.candidate.update(rev=19, create_time=self.now-6)
        self.assertEqual(self.verify()['status'], 'pending')

    def test_scope_or_plan_or_actor_change_same_later_or_reverted_is_not_confirmed(self):
        for field in ('fldConfirmer', 'fldPlan', 'fldscope', 'fldProject', 'fldplan_revision'):
            with self.subTest(field=field):
                change = dict(field_id=field,before='x',after='y')
                self.candidate['field_changes'].append(change)
                self.assertEqual(self.verify()['status'], 'pending')
                self.candidate['field_changes'].pop()
                self.entries[:0] = [dict(activity_type='update', rev=23, create_time=self.now-2,
                                         field_changes=[dict(change,before='y',after='x')]),
                                    dict(activity_type='update', rev=22, create_time=self.now-3,field_changes=[change])]
                self.assertEqual(self.verify()['status'], 'pending')
                del self.entries[:2]

    def test_late_ambiguous_history_and_unseen_page_fail_closed(self):
        self.entries.insert(0, dict(self.candidate, rev=20, operator='小吴（同名）'))
        self.assertEqual(self.verify()['status'], 'pending')
        self.entries.pop(0)
        self.reader.base.read.side_effect = lambda *a: dict(items=self.entries, has_more=True)
        self.assertEqual(self.verify()['status'], 'pending')

    def test_raw_option_ids_can_only_be_matched_to_the_one_native_history_change(self):
        with self.journal.connect() as conn:
            conn.execute('DELETE FROM receipts')
        self.event(event_id='evtOpaque', before='optAlpha', after='optBeta')
        index = json.loads((self.directory/gate_events.INDEX).read_text())
        self.assertEqual(self.verify()['status'], 'verified')
        self.change['after'] = '流程暂停'
        self.assertEqual(self.verify()['status'], 'pending')
        self.change['after'] = '可开始'
        del index['tables']['tblWork']['native_stage_field_id']
        write_json(self.directory/gate_events.INDEX,index)
        self.assertEqual(self.verify()['status'], 'pending')

    def test_single_project_private_journal_and_global_membership_barrier(self):
        self.row['fldProject'] = [{'id':'otherProject'}]
        self.assertEqual(self.verify()['status'], 'pending')
        self.row['fldProject'] = [{'id':'recA'}]
        with self.journal.connect() as db:
            db.execute("INSERT INTO membership_barriers VALUES ('','',?,NULL)", (self.now,))
        self.assertEqual(self.verify()['status'], 'pending')
        with self.journal.connect() as db:
            db.execute('DELETE FROM membership_barriers')
            db.execute("UPDATE writer SET binding='different_runner'")
        self.assertEqual(self.verify()['status'], 'pending')

    def test_scope_confirmations_for_requirements_and_bugs(self):
        # Exercise each scope alias through the paired database, record read and its actual field history.
        for role in ('requirements','bugs'):
            with self.subTest(role=role):
                prior_role = self.role
                self.binding['tables'][role], self.binding['tables'][prior_role] = (
                    self.binding['tables'][prior_role], self.binding['tables'][role])
                self.role = role
                if prior_role == 'work':
                    self.fields['confirmer'] = self.fields.pop('plan_confirmer')
                    self.fields['revision'] = self.fields.pop('plan_revision')
                    self.fields['criteria'] = self.fields.pop('deliverable')
                    self.steps[0].update(name='待确认', actor='human', person_field='confirmer',
                                         required_fields=['scope', 'criteria'])
                    self.steps[1]['name'] = '可研发'
                    self.fields['status']['options'][0]['name']='待确认'
                    self.fields['status']['options'][1]['name']='可研发'
                    self.reader.options = copy.deepcopy(self.fields['status']['options'])
                    self.row['fldStage']=['可研发']
                    self.change.update(before='待确认', after='可研发')
                write_json(self.project_path, self.binding)
                with mock.patch.object(gate_events.time, 'time', return_value=self.now-10):
                    snapshot = watch.collect_with_reader(self.binding,self.reader,inspect_native_stages=True,
                                             comments_state_path=self.directory/role/'comments.json')
                    gate_events.publish_scope(self.config,self.binding,snapshot,self.directory)
                self.event(event_id='evt'+role, before='待确认', after='可研发')
                result=self.verify()
                self.assertEqual((result['status'],result.get('business_revision'),result['dispatch_checked']),
                                 ('verified',2,False),result)

    def test_live_history_head_checked_again_after_readback(self):
        def stale_head(command, *args):
            if args[-1] == '1':
                return dict(items=[dict(self.candidate,rev=60)],has_more=True)
            return self.history_page(command,*args)
        self.reader.base.read.side_effect = stale_head
        self.assertEqual(self.verify()['status'], 'pending')
