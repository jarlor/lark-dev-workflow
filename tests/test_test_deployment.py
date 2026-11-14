import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import test_deployment


class TestDeploymentEvidence(unittest.TestCase):
    def setUp(self):
        self.expected = {
            'environment': 'test', 'deployment_url': 'https://test.invalid/test-status.json',
            'repositories': {'web': {'sha': 'a' * 40, 'workflow_run_id': 123},
                             'api': {'sha': 'b' * 40, 'workflow_run_id': 456}},
            'health_urls': {'web': 'https://test.invalid/ready', 'api': 'https://test.invalid/api/ready'},
        }
        self.now = '2026-01-02T10:00:00Z'
        self.before = {
            'source_url': self.expected['deployment_url'], 'observed_at': '2026-01-02T09:59:59Z',
            'deployment': {'environment': 'test', 'state': 'ready',
                           'deployed_at': '2026-01-01T00:00:00Z',
                           'repositories': copy.deepcopy(self.expected['repositories'])},
            'health_checks': {name: {'url': url, 'checked_at': '2026-01-02T09:59:55Z', 'ok': True}
                              for name, url in self.expected['health_urls'].items()},
        }

    def check(self, after=None):
        return test_deployment.check(self.expected, self.before, after, now=self.now)

    def test_actual_public_shape_and_separate_health_accept_old_fixed_deployment(self):
        original = copy.deepcopy((self.expected, self.before))
        result = self.check()
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['window_checked'])
        self.assertEqual(result['human_acceptance'], 'not_evaluated')
        self.assertEqual((self.expected, self.before), original)
        result['deployment']['repositories']['web']['sha'] = 'c' * 40
        self.assertEqual((self.expected, self.before), original)

    def test_ci_checked_unchanged_deploying_and_failed_are_not_ready(self):
        for state in ('success', 'checked', 'unchanged', 'deploying', 'failed', None):
            with self.subTest(state=state):
                self.before['deployment']['state'] = state
                self.assertFalse(self.check()['ok'])

    def test_stale_ready_for_a_different_target_commit_is_rejected(self):
        self.expected['repositories']['web']['sha'] = 'c' * 40
        self.assertFalse(self.check()['ok'])

    def test_workflow_identity_must_match_not_only_source_sha(self):
        self.before['deployment']['repositories']['api']['workflow_run_id'] = 789
        self.assertFalse(self.check()['ok'])

    def test_missing_dependency_and_partial_cross_repository_update_are_rejected(self):
        saved = copy.deepcopy(self.before['deployment']['repositories'])
        del self.before['deployment']['repositories']['api']
        self.assertFalse(self.check()['ok'])
        self.before['deployment']['repositories'] = saved
        self.before['deployment']['repositories']['api']['sha'] = 'd' * 40
        self.assertFalse(self.check()['ok'])

    def test_full_sha_and_integer_workflow_id_required(self):
        for key, value in [('sha', 'main'), ('sha', 'abcdef0'), ('workflow_run_id', True),
                           ('workflow_run_id', '123'), ('workflow_run_id', 0)]:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(self.before)
                changed['deployment']['repositories']['web'][key] = value
                self.assertFalse(test_deployment.check(self.expected, changed, now=self.now)['ok'])

    def test_public_ready_boolean_is_not_current_health_evidence(self):
        self.before['deployment']['healthy'] = True
        self.before['deployment']['ready'] = True
        self.before['health_checks'] = {}
        self.assertFalse(self.check()['ok'])

    def test_each_required_health_result_and_actual_source_are_checked(self):
        for key, value in [('ok', False), ('ok', 1), ('url', 'https://other.invalid/ready'),
                           ('checked_at', '2026-01-02T09:50:00Z')]:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(self.before)
                changed['health_checks']['api'][key] = value
                self.assertFalse(test_deployment.check(self.expected, changed, now=self.now)['ok'])

    def test_environment_or_deployment_source_mismatch_is_rejected(self):
        self.before['deployment']['environment'] = 'production'
        self.assertFalse(self.check()['ok'])
        self.before['deployment']['environment'] = 'test'
        self.before['source_url'] = 'https://other.invalid/test-status.json'
        self.assertFalse(self.check()['ok'])

    def test_missing_naive_invalid_and_future_deployment_times_are_rejected(self):
        for value in (None, '', 'yesterday', '2026-01-01T00:00:00', '2026-01-03T00:00:00Z'):
            with self.subTest(value=value):
                self.before['deployment']['deployed_at'] = value
                self.assertFalse(self.check()['ok'])

    def test_observation_must_be_current_and_not_in_the_future(self):
        for value in ('2026-01-02T09:50:00Z', '2026-01-02T10:01:00Z'):
            with self.subTest(value=value):
                self.before['observed_at'] = value
                self.assertFalse(self.check()['ok'])

    def test_health_before_deployment_or_after_capture_is_rejected(self):
        for value in ('2025-12-31T23:59:59Z', '2026-01-02T10:01:00Z'):
            with self.subTest(value=value):
                self.before['health_checks']['web']['checked_at'] = value
                self.assertFalse(self.check()['ok'])

    def test_health_freshness_is_relative_to_now_not_only_capture(self):
        self.before['observed_at'] = '2026-01-02T09:56:00Z'
        for value in self.before['health_checks'].values():
            value['checked_at'] = '2026-01-02T09:52:00Z'
        self.assertFalse(self.check()['ok'])

    def test_long_test_can_have_historical_start_and_current_end(self):
        after = copy.deepcopy(self.before)
        self.before['observed_at'] = '2026-01-02T08:59:59Z'
        for value in self.before['health_checks'].values():
            value['checked_at'] = '2026-01-02T08:59:55Z'
        self.assertTrue(self.check(after)['ok'])
        self.assertFalse(self.check()['ok'])

    def test_long_test_still_requires_fresh_health_at_start(self):
        after = copy.deepcopy(self.before)
        self.before['observed_at'] = '2026-01-02T08:59:59Z'
        for value in self.before['health_checks'].values():
            value['checked_at'] = '2026-01-02T08:40:00Z'
        self.assertFalse(self.check(after)['ok'])

    def test_after_observation_and_after_health_must_be_current(self):
        for field in ('observed_at', 'health'):
            with self.subTest(field=field):
                after = copy.deepcopy(self.before)
                if field == 'observed_at':
                    after[field] = '2026-01-02T09:50:00Z'
                else:
                    after['health_checks']['api']['checked_at'] = '2026-01-02T09:50:00Z'
                self.assertFalse(self.check(after)['ok'])

    def test_changed_commit_or_failed_deployment_during_test_cannot_pass(self):
        for change in ('sha', 'state'):
            with self.subTest(change=change):
                after = copy.deepcopy(self.before)
                if change == 'sha':
                    after['deployment']['repositories']['web']['sha'] = 'c' * 40
                else:
                    after['deployment']['state'] = 'failed'
                self.assertFalse(self.check(after)['ok'])

    def test_same_sha_redeployment_invalidates_stable_window(self):
        after = copy.deepcopy(self.before)
        after['deployment']['deployed_at'] = '2026-01-02T09:00:00Z'
        result = self.check(after)
        self.assertFalse(result['ok'])
        self.assertIn('部署发生变化', result['errors'][0])

    def test_equivalent_timezone_notation_does_not_fabricate_a_new_deployment(self):
        after = copy.deepcopy(self.before)
        after['deployment']['deployed_at'] = '2026-01-01T08:00:00+08:00'
        self.assertTrue(self.check(after)['ok'])

    def test_reversed_observation_order_is_rejected(self):
        after = copy.deepcopy(self.before)
        after['observed_at'] = '2026-01-02T09:59:58Z'
        self.assertFalse(self.check(after)['ok'])

    def test_required_configuration_and_malformed_inputs_fail_closed(self):
        for expected in (None, {}, {'environment': 'test'}):
            self.assertFalse(test_deployment.check(expected, self.before, now=self.now)['ok'])
        for key, value in [('repositories', {}), ('health_urls', {}), ('max_age_seconds', False),
                           ('max_age_seconds', 0), ('unexpected', True)]:
            with self.subTest(key=key):
                expected = copy.deepcopy(self.expected)
                expected[key] = value
                self.assertFalse(test_deployment.check(expected, self.before, now=self.now)['ok'])
        for before in (None, {}, {'observed_at': self.now}):
            self.assertFalse(test_deployment.check(self.expected, before, now=self.now)['ok'])

    def test_optional_native_metadata_does_not_require_an_invented_protocol(self):
        self.before['deployment']['capabilities'] = {'synthetic_only': True}
        self.before['deployment']['repositories']['web']['artifact_url'] = 'https://build.invalid/artifact'
        self.assertTrue(self.check()['ok'])

    def test_cli_uses_only_saved_files_and_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            paths = [p / 'expected.json', p / 'before.json']
            for path, obj in zip(paths, (self.expected, self.before)):
                path.write_text(json.dumps(obj))
            saved = [path.read_bytes() for path in paths]
            cmd = [sys.executable, str(SCRIPTS / 'test_deployment.py'), '--expected', str(paths[0]),
                   '--before', str(paths[1]), '--now', self.now]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertEqual([path.read_bytes() for path in paths], saved)
            paths[1].write_text('{"deployment": {}, "deployment": {}}')
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)['ok'])


if __name__ == '__main__':
    unittest.main()
