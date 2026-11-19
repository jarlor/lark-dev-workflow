import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import prepare_plan as plan


DRAFT = dict(plan='打开条目后显示计划。', scope='只处理条目详情页。', deliverable='一条可查看的计划。',
             verification_plan='打开条目，核对显示的计划。')


class Project:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'bridge.json'
        self.token, self.project, self.signature, self.open_id = 'baseTest', 'recP', 'binding', 'ou_bot'
        self.tables = {'requirements': {'id':'tblR', 'fields':{'work':{'id':'fldWork'}}}}
        self.live = {'checked': True}
        self.req = {'title':'打开条目看计划', 'confirmer':[{'id':'ou_reporter'}], 'revision':1,
                    'scope':'查看', 'criteria':'可以看', 'work':[], 'feedback':[{'id':'recF'}]}
        self.fb = {'work': []}
        self.entries = [dict(rev=19, activity_type='update', field_changes=[{'field_id':'fldStatus'}])]
        self.reader = mock.Mock()
        self.proof = dict(status='verified', record_id='recR', table_id='tblR', project_record_id='recP',
                          business_revision=1, operator_open_id='ou_reporter', event_id='evt19')
        self.verify = mock.Mock(side_effect=lambda *args: dict(self.proof))
        self.created = 0
        self.work = None
        self.default = ['待确认计划']
        self.drop_reply = False
        self.cancelled = False

    def unchanged(self):
        if self.cancelled:
            raise plan.Conflict('项目已暂停。')

    def source(self, record, revision):
        return copy.deepcopy(self.req)

    def row(self, role, record):
        assert (role, record) in (('work','recW'), ('feedback','recF'))
        return copy.deepcopy(self.work if role == 'work' else self.fb)

    def find_key(self, key):
        return 'recW' if self.work and self.work['key'] == key else None

    def native_fields(self, values):
        return values

    def create(self, values, token):
        assert uuid.UUID(token).version == 4
        self.created += 1
        self.work = dict(copy.deepcopy(values), status=self.default)
        self.req['work'] = [{'id':'recW'}]
        self.fb['work'] = [{'id':'recW'}]
        self.entries.insert(0, dict(rev=20, activity_type='update', field_changes=[{'field_id':'fldWork'}]))
        if self.drop_reply:
            raise TimeoutError()
        return 'recW'


class PreparePlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Project(temp.name)
        for name, args in [('history', {'side_effect':lambda *a:copy.deepcopy(self.project.entries)}),
                           ('confirm_directory_user', {})]:
            patch = mock.patch.object(plan.confirmation, name, **args)
            patch.start()
            self.addCleanup(patch.stop)

    def run_plan(self, **kwargs):
        return plan.run(self.project, 'recR', 1, 'ou_manager', kwargs.get('draft', DRAFT), self.project.verify)

    def check_plan(self):
        return plan.get_prepared(self.project.path, 'recR', project=self.project)

    def test_prepare_and_notify_readback_use_one_existing_draft_without_modification(self):
        out = self.run_plan()
        self.assertEqual((out['status'],out['record_id'],out['plan_revision']), ('prepared','recW',1))
        intent = json.loads(next(self.project.directory.glob('*.json')).read_text())
        self.assertIs(intent['verified_readback'], True)
        self.assertNotIn('status', intent['values'])
        self.assertEqual(self.project.work['scope'], DRAFT['scope'])
        self.assertEqual(self.project.work['plan_confirmer'], [{'id':'ou_manager'}])
        self.assertEqual(self.project.work['feedback'], [{'id':'recF'}])
        self.assertEqual(self.check_plan()['values']['plan'], DRAFT['plan'])
        self.project.proof = {'status':'pending', 'reason':'后来多了反向关联'}
        self.assertEqual(self.run_plan(draft=dict(DRAFT, plan='另一种提议。'))['status'], 'prepared')
        self.assertEqual(self.project.created, 1)
        self.assertEqual(self.project.work['plan'], DRAFT['plan'])

    def test_pending_does_not_enumerate_project_or_persist_an_intent(self):
        self.project.proof = {'status':'pending', 'reason':'尚待反馈人确认'}
        self.project.find_key = mock.Mock(side_effect=AssertionError('scope pending'))
        self.assertEqual(self.run_plan()['status'], 'pending')
        self.assertEqual(self.project.created, 0)
        self.assertFalse(list(self.project.directory.glob('*.json')))

    def test_wrong_revision_actor_or_duplicate_source_never_creates(self):
        for key, value in [('business_revision',2), ('operator_open_id','ou_other'), ('project_record_id','recOther')]:
            old = self.project.proof[key]
            self.project.proof[key] = value
            with self.subTest(key=key), self.assertRaises(plan.Conflict):
                self.run_plan()
            self.project.proof[key] = old
        self.project.req['work'] = [{'id':'recOther'}]
        self.assertEqual(self.run_plan()['status'], 'conflict')
        self.assertEqual(self.project.created, 0)

    def test_timeout_after_write_is_recovered_without_posting_again(self):
        self.project.drop_reply = True
        with self.assertRaises(TimeoutError):
            self.run_plan()
        path = next(self.project.directory.glob('*.json'))
        original = path.read_bytes()
        self.assertNotIn('record_id', json.loads(original))
        self.assertEqual(self.check_plan()['status'], 'prepared')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.run_plan()['status'], 'prepared')
        self.assertEqual(self.project.created, 1)

    def test_uncertain_post_without_any_row_is_not_replayed(self):
        create = mock.Mock(side_effect=TimeoutError())
        self.project.create = create
        with self.assertRaises(TimeoutError):
            self.run_plan()
        self.assertEqual(self.check_plan()['status'], 'pending')
        self.assertEqual(self.run_plan()['status'], 'pending')
        self.assertEqual(create.call_count, 1)

    def test_unsent_intent_reuses_its_plan_only_after_fresh_confirmation(self):
        self.project.cancelled = True
        with self.assertRaises(plan.Conflict):
            self.run_plan()
        self.assertEqual(self.project.created, 0)
        self.project.cancelled = False
        prior = self.project.proof
        self.project.proof = {'status':'pending','reason':'尚未核实'}
        self.assertEqual(self.run_plan()['status'], 'pending')
        self.assertEqual(self.project.created, 0)
        self.project.proof = prior
        self.assertEqual(self.run_plan()['status'], 'prepared')
        self.assertEqual(self.project.created, 1)

    def test_node_readback_human_edits_and_changed_source_are_not_overwritten(self):
        self.project.default = []
        self.assertEqual(self.run_plan()['status'], 'pending')
        self.project.work['status'] = ['待确认计划']
        self.assertEqual(self.run_plan()['status'], 'prepared')
        self.project.work['plan'] = '人的新方案'
        self.assertEqual(self.check_plan()['status'], 'conflict')
        self.assertEqual(self.run_plan()['status'], 'conflict')
        self.project.work['plan'] = DRAFT['plan']
        self.project.entries.insert(0, dict(rev=21, activity_type='update', field_changes=[{'field_id':'fldScope'}]))
        self.assertEqual(self.check_plan()['status'], 'conflict')
        self.assertEqual(self.project.created, 1)

    def test_native_post_contains_only_one_row_ordinary_fields_with_exact_wire_types(self):
        values = dict(DRAFT, project=[{'id':'recP'}], requirements=[{'id':'recR'}], feedback=[{'id':'recF'}],
                      plan_confirmer=[{'id':'ou_manager'}], title='条目计划', nature=['当前工作'], key='test-key', plan_revision=1)
        test = plan.Project.__new__(plan.Project)
        test.token = 'baseTest'
        fields = {k:{'id':'fld'+k, 'name':k} for k in values}
        test.tables = {'work': {'id':'tblWork','fields':fields}}
        test.live = {'work': {v['id']:v for v in fields.values()}}
        test.reader = mock.Mock(profile='bound')
        test.reader.base._envelope.return_value = {'record':{'record_id':'recW'}}
        test.reader.runner.return_value = subprocess.CompletedProcess([], 0, '{"identity":"bot"}', '')
        token = str(uuid.uuid4())
        test.binding = {}
        with mock.patch.object(plan.watch, '_fields', return_value=test.live['work']):
            body = test.native_fields(values)
        self.assertEqual(test.create(body,token), 'recW')
        args, kw = test.reader.runner.call_args
        argv = args[0]
        self.assertEqual(argv[2:8], ['POST','/open-apis/bitable/v1/apps/baseTest/tables/tblWork/records',
                                      '--as','bot','--profile','bound'])
        self.assertEqual(json.loads(argv[argv.index('--params')+1]), {'user_id_type':'open_id','client_token':token})
        data = json.loads(kw['input'])['fields']
        self.assertEqual(data, dict(values, project=['recP'],requirements=['recR'],feedback=['recF'],nature='当前工作'))

    def test_only_four_plan_strings_accepted(self):
        for value in [dict(DRAFT, status='可开始'), dict(DRAFT, scope=''), dict(DRAFT, plan={'step':'run'}),
                      dict(DRAFT, deliverable='https://example.test/file')]:
            with self.subTest(value=value), self.assertRaises(plan.Conflict):
                plan.checked_draft(value)
