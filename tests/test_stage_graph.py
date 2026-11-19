import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import stage_action
import stage_business as business


def links(*values):
    return [{'id': value} for value in values]


class Graph(business.Business):
    """Only the service boundaries are replaced; the real graph and artifact rules run."""
    def __init__(self, key):
        self.key = key
        self.p = mock.Mock(project='recProject', bot='ou_bot', path='service')
        self.p.binding = {'project_number': 'P1'}
        self.approvals, self.proofs = {}, {}
        self.data, self.inputs = {}, {}
        self.previous_ids = set()
        self.data['projects', 'recProject'] = {'number': 'P1', 'status': ['进行中']}
        for role, rid in [('requirements', 'recReq'), ('bugs', 'recBug')]:
            self.data[role, rid] = dict(processing=['正常'], status=['研发中'], project=links('recProject'),
                nature=['当前工作'], feedback=links('recFeedback'), work=links('recW1', 'recW2'),
                baseline='原提交', candidate_build='已修构建', acceptor=links('ou_human'),
                verification=links('recQA', 'recHuman'), retest_tests=links('recQA', 'recHuman'),
                reproduction_tests=links('recRepro'))
        self.data['feedback', 'recFeedback'] = dict(status=['已转办'], requirements=links('recReq'), bugs=links('recBug'),
                                                    work=links('recW1', 'recW2'))
        for work in ('recW1', 'recW2'):
            self.data['work', work] = dict(processing=['正常'], status=['已交付'], owner=links('ou_bot'),
                feedback=links('recFeedback'), requirements=links('recReq'), bugs=links('recBug'), dependencies=[],
                summary='已补充日期说明，并检查满房时的页面。')
            self.previous_ids.update({(work, 'work-start'), (work, 'work-deliver')})
        self.repro_target = self.target_data('原提交', {'reproduction': ['recRepro']}, build='v1', component='0')
        self.test_data('recRepro', 'Bug复现', '失败', self.repro_target, [], {'recBug': 1})
        for w, test in (('recW1', 'recSelf1'), ('recW2', 'recSelf2')):
            target = self.target_data(None, {'self': [test]})
            self.inputs['target', 'work', w] = target
            self.test_data(test, '研发自测', '通过', target, [w], {w: 1})
        for role, rid, kind in (('requirements', 'recReq', '需求验收'), ('bugs', 'recBug', 'Bug复测')):
            t = self.target_data('已修构建' if role == 'bugs' else None,
                                 {'manual': ['recHuman'], 'independent': ['recQA']})
            self.inputs['target', role, rid] = t
        if key in ('bug-start', 'work-start'):
            self.inputs['target', 'bugs', 'recBug'] = self.repro_target
        role = 'bugs' if key.startswith('bug') else 'requirements'
        parent = 'recBug' if role == 'bugs' else 'recReq'
        chosen = self.inputs['target', role, parent]
        self.test_data('recQA', 'Bug复测' if role == 'bugs' else '需求验收', '通过', chosen,
                       ['recW1', 'recW2'], {parent: 1, 'recW1': 1, 'recW2': 1}, independent=True)
        self.data['verification', 'recHuman'] = dict(key='human', purpose=['Bug复测' if role == 'bugs' else '需求验收'],
            method=['人工核验'], result=['未执行'], environment=[chosen['environment']], build=chosen['build'],
            criteria_snapshot='打开页面可看到缺少原因的日期。', work=links('recW1', 'recW2'),
            requirements=links('recReq'), retested_bugs=links('recBug'))
        self.inputs['acceptance_plan', 'verification', 'recHuman'] = dict(key='human', subject_id=parent,
            revision=1, acceptor='ou_human', components=chosen['components'],
            criteria_sha256=business.digest(self.data['verification', 'recHuman']['criteria_snapshot']))

    def target_data(self, reference, tests, *, build='v2', component='1'):
        return dict(revision=1, build=build, components={'app': component * 64}, environment='测试',
                    reference=reference, tests=tests, delivery_mode='artifact', verified_artifact=True,
                    require_independent=True)

    def test_data(self, rid, purpose, result, target, work, subjects, *, independent=False):
        label = '独立复测' if independent else '执行人'
        row = dict(key=rid, nature=['当前工作'], result=[result], purpose=[purpose], method=['自动化测试'],
            build=target['build'], environment=[target['environment']], summary='运行了已登记的用例。', executor=label,
            evidence='保存在项目执行记录中。', criteria_snapshot='需要通过已确认的检查。',
            work=links(*work), requirements=links('recReq'), retested_bugs=links('recBug'), reproduced_bugs=links('recBug'))
        self.data['verification', rid] = row
        self.inputs['test', 'verification', rid] = dict(key=rid, purpose=purpose, result=result, method='自动化测试',
            build=target['build'], components=target['components'], environment=target['environment'], executor=label,
            executor_id='actor-qa' if independent else 'actor-dev', session_id='qa' if independent else 'dev',
            started_ms=300 if independent else 100, ended_ms=400 if independent else 200,
            criteria_sha256=business.digest(row['criteria_snapshot']), evidence_sha256=business.digest(row['evidence']),
            report_sha256='f' * 64, subject_revisions=subjects)

    def row(self, role, record):
        return self.data[role, record]

    def links(self, role, record, field, target):
        return business.record_ids(self.row(role, record), field)

    def approved(self, role, record):
        self.approvals[record] = (role, 1, 'f' * 64)
        return 1

    def previous(self, work, key):
        stage_action.require((work, key) in self.previous_ids, '缺少真实历史。')

    def source(self, kind, role, record):
        return self.inputs[kind, role, record]

    def timestamp(self, test, row):
        return 400 if test == 'recQA' else 200

    def apply(self):
        self.project_open()
        role = business.rules.STAGE_REQUESTS[self.key][0]
        if role == 'work':
            self.work('recW1')
        else:
            self.parent(role, 'recReq' if role == 'requirements' else 'recBug')


class GraphTests(unittest.TestCase):
    def setUp(self):
        user = mock.patch.object(business.confirmation, 'confirm_directory_user')
        self.addCleanup(user.stop)
        user.start()

    def test_all_six_business_paths_with_shared_parents_and_two_work_items(self):
        for key in business.rules.STAGE_REQUESTS:
            with self.subTest(key=key):
                Graph(key).apply()

    def test_start_requires_one_real_started_child_but_all_must_finish_for_acceptance(self):
        g = Graph('req-start')
        g.data['work', 'recW1']['status'] = ['待确认计划']
        g.apply()
        g.data['work', 'recW2']['status'] = ['可开始']
        with self.assertRaises(stage_action.Pending):
            g.apply()
        g = Graph('req-accept')
        g.data['work', 'recW2']['status'] = ['进行中']
        with self.assertRaises(stage_action.Pending):
            g.apply()

    def test_unrelated_child_standalone_or_open_dependency_does_not_start_work(self):
        for fail in ('owner', 'reverse', 'feedback', 'dependency', 'standalone'):
            with self.subTest(fail=fail):
                g = Graph('work-start')
                w = g.data['work', 'recW1']
                if fail == 'owner': w['owner'] = links('ou_other')
                if fail == 'reverse': g.data['bugs', 'recBug']['work'] = links('recW2')
                if fail == 'feedback': g.data['feedback', 'recFeedback']['work'] = links('recW2')
                if fail == 'dependency':
                    w['dependencies'] = links('recW2')
                    g.data['work', 'recW2']['status'] = ['进行中']
                if fail == 'standalone': w.update(bugs=[], requirements=[])
                with self.assertRaises(stage_action.Pending):
                    g.apply()

    def test_reproduction_must_be_original_failed_observation_on_the_bound_build(self):
        for setting in ('result', 'reference', 'revision', 'scope', 'build'):
            g = Graph('bug-start')
            if setting == 'result': g.data['verification', 'recRepro']['result'] = ['通过']
            if setting == 'reference': g.repro_target['reference'] = '其他基线'
            if setting == 'revision': g.inputs['test', 'verification', 'recRepro']['subject_revisions']['recBug'] = 2
            if setting == 'scope': g.data['verification', 'recRepro']['reproduced_bugs'] = []
            if setting == 'build': g.data['verification', 'recRepro']['build'] = '最新'
            with self.subTest(setting=setting), self.assertRaises(stage_action.Pending):
                g.apply()

    def test_independence_version_environment_actual_time_or_manual_recipient_mismatches_block(self):
        for setting in ('developer', 'session', 'order', 'environment', 'real_time', 'test_revision',
                        'candidate', 'target', 'recipient', 'stale_manual', 'uncovered'):
            g = Graph('bug-accept')
            p = g.inputs['test', 'verification', 'recQA']
            if setting == 'developer': p['executor_id'] = 'actor-dev'
            if setting == 'session': p['session_id'] = 'dev'
            if setting == 'order': p['started_ms'] = 120
            if setting == 'environment': p['environment'] = '本地'
            if setting == 'real_time': p['ended_ms'] = 401
            if setting == 'test_revision': p['subject_revisions']['recW1'] = 0
            if setting == 'candidate': g.data['bugs', 'recBug']['candidate_build'] = '另一版'
            if setting == 'target': g.inputs['target', 'bugs', 'recBug']['verified_artifact'] = False
            if setting == 'recipient': g.inputs['acceptance_plan', 'verification', 'recHuman']['acceptor'] = 'ou_other'
            if setting == 'stale_manual': g.data['verification', 'recHuman']['executor'] = '已执行'
            if setting == 'uncovered': g.data['verification', 'recHuman']['work'] = links('recW1')
            with self.subTest(setting=setting), self.assertRaises(stage_action.Pending):
                g.apply()

    def test_unmatched_scope_missing_artifact_and_pending_deployment_are_distinct(self):
        g = Graph('req-accept')
        t = g.inputs['target', 'requirements', 'recReq']
        t.update(delivery_mode='deployment', deployment={'expected': {}})
        with self.assertRaisesRegex(stage_action.Pending, '部署版本'):
            g.apply()
        g = Graph('work-deliver')
        g.data['work', 'recW1']['summary'] = ''
        with self.assertRaisesRegex(stage_action.Pending, '交付说明'):
            g.apply()

    def test_minute_precision_requires_explicit_writer_encoding_and_correct_minute(self):
        g = Graph('work-deliver')
        p = g.inputs['test', 'verification', 'recSelf1']
        p['ended_ms'] = 123456
        g.timestamp = lambda *_: 120000
        with self.assertRaisesRegex(stage_action.Pending, '执行时间'):
            g.apply()
        p['feishu_time_precision'] = 'minute'
        g.apply()
        g.timestamp = lambda *_: 60000
        with self.assertRaisesRegex(stage_action.Pending, '执行时间'):
            g.apply()

    def test_one_observed_failure_cannot_substitute_for_a_self_test(self):
        g = Graph('bug-start')
        p = g.inputs['test', 'verification', 'recRepro']
        p.update(observation='reproduced_failure', verified_input=True, expected=['a'], observed=['b'], observed_ms=200)
        for k in ('executor_id', 'session_id', 'started_ms', 'ended_ms'):
            del p[k]
        g.apply()
        p['observed'] = p['expected']
        with self.assertRaisesRegex(stage_action.Pending, '原构建复现'):
            g.apply()

    def test_cli_does_not_accept_an_evidence_or_command_parameter(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            stage_action.main(['--config','/tmp/test', '--action','req-start', '--record-id','recX', '--allowed'])


if __name__ == '__main__':
    unittest.main()
