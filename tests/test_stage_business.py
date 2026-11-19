import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'service'))
import stage_action
import stage_business as business


class Project:
    def __init__(self, tmp, role):
        self.path, self.directory, self.paths = 'service.json', tmp, ('workdir', str(tmp), 'project.json')
        self.role, self.table, self.project, self.bot = role, 'tblCurrent', 'recProject', 'ou_agent'
        self.config = dict(app_id='cli_test', tenant_key='tenant', bot_open_id=self.bot, base_token='base',
                           project_record_id=self.project, lark_profile='test', table_ids=[self.table])
        self.identity, self.token = 'bound', 'base'
        self.rule = {'marker_field_id': 'fldMarker'}
        table = json.loads((REPO / 'template' / 'schema.json').read_text())['tables'][role]
        self.fields = {k: dict(v, id='fld' + str(i)) for i, (k, v) in enumerate(table['fields'].items())}
        self.binding = {'tables': {role: {'id': self.table, 'fields': self.fields}}}
        self.row = {k: None for k in self.fields}
        self.before, after, self.person, self.revision, texts = business.confirmation.GATES[role]
        self.row.update(nature=['当前工作'], processing=['正常'], status=[after], project=[{'id': self.project}],
                        title='显示进度', key='current', feedback=[{'id': 'recFeedback'}])
        self.row[self.revision] = 2
        self.row[self.person] = [{'id': 'ou_colleague', 'name': '负责的同事'}]
        if role == 'work':
            self.row['owner'] = [{'id': self.bot, 'name': '研发助手'}]
        for key in texts:
            self.row[key] = '打开记录能查看进度和下次检查日期。'
        self.rows = []
        self.add_change('status', self.before, after, '负责的同事')
        self.reader = mock.Mock()
        self.reader.read_record.side_effect = self.read_row
        self.check_binding = mock.Mock()

    def read_row(self, *args):
        return {'record_id': 'recRow', 'values': {self.fields[k]['id']: copy.deepcopy(v) for k, v in self.row.items()}}

    def add_change(self, key, before, after, who):
        field = self.fields[key] if key in self.fields else {'id': key, 'type': 'text'}
        kind = 'Stage' if key == 'status' else field['type']
        rev = self.rows[0]['rev'] + 1 if self.rows else 37
        self.rows.insert(0, dict(rev=rev, create_time=1000 + rev, operator=who, activity_type='update',
                                 field_changes=[dict(field_id=field['id'], before=before, after=after, field_type=kind)]))

    def history(self, rid, anchor=None):
        end = next((i for i, row in enumerate(self.rows) if row['rev'] == anchor), None)
        if anchor is not None and end is None:
            raise stage_action.Pending('原版记录历史缺失。')
        return copy.deepcopy(self.rows[:1] if anchor is None else self.rows[:end+1])

    def proof(self):
        raw = next(h for h in self.rows if h['operator'] == '负责的同事' and h['field_changes'][0]['before'] == self.before)
        return dict(status='verified', check='current_record_human_transition', role=self.role,
                    project_record_id=self.project, table_id=self.table, record_id='recRow',
                    field_id=self.fields['status']['id'], business_revision=self.row[self.revision],
                    operator_open_id='ou_colleague', event_id='evt' + str(raw['rev']),
                    update_time=raw['create_time'], table_revision=raw['rev'])

    def event(self):
        proof = self.proof()
        return {**{k: proof[k] for k in ('event_id', 'table_id', 'record_id', 'field_id',
                                           'operator_open_id', 'update_time')},
                'before': self.before, 'after': business.confirmation.GATES[self.role][1],
                'event_revision': proof['table_revision']}


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.set_project('work')
        def verify(*args, receipt_reader):
            receipt_reader()
            receipt_reader()
            return self.p.proof()
        for module, fn, kwargs in (
            (stage_action, 'Project', dict(side_effect=lambda *a: self.p)),
            (business.watch, '_fields', dict(return_value={})),
            (business.confirmation, 'verify', dict(side_effect=verify)),
            (business.confirmation, 'receipt', dict(side_effect=lambda *a: self.p.event())),
        ):
            patch = mock.patch.object(module, fn, **kwargs)
            self.addCleanup(patch.stop)
            setattr(self, fn, patch.start())

    def set_project(self, role):
        folder = Path(self.tmp.name) / role
        folder.mkdir(mode=0o700, exist_ok=True)
        self.p = Project(folder, role)

    def check(self):
        return business.confirmation_for(self.p.path, self.p.role, 'recRow')

    def test_each_role_records_the_actual_click_and_only_once(self):
        for role in business.FIRST:
            with self.subTest(role=role):
                self.set_project(role)
                first = self.check()
                self.assertEqual(first['status'], 'verified')
                self.assertFalse(first['linked_work_checked'])
                self.assertEqual(first['confirmation_sha256'], self.check()['confirmation_sha256'])
                saved = json.loads(next((self.p.directory / 'stage-confirmations').glob('*.json')).read_text())
                self.assertEqual(len(saved['confirmations']), 1)
                self.assertEqual(saved['confirmations'][0]['event']['event_id'], first['event_id'])

    def test_after_native_ai_transition_old_click_survives_original_event_retention(self):
        before = self.check()
        self.verify.reset_mock()
        self.receipt.reset_mock()
        self.p.add_change('fldMarker', '', '研发项开始执行', '研发助手')
        self.p.add_change('status', '可开始', '进行中', '多维表格助手')
        self.p.row['status'] = ['进行中']
        after = self.check()
        self.assertEqual(before['confirmation_sha256'], after['confirmation_sha256'])
        self.verify.assert_not_called()
        self.receipt.assert_not_called()

    def hand_over(self, *, seal_first=True):
        import stage_source
        p = self.p
        old_owner = {'id': 'ou_previous', 'name': '原负责人'}
        new_owner = {'id': p.bot, 'name': '研发助手'}
        p.row['owner'] = [old_owner]
        first = self.check() if seal_first else None
        p.row['owner'] = [new_owner]
        p.add_change('owner', old_owner['name'], new_owner['name'], new_owner['name'])
        change = p.rows[0]
        change['field_changes'][0]['field_type'] = 'User'
        proof = p.proof()
        material = dict(method='authorized_owner_handoff', project_record_id=p.project, table_id=p.table,
                        record_id='recRow', owner_field_id=p.fields['owner']['id'],
                        from_open_id=old_owner['id'], from_name=old_owner['name'],
                        to_open_id=new_owner['id'], to_name=new_owner['name'],
                        plan_confirmer_open_id=proof['operator_open_id'], plan_revision=proof['business_revision'],
                        confirmation_table_revision=proof['table_revision'], confirmation_update_time=proof['update_time'],
                        history=copy.deepcopy(change), authorization_sha256='a' * 64, report_sha256='b' * 64)
        for fn, kw in [('location', {'return_value': p.directory}), ('query', {'return_value': material})]:
            patched = mock.patch.object(stage_source, fn, **kw)
            self.addCleanup(patched.stop)
            patched.start()
        return first, material

    def test_authorized_owner_handoff_preserves_seal_and_subsequent_native_steps(self):
        first, _ = self.hand_over()
        for stage in ('可开始', '进行中', '已交付'):
            if stage != '可开始':
                self.p.add_change('status', self.p.row['status'][0], stage, '多维表格助手')
                self.p.row['status'] = [stage]
            self.assertEqual(first['confirmation_sha256'], self.check()['confirmation_sha256'])
        book = json.loads(next((self.p.directory / 'stage-confirmations').glob('*.json')).read_text())
        self.assertEqual(len(book['confirmations']), 1)
        self.assertEqual(book['confirmations'][0]['fields'][self.p.fields['owner']['id']], ['ou_previous'])

    def test_owner_handoff_before_first_seal_records_original_owner(self):
        self.hand_over(seal_first=False)
        first = self.check()
        self.assertEqual(first['status'], 'verified')
        self.assertEqual(first['confirmation_sha256'], self.check()['confirmation_sha256'])
        book = json.loads(next((self.p.directory / 'stage-confirmations').glob('*.json')).read_text())
        self.assertEqual(book['confirmations'][0]['fields'][self.p.fields['owner']['id']], ['ou_previous'])

    def test_actual_archived_click_can_be_rechecked_after_handoff(self):
        import stage_source
        _, transfer = self.hand_over(seal_first=False)
        original = dict(method='native_event', business_revision=self.p.row['plan_revision'],
                        event=self.p.event(), report_sha256='a' * 64)
        materials = {'owner_handoff': transfer, 'original_click': original}
        self.verify.side_effect = None
        self.verify.return_value = {'status': 'pending'}
        with mock.patch.object(stage_source, 'query', side_effect=lambda p, kind, *args: materials[kind]), \
                mock.patch.object(business.confirmation, 'confirm_directory_user'):
            self.assertEqual(self.check().get('event_id'), self.p.event()['event_id'])
            self.p.row['owner'] = [{'id': 'ou_other', 'name': '研发助手'}]
            self.assertEqual(self.check()['status'], 'pending')

    def test_missing_handoff_evidence_or_a_change_after_start_cannot_be_used(self):
        import stage_source
        _, transfer = self.hand_over()
        with mock.patch.object(stage_source, 'query', side_effect=stage_action.Pending('缺少授权记录')):
            self.assertEqual(self.check()['status'], 'pending')
        self.p.rows.pop(0)
        self.p.add_change('status', '可开始', '进行中', '多维表格助手')
        self.p.add_change('owner', '原负责人', '研发助手', '研发助手')
        self.p.rows[0]['field_changes'][0]['field_type'] = 'User'
        transfer['history'] = copy.deepcopy(self.p.rows[0])
        self.p.row['status'] = ['进行中']
        self.assertEqual(self.check()['status'], 'pending')

    def test_handoff_cannot_reassign_other_people_plans_or_records(self):
        _, evidence = self.hand_over()
        original = copy.deepcopy(evidence)
        for key, v in dict(from_open_id='ou_wrong', to_open_id='ou_other', record_id='recOther',
                           table_id='tblOther', project_record_id='recOther', owner_field_id='fldOther',
                           confirmation_table_revision=4, confirmation_update_time=5, plan_revision=True,
                           plan_confirmer_open_id='ou_other', from_name='另一人', history={}).items():
            with self.subTest(key=key):
                evidence[key] = v
                self.assertEqual(self.check()['status'], 'pending')
                evidence[key] = original[key]
        self.p.row['owner'].append({'id': 'ou_other', 'name': '另一人'})
        self.assertEqual(self.check()['status'], 'pending')
        self.p.row['owner'].pop()
        self.p.add_change('owner', '研发助手', '原负责人', '研发助手')
        self.p.add_change('owner', '原负责人', '研发助手', '研发助手')
        self.assertEqual(self.check()['status'], 'pending')

    def test_handoff_does_not_excuse_other_edits_or_changed_confirmer(self):
        _, material = self.hand_over()
        clean = copy.deepcopy(self.p.rows)
        for key in ('plan', 'dependencies', 'plan_confirmer'):
            self.p.rows = copy.deepcopy(clean)
            self.p.add_change(key, '改前', '改后', '研发助手')
            self.assertEqual(self.check()['status'], 'pending')
        self.p.rows = copy.deepcopy(clean)
        material['history']['field_changes'].append({'field_id': 'fldUnknown'})
        self.p.rows[0] = copy.deepcopy(material['history'])
        self.assertEqual(self.check()['status'], 'pending')
        self.p.rows = copy.deepcopy(clean)
        material['history'] = copy.deepcopy(clean[0])
        self.p.row['plan_confirmer'][0]['id'] = 'ou_other'
        self.assertEqual(self.check()['status'], 'pending')

    def test_material_field_owner_project_links_or_unknown_edits_invalidate_even_after_revert(self):
        for key in ('plan', 'plan_confirmer', 'owner', 'project', 'requirements', 'dependencies', 'fldUnknown'):
            with self.subTest(key=key):
                self.p.rows = self.p.rows[-1:]
                self.assertEqual(self.check()['status'], 'verified')
                self.p.add_change(key, 'before', 'other', '另一人')
                self.p.add_change(key, 'other', 'before', '另一人')
                self.assertEqual(self.check()['status'], 'pending')

    def test_new_authentic_same_revision_click_can_confirm_restored_identical_contents(self):
        old = self.check()
        self.p.add_change('plan', '原计划', '其他计划', '另一人')
        self.p.add_change('plan', '其他计划', '原计划', '另一人')
        self.p.add_change('status', '可开始', '待确认计划', '负责的同事')
        self.p.add_change('status', '待确认计划', '可开始', '负责的同事')
        renewed = self.check()
        self.assertEqual(renewed['status'], 'verified')
        self.assertNotEqual(old['event_id'], renewed['event_id'])

    def test_changed_content_requires_new_revision_and_new_click(self):
        old = self.check()
        self.p.row['plan'] = '改为每周发送一次消息。'
        self.p.add_change('plan', '原计划', self.p.row['plan'], '另一人')
        self.assertEqual(self.check()['status'], 'pending')
        self.p.row['plan_revision'] += 1
        self.p.add_change('plan_revision', 2, 3, '另一人')
        self.assertEqual(self.check()['status'], 'pending')
        self.p.add_change('status', '可开始', '待确认计划', '负责的同事')
        self.p.add_change('status', '待确认计划', '可开始', '负责的同事')
        current = self.check()
        self.assertNotEqual(old['event_id'], current['event_id'])
        self.assertEqual(current['business_revision'], 3)

    def test_unknown_actor_mixed_project_missing_history_or_changed_anchor_fails_closed(self):
        self.assertEqual(self.check()['status'], 'verified')
        anchor = copy.deepcopy(self.p.rows)
        self.p.add_change('status', '可开始', '进行中', '另一人')
        self.p.row['status'] = ['进行中']
        self.assertEqual(self.check()['status'], 'pending')
        self.p.row['project'] += [{'id': 'recOther'}]
        self.assertEqual(self.check()['status'], 'pending')
        self.p.row['project'].pop()
        self.p.rows = []
        self.assertEqual(self.check()['status'], 'pending')
        self.p.rows = anchor
        self.p.rows[0]['create_time'] += 1
        self.assertEqual(self.check()['status'], 'pending')

    def test_no_freezing_from_displayed_status_paused_service_or_unrelated_raw_event(self):
        self.verify.side_effect = None
        self.verify.return_value = {'status': 'pending'}
        self.assertEqual(self.check()['status'], 'pending')
        self.verify.return_value = self.p.proof()
        self.receipt.side_effect = lambda *_: dict(self.p.event(), operator_open_id='ou_unrelated')
        self.assertEqual(self.check()['status'], 'pending')
        self.receipt.side_effect = lambda *_: self.p.event()
        (self.p.directory / 'PAUSED').write_text('暂停')
        self.assertEqual(self.check()['status'], 'pending')
        self.assertFalse(list((self.p.directory / 'stage-confirmations').glob('*.json')))

    def test_official_card_match_keeps_the_original_event_absent(self):
        import stage_source
        p = self.p
        material = dict(method='official_history_profile', original_event_captured=False,
                        business_revision=2, operator_open_id='ou_colleague', history=p.rows[0],
                        history_profile_sha256='a' * 64, confirmer_profile_sha256='a' * 64,
                        report_sha256='b' * 64)
        with mock.patch.object(stage_source, 'query', return_value=material), \
                mock.patch.object(business.confirmation, 'confirm_directory_user'):
            observed = business.archived_click(p, p.role, 'recRow', p.row, {})
            self.assertIsNone(observed['proof']['event_id'])
            self.assertFalse(observed['proof']['original_event_captured'])
            material['confirmer_profile_sha256'] = 'c' * 64
            with self.assertRaisesRegex(stage_action.Pending, '名片'):
                business.archived_click(p, p.role, 'recRow', p.row, {})

    def test_current_processing_can_pause_work_without_erasing_scope(self):
        self.assertEqual(self.check()['status'], 'verified')
        self.p.row['processing'] = ['暂停']
        self.assertEqual(self.check()['status'], 'pending')

    def test_single_project_plan_links_outbound_and_test_records_are_not_human_approval(self):
        self.set_project('requirements')
        original = self.check()
        self.p.add_change('work', '', '关联研发项', '研发助手')
        self.p.row['work'] = [{'id': 'recWork'}]
        updated = self.check()
        self.assertEqual(original['confirmation_sha256'], updated['confirmation_sha256'])
        self.assertFalse(updated['linked_work_checked'])
        self.assertFalse(updated['delivery_evidence_checked'])


if __name__ == '__main__':
    unittest.main()
