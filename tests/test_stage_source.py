import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import stage_action
import stage_source as source


class StageSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name).resolve()
        self.folder = self.directory / 'stage-proof-source'
        self.folder.mkdir(mode=0o700)
        self.reports = self.directory / 'reports'
        self.reports.mkdir(mode=0o700)
        self.report = self.reports / 'actual-result'
        self.report.write_bytes(b'two cases ran')
        self.hash = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.project = mock.Mock(path=str(self.directory / 'etc' / 'service.json'), directory=self.directory, paths=(str(self.directory / 'work'), 'state', 'project'),
                                 key='work-deliver', config=dict(app_id='cli_test', tenant_key='tenant',
                                 base_token='base', project_record_id='recProject', bot_open_id='ou_bot',
                                 lark_profile='project', table_ids=['tblWork']))
        self.program = self.folder / 'read_evidence.py'
        self.program.write_text("""import json, os, sys
q = json.load(sys.stdin)
assert not any(k in os.environ for k in ('STAGE_TEST_SECRET', 'HOME', 'LARK_APP_SECRET', 'PYTHONPATH'))
assert set(q) == {'protocol', 'kind', 'binding', 'action', 'role', 'record_id'}
source = {'path': %r, 'sha256': %r}
evidence = {'report_sha256': source['sha256'], 'components': {'app': source['sha256']}}
print(json.dumps({'request': q, 'sources': [source], 'evidence': evidence}))
""" % (str(self.report), self.hash))
        self.registration = dict(protocol=1, service='s', runner='r', roots=[str(self.reports)],
                                 reader_sha256=hashlib.sha256(self.program.read_bytes()).hexdigest())
        self.register()
        self.pins = mock.patch.object(source.workflow_rule, 'service_pins', return_value={'service': 's', 'runner': 'r'})
        self.addCleanup(self.pins.stop)
        self.pins.start()

    def register(self):
        (self.folder / 'registration.json').write_text(json.dumps(self.registration))

    def run_reader(self, kind='test'):
        # On development hosts the installer is not root, and /tmp is shared. The
        # owner check itself is exercised separately. The child process runs for real.
        with mock.patch.object(source, 'admin_path'):
            return source.query(self.project, kind, 'verification', 'recTest')

    def test_pinned_subprocess_reads_only_fixed_request_and_checks_real_bytes(self):
        with mock.patch.dict(os.environ, {'STAGE_TEST_SECRET': 'canary'}):
            self.assertEqual(self.run_reader()['report_sha256'], self.hash)
        self.assertTrue(self.run_reader('target')['verified_artifact'])
        self.report.write_bytes(b'replaced')
        with self.assertRaisesRegex(stage_action.Pending, '已变化'):
            self.run_reader()

    def test_assigned_acceptor_needs_two_real_distinct_sources_and_original_action(self):
        authorization = self.reports / 'test-acceptor-policy'
        authorization.write_bytes(b'Owner appointed a separate human to test this project.')
        pin = hashlib.sha256(authorization.read_bytes()).hexdigest()
        script = self.program.read_text().replace("evidence = {'report_sha256':",
            "evidence = {'authorization_sha256': %r, 'assignment_sha256': %r, 'report_sha256':" % (pin, self.hash)
        ).replace("'sources': [source]", "'sources': [source, %r]" % {'path':str(authorization),'sha256':pin})
        with mock.patch.object(source, 'admin_path'):
            def query(key='req-start',role='requirements'):
                self.project.key = key
                return source.query(self.project, 'acceptor_assignment', role, 'recSubject')
            self.program.write_text(script)
            self.registration['reader_sha256'] = hashlib.sha256(self.program.read_bytes()).hexdigest()
            self.register()
            self.assertEqual(query()['assignment_sha256'], self.hash)
            for key,role in [('req-accept','requirements'),('req-start','work')]:
                with self.assertRaises(stage_action.Pending): query(key,role)
            self.program.write_text(script.replace(pin,self.hash,1))
            self.registration['reader_sha256'] = hashlib.sha256(self.program.read_bytes()).hexdigest()
            self.register()
            with self.assertRaisesRegex(stage_action.Pending,'原始授权'): query()

    def test_owner_handoff_is_work_only_and_requires_distinct_verified_authorization_and_report(self):
        auth = self.reports / 'approval'
        auth.write_bytes(b'explicit authorization for this owner change')
        approval_hash = hashlib.sha256(auth.read_bytes()).hexdigest()
        script = self.program.read_text().replace(
            "evidence = {'report_sha256':", "evidence = {'authorization_sha256': %r, 'report_sha256':" % approval_hash
        ).replace("'sources': [source]", "'sources': [source, %r]" % {'path': str(auth), 'sha256': approval_hash})
        with mock.patch.object(source, 'admin_path'):
            for valid in (True, False):
                self.program.write_text(script if valid else script.replace(approval_hash, self.hash, 1))
                self.registration['reader_sha256'] = hashlib.sha256(self.program.read_bytes()).hexdigest()
                self.register()
                if valid:
                    with self.assertRaises(stage_action.Pending):
                        self.run_reader('owner_handoff')
                    self.assertEqual(source.query(self.project, 'owner_handoff', 'work', 'recWork')
                                     ['authorization_sha256'], approval_hash)
                else:
                    with self.assertRaisesRegex(stage_action.Pending, '授权记录'):
                        source.query(self.project, 'owner_handoff', 'work', 'recWork')
            auth.write_bytes(b'changed')
            with self.assertRaisesRegex(stage_action.Pending, '已变化'):
                source.query(self.project, 'owner_handoff', 'work', 'recWork')

    def test_replaced_reader_service_binding_or_project_location_are_refused(self):
        self.program.write_text('raise SystemExit(99)')
        with self.assertRaisesRegex(stage_action.Pending, '读取器与登记'):
            self.run_reader()
        self.registration['service'] = 'other'
        self.register()
        with self.assertRaisesRegex(stage_action.Pending, '正式绑定'):
            self.run_reader()
        self.project.paths = (str(self.directory), 'state', 'project')
        with self.assertRaisesRegex(stage_action.Pending, '工作区'):
            self.run_reader()

    def test_other_writable_install_path_symlinks_and_outside_files_are_refused(self):
        linked = self.reports / 'other'
        linked.symlink_to(self.report)
        with self.assertRaises(stage_action.Pending):
            source.response_sources([{'path': str(linked), 'sha256': self.hash}], [self.reports])
        with self.assertRaises(stage_action.Pending):
            source.response_sources([{'path': str(self.report), 'sha256': self.hash}], [self.folder])
        self.program.chmod(0o666)
        with self.assertRaises(stage_action.Pending):
            source.admin_path(self.program)
        self.folder.chmod(0o755)
        with self.assertRaisesRegex(stage_action.Pending, '目录不能'):
            self.run_reader()

    def test_separate_admin_config_for_non_root_runner_and_ambiguous_install(self):
        # Resolving the source never requires a non-root runner's state parent to be admin-owned.
        etc = self.directory / 'etc'
        etc.mkdir()
        managed = etc / 'stage-proof-source'
        self.folder.rename(managed)
        self.assertEqual(source.location(self.project), managed)
        self.folder.mkdir(mode=0o700)
        with self.assertRaisesRegex(stage_action.Pending, '两份'):
            source.location(self.project)

    def test_unknown_query_no_default_and_duplicate_sources_fail(self):
        with self.assertRaises(stage_action.Pending):
            self.run_reader('shell')
        src = {'path': str(self.report), 'sha256': self.hash}
        with self.assertRaises(stage_action.Pending):
            source.response_sources([src, src], [self.reports])
        self.folder.rename(self.directory / 'not-enrolled')
        with self.assertRaisesRegex(stage_action.Pending, '尚未接入'):
            self.run_reader()


if __name__ == '__main__':
    unittest.main()
