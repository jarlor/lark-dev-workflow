import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_stage_graph import Graph
import isolated_retest
from prepare_plan import digest
from stage_action import Pending
import stage_business as business
import stage_source


class IsolatedRetestTests(unittest.TestCase):
    def setUp(self):
        deployment = mock.patch('test_deployment.check', return_value={'ok': True})
        self.addCleanup(deployment.stop)
        self.deploy = deployment.start()
        directory = mock.patch.object(business.confirmation, 'confirm_directory_user')
        self.addCleanup(directory.stop)
        directory.start()
        self.graph = g = Graph('req-accept')
        self.parent = t = g.inputs['target', 'requirements', 'recReq']
        self.local = {'app': '1' * 64}
        t['components'] = {**self.local, 'backend': '2' * 40}
        t.update(delivery_mode='deployment', deployment={
            'expected': {'repositories': {k: {'sha': v} for k, v in t['components'].items()}},
            'environment_label': '测试', 'before': {}, 'after': {}})
        g.inputs['acceptance_plan', 'verification', 'recHuman']['components'] = t['components'].copy()
        plans = {}
        for w in ('recW1', 'recW2'):
            g.data['work', w]['plan'] = '固定版本复测 ' + w
            plans[w] = digest(g.data['work', w]['plan'])
        self.rule = dict(build='隔离候选', components=self.local.copy(), plan_sha256=plans, authorization_sha256='a' * 64)
        t['isolated_independent'] = {'recQA': self.rule}
        self.test_proof = p = g.inputs['test', 'verification', 'recQA']
        g.data['verification', 'recQA'].update(build=self.rule['build'], environment=['本地'])
        self.asset = 'b' * 64
        self.comparison = 'c' * 64
        self.observed = dict(deployment_build=t['build'], deployment_components=t['components'].copy(),
                             local_assets={'index.js': self.asset}, deployed_assets={'index.js': self.asset},
                             comparison_sha256=self.comparison)
        p.update(build=self.rule['build'], environment='本地', components=self.local.copy(),
                 isolated_execution=self.observed)

    def test_separate_deployment_local_test_and_human_plan(self):
        self.graph.apply()
        self.deploy.assert_called_once()
        self.assertIn(('isolated_execution', 'recQA'), self.graph.proofs)
        self.assertEqual(self.graph.data['verification', 'recHuman']['result'], ['未执行'])

    def test_nothing_implies_permission_to_change_test_environment(self):
        del self.parent['isolated_independent']
        with self.assertRaisesRegex(Pending, '执行环境'):
            self.graph.apply()

    def test_separately_listed_integration_still_runs_on_shared_target(self):
        g = self.graph
        self.parent['tests']['integration'] = ['recIntegration']
        g.data['requirements', 'recReq']['verification'].append({'id': 'recIntegration'})
        local = dict(self.parent, build=self.rule['build'], environment='本地', components=self.local)
        g.test_data('recIntegration', '交付验证', '通过', local, ['recW1'], {'recReq': 1, 'recW1': 1})
        with self.assertRaisesRegex(Pending, '环境或构建'):
            g.apply()

    def test_unlisted_isolated_report_cannot_be_relabeled_as_testing_server(self):
        del self.parent['isolated_independent']
        self.test_proof.update(build=self.parent['build'], components=self.parent['components'], environment='测试')
        self.graph.data['verification', 'recQA'].update(build=self.parent['build'], environment=['测试'])
        with self.assertRaisesRegex(Pending, '执行环境'):
            self.graph.apply()

    def test_only_signed_plan_and_exact_confirmed_revisions(self):
        self.graph.data['work', 'recW2']['plan'] += ' 新验收条件'
        with self.assertRaisesRegex(Pending, '计划与原授权'):
            self.graph.apply()
        self.rule['plan_sha256']['recW2'] = digest(self.graph.data['work', 'recW2']['plan'])
        for wrong in (True, 2, None):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(Pending, '计划与原授权'):
                self.test_proof['subject_revisions']['recW2'] = wrong
                self.graph.apply()

    def test_partial_or_mixed_deployment_and_unchecked_component_rejected(self):
        for proof, key, wrong in ((self.observed, 'deployment_build', '旧测试服'),
                                  (self.observed, 'deployment_components', self.local.copy()),
                                  (self.rule, 'components', self.parent['components'].copy()),
                                  (self.observed, 'deployed_assets', {'index.js': '9' * 64}),
                                  (self.test_proof, 'components', {'app': '9' * 64})):
            with self.subTest(key=key), mock.patch.dict(proof, {key: wrong}), self.assertRaises(Pending):
                self.graph.apply()
        self.deploy.return_value = {'ok': False}
        with self.assertRaisesRegex(Pending, '部署版本'):
            self.graph.apply()

    def test_cannot_replace_test_server_integration_or_manual_plan(self):
        self.parent['tests']['integration'] = ['recQA']
        with self.assertRaisesRegex(Pending, '独立检查'):
            self.graph.apply()
        del self.parent['tests']['integration']
        self.graph.data['verification', 'recHuman']['environment'] = ['本地']
        with self.assertRaisesRegex(Pending, '人工验收记录'):
            self.graph.apply()

    def test_original_timing_and_independence_still_checked(self):
        for key, wrong in [('session_id', 'dev'), ('executor_id', 'actor-dev'), ('started_ms', 199)]:
            with self.subTest(key=key), mock.patch.dict(self.test_proof, {key: wrong}), self.assertRaisesRegex(Pending, '自测后'):
                self.graph.apply()

    def test_never_extends_to_bug_retest_or_artifact_delivery(self):
        for key, role in [('bug-accept', 'bugs'), ('req-start', 'requirements'), ('work-deliver', 'work')]:
            with self.subTest(key=key), self.assertRaises(Pending):
                isolated_retest.validate_target(self.parent, key, role, True)
        self.parent['delivery_mode'] = 'artifact'
        with self.assertRaisesRegex(Pending, '核实测试服部署'):
            self.graph.apply()

    def test_required_report_and_two_independently_read_files_per_asset(self):
        checked = {'local/index.js': self.asset, 'deployed/index.js': self.asset, 'comparison': self.comparison}
        ids = {k: (1, i) for i, k in enumerate(checked)}
        isolated_retest.check_sources('test', 'verification', 'req-accept', self.test_proof, checked, ids)
        for key in checked:
            with self.subTest(missing=key), self.assertRaisesRegex(Pending, '实际文件'):
                isolated_retest.check_sources('test', 'verification', 'req-accept', self.test_proof,
                                              {k: v for k, v in checked.items() if k != key}, ids)
        allowed = {'scope-and-plan': self.rule['authorization_sha256']}
        isolated_retest.check_sources('target', 'requirements', 'req-accept', self.parent, allowed, {})
        with self.assertRaisesRegex(Pending, '授权文件'):
            isolated_retest.check_sources('target', 'requirements', 'req-accept', self.parent, checked, ids)
        with self.assertRaisesRegex(Pending, '授权文件'):
            isolated_retest.check_sources('target', 'bugs', 'bug-accept', self.parent, allowed, {})

    def test_two_local_files_and_aliases_to_one_original_cannot_prove_a_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            local = root / 'local' / 'index.js'
            remote = root / 'deployed' / 'index.js'
            report = root / 'report'
            for path in (local, remote):
                path.parent.mkdir()
                path.write_bytes(b'built content')
            sha = hashlib.sha256(local.read_bytes()).hexdigest()
            report.write_bytes(b'comparison')
            self.observed.update(local_assets={'index.js': sha}, deployed_assets={'index.js': sha},
                                 comparison_sha256=hashlib.sha256(report.read_bytes()).hexdigest())

            def read_pair(second):
                ids = {}
                sources = [dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                           for path in (local, second, report)]
                checked = stage_source.response_sources(sources, [root], identities=ids)
                isolated_retest.check_sources('test', 'verification', 'req-accept', self.test_proof, checked, ids)

            read_pair(remote)
            remote.unlink()
            os.link(local, remote)
            with self.assertRaisesRegex(Pending, '同名实际文件'):
                read_pair(remote)
            unrelated = root / 'backup.js'
            unrelated.write_bytes(local.read_bytes())
            with self.assertRaisesRegex(Pending, '同名实际文件'):
                read_pair(unrelated)

    def test_conflicting_versions_cannot_be_overwritten_by_a_later_self_test(self):
        plans = {w: self.graph.data['work', w]['plan'] for w in self.rule['plan_sha256']}
        self_tests = [{'components': {'app': '2' * 64}}, {'components': self.local}]
        with self.assertRaisesRegex(Pending, '不同版本'):
            isolated_retest.test_target(self.parent, 'recQA', self.test_proof, plans,
                                        self.test_proof['subject_revisions'], self_tests)
