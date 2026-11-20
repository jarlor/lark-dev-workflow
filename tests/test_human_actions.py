import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'service'))
import human_actions as actions


def link(identifier):
    return [{'id': identifier}]


class HumanActions(unittest.TestCase):
    def setUp(self):
        schema = json.loads((REPO / 'template/schema.json').read_text())['tables']
        self.tables = {}
        for role, spec in schema.items():
            fields = {}
            for name, f in spec['fields'].items():
                field = dict(f, id='field_' + name)
                if 'target_role' in f:
                    field['target_table_id'] = 'tbl' + f['target_role']
                fields[name] = field
            self.tables[role] = dict(id='tbl' + role, fields=fields)
        self.binding = dict(format_version=2, project_number='PRJ-001', tables=self.tables, documents={},
            base=dict(token='baseToken', url='https://example.feishu.cn/base/baseToken?view=old'),
            scope=dict(mode='project_record', project_record_id='recProject', project_table_id='tblprojects'),
            cli_actor=dict(profile='test', identity='bot', app_id='cli_test', open_id='ou_bot'))
        self.snapshot = dict(format_version=1, binding=actions.comments.binding_identity(self.binding),
                             records={}, comments={})
        self.row('projects', 'recProject', number='PRJ-001', status=['进行中'])

    def row(self, role, record, **values):
        fields = self.tables[role]['fields']
        source = dict(nature=['当前工作'], project=link('recProject'), number='ID-1', title='检查筛选条件')
        source.update(values)
        result = {f['id']: source.get(name) for name, f in fields.items()}
        self.snapshot['records'][self.tables[role]['id'] + '/' + record] = dict(role=role, fields=result)
        return result

    def comment(self, role, record, text, *, author='ou_bot', mentions=('ou_person',), time=100, solved=False):
        key = str(len(self.snapshot['comments']))
        entry = dict(location=dict(status='in_scope', table_id='tbl'+role, record_id=record,
                                   project_record_id='recProject'),
                     content=dict(create_time=time, author=author, text=text, resource_type='bitable',
                                  resource_token='baseToken', body=dict(elements=[
                                      dict(type='person', person=dict(user_id=value)) for value in mentions])),
                     thread=dict(is_solved=solved))
        self.snapshot['comments'][key] = entry
        return entry

    def result(self):
        return actions.by_recipient(self.binding, self.snapshot)

    def test_gates_are_personal_and_record_links_are_canonical(self):
        self.row('requirements', 'recR', status=['待确认'], confirmer=link('ou_one'), acceptor=link('ou_two'),
                 scope='筛选', criteria='按日期显示')
        self.row('bugs', 'recB', status=['待验收'], acceptor=link('ou_two'))
        self.row('work', 'recW', status=['待确认计划'], plan_confirmer=link('ou_three'),
                 scope='筛选', deliverable='页面', verification_plan='测试日期', plan='按日期筛选')
        output = self.result()
        self.assertEqual({'ou_one':'scope', 'ou_two':'acceptance', 'ou_three':'plan'},
                         {k:v[0]['kind'] for k,v in output.items()})
        self.assertEqual(output['ou_one'][0]['url'], 'https://example.feishu.cn/base/baseToken?table=tblrequirements&record=recR')
        self.assertEqual(len(output['ou_one']), 1)

    def test_unrelated_terminal_historical_bot_and_ambiguous_assignees(self):
        for rid, changes in {
            'recOther':dict(project=link('recOther')), 'recForeign':dict(project=link('recProject')+link('recOther')),
            'recOld':dict(nature=['历史记录']), 'recDone':dict(status=['已验收']), 'recBot':dict(acceptor=link('ou_bot')),
            'recAmbiguous':dict(acceptor=link('ou_a')+link('ou_b')), 'recPaused':dict(processing=['暂停']),
        }.items():
            data = dict(status=['待验收'], acceptor=link('ou_person'))
            self.row('requirements', rid, **(data | changes))
        self.assertEqual(self.result(), {})
        self.snapshot['binding'] = {}
        with self.assertRaises(ValueError):
            self.result()

    def test_flagged_feedback_uses_latest_real_mention_and_drops_answered_or_resolved(self):
        self.row('feedback', 'recF', status=['已转办'], author='同名', human_action=True,
                 next_step='开发者继续检查', requirements=link('recElsewhere'))
        self.comment('feedback', 'recF', '已定位筛选页。请发一下当时的筛选栏截图。')
        item = self.result()['ou_person'][0]
        self.assertEqual(item['action'], '请发一下当时的筛选栏截图。')
        reply = self.comment('feedback', 'recF', '见截图', author='ou_person', time=101)
        self.assertEqual(self.result(), {})
        reply['content']['author'] = 'ou_bot'
        reply['content']['text'] = '请补充截图。'
        reply['thread']['is_solved'] = True
        self.assertEqual(self.result(), {})

    def test_legacy_text_author_and_stale_gate_text_cannot_route_reminders(self):
        self.row('feedback', 'recF', status=['待补充'], author='ou_person', next_step='请补充截图。')
        self.assertEqual(self.result(), {})
        self.comment('feedback','recF','请完成节点，有问题吗？')
        self.assertEqual(self.result(), {})
        self.comment('feedback','recF','请发一下截图。', mentions=['ou_one','ou_two'], time=101)
        self.assertEqual(self.result(), {})

    def test_unassigned_feedback_must_match_exact_project_number(self):
        self.tables['feedback']['fields']['author']['type'] = 'user'
        row = self.row('feedback','recF',status=['待补充'], project=[], project_input=['PRJ-001 · 测试'],
                       next_step='请补充截图。', author=link('ou_person'))
        self.assertEqual(self.result()['ou_person'][0]['record_id'], 'recF')
        row['field_project_input'] = ['PRJ-0010']
        self.assertEqual(self.result(), {})

    def test_feedback_source_uses_exact_in_scope_event_and_original_author(self):
        source = self.comment('requirements','recR','页面显示 0', author='ou_reporter', mentions=())
        source['content'].update(comment_id='comment42', reply_id='reply1')
        key = 'bitable:baseToken/comment:comment42/reply:reply1'
        self.snapshot['comments'] = {key:source}
        self.row('feedback','recF',status=['待补充'],author='收件人',source_event_key=key,
                 next_step='请补充页面地址。')
        self.assertEqual(self.result()['ou_reporter'][0]['action'], '请补充页面地址。')
        source['location']['project_record_id'] = 'recAnotherProject'
        self.assertEqual(self.result(), {})

    def test_tied_comment_times_never_authorize_an_ambiguous_recipient(self):
        self.tables['feedback']['fields']['author']['type'] = 'user'
        self.row('feedback','recF',status=['待补充'],author=link('ou_one'),next_step='请补充地址。')
        self.comment('feedback','recF','请补充地址。', mentions=['ou_one'], time=100)
        self.comment('feedback','recF','已回复', author='ou_one', time=100)
        self.assertEqual(self.result(), {})

    def test_human_development_owner_is_distinct_from_plan_confirmer_and_dependencies(self):
        values = dict(status=['可开始'], human_action=True, next_step='阿明已接手。请按计划开发。',
                      owner=link('ou_owner'), plan_confirmer=link('ou_confirmer'))
        self.row('work','recActive', **values)
        self.row('work','recDep', **values, processing=['等待前置'])
        self.row('work','recBot', **(values | dict(owner=link('ou_bot'))))
        self.assertEqual([(k,[i['action'] for i in v]) for k,v in self.result().items()],
                         [('ou_owner',['请按计划开发。'])])

    def test_reassignment_overrides_former_owner_and_acceptor_mentions(self):
        row = self.row('work', 'recW', status=['可开始'], owner=link('ou_new'),
                       next_step='请按第 2 版开发。', human_action=True)
        self.comment('work', 'recW', '请补充此前的操作。', mentions=['ou_old'])
        self.assertEqual(set(self.result()), {'ou_new'})
        row['field_next_step'] = '请在「负责人」栏选好负责开发的人。'
        self.assertNotIn('负责人', self.result()['ou_new'][0]['action'])
        for assignee in (link('ou_bot'), [], link('ou_one') + link('ou_two')):
            row['field_owner'] = assignee
            self.assertEqual(self.result(), {})
        self.row('requirements','recAccepted',status=['已验收'],acceptor=link('ou_new'),human_action=True)
        self.comment('requirements','recAccepted','请补充试用时间。',mentions=['ou_old'])
        self.assertEqual(self.result(), {})

    def test_link_in_an_update_is_not_a_question(self):
        self.row('feedback','recF',status=['已转办'],human_action=True)
        self.comment('feedback','recF','筛选结果已记录 https://example.feishu.cn/base/demo?table=tblData。')
        self.assertEqual(self.result(), {})

    def test_post_acceptance_followup_directs_only_to_the_actual_linked_test(self):
        self.row('requirements','recR',status=['已验收'],acceptor=link('ou_person'),human_action=True,
                 next_step='TEST-0070 的试用步骤及结果待补。',verification=link('recTest'))
        self.row('verification','recTest',number='TEST-0070',method=['人工核验'],result=['未执行'],
                 purpose=['需求验收'],requirements=link('recR'),summary='实际步骤和结果待补。')
        self.comment('requirements','recR','请在 TEST-0070 补充当时的操作和结果。')
        items = self.result()['ou_person']
        self.assertEqual([(i['kind'],i['url']) for i in items],
                         [('evidence','https://example.feishu.cn/base/baseToken?table=tblverification&record=recTest')])

    def test_test_evidence_requires_exact_record_invite_recipient_and_both_current_links(self):
        row = self.row('requirements','recR',status=['已验收'], acceptor=link('ou_person'),
                       next_step='TEST-0070 的试用步骤及结果待补。', verification=link('recTest'))
        test = self.row('verification','recTest',number='TEST-0070',method=['人工核验'],result=['未执行'],
                        purpose=['需求验收'],requirements=link('recR'),summary='实际角色、步骤和结果待补。',
                        evidence='已完成验收节点，待补试用材料。')
        invitation = self.comment('requirements','recR','请按 TEST-0070 检查，通过后完成「待验收」节点。')
        self.assertEqual(self.result()['ou_person'][0]['kind'], 'evidence')
        self.assertNotIn('节点', self.result()['ou_person'][0]['action'])
        row['field_status'] = ['待验收']
        self.assertEqual([i['kind'] for i in self.result()['ou_person']], ['acceptance'])
        row['field_status'] = ['已验收']
        for key, value in [('field_acceptor',link('ou_other')),('field_verification',[]),
                           ('field_next_step','TEST-00701 的结果待补。')]:
            original = row[key]
            row[key] = value
            self.assertEqual(self.result(), {})
            row[key] = original
        test['field_result'] = ['通过']
        self.assertEqual(self.result(), {})
        test['field_result'] = ['未执行']
        invitation['content']['text'] = '请按 TEST-00701 检查。'
        self.assertEqual(self.result(), {})

    def test_assigned_manual_tester_can_differ_from_gate_acceptor(self):
        self.tables['verification']['fields']['executor']['type'] = 'user'
        self.row('bugs','recB',status=['待验收'],acceptor=link('ou_person'),retest_tests=link('recTest'))
        self.row('verification','recTest',number='TEST-001',method=['人工核验'],result=['未执行'],
                 purpose=['Bug复测'],retested_bugs=link('recB'),executor=link('ou_tester'))
        self.assertEqual({k:v[0]['kind'] for k,v in self.result().items()},
                         {'ou_tester':'verification','ou_person':'acceptance'})
