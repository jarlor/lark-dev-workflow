import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
spec = importlib.util.spec_from_file_location('unattended_control_tested', SCRIPTS / 'unattended_control.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeCrontab:
    def __init__(self, text='', *, exists=True):
        self.text = text
        self.exists = exists
        self.calls = []
        self.reads = 0
        self.read_overrides = {}
        self.write_result = 0
        self.corrupt_after_write = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv == ['crontab', '-l']:
            self.reads += 1
            override = self.read_overrides.get(self.reads)
            if override is not None:
                if isinstance(override, Exception):
                    raise override
                return subprocess.CompletedProcess(argv, *override)
            if not self.exists:
                return subprocess.CompletedProcess(argv, 1, '', 'no crontab for fixture\n')
            return subprocess.CompletedProcess(argv, 0, self.text, '')
        if argv != ['crontab', '-']:
            raise AssertionError(f'Unexpected command: {argv}')
        if not self.write_result:
            self.text = kwargs['input'] if self.corrupt_after_write is None else self.corrupt_after_write
            self.exists = True
        return subprocess.CompletedProcess(argv, self.write_result, '', '')

    @property
    def writes(self):
        return [kwargs['input'] for argv, kwargs in self.calls if argv == ['crontab', '-']]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='workflow control ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workdir = self.root / 'project with spaces'
        self.workdir.mkdir()
        self.binding = self.workdir / 'binding.json'
        self.binding.write_text('{"fixture": "unchanged"}\n')
        self.config_path = self.workdir / '.codex' / 'runner config.json'
        self.binary_dir = self.root / 'tool bins'
        self.binary_dir.mkdir()
        self.binaries = {}
        for command in ('python3', 'codex', 'lark-cli'):
            path = self.binary_dir / command
            path.write_text('#!/bin/sh\nexit 99\n')
            path.chmod(0o700)
            self.binaries[command] = str(path)
        self.entrypoint_dir = self.root / 'skill scripts'
        self.entrypoint_dir.mkdir()
        self.entrypoint = self.entrypoint_dir / 'unattended.sh'
        self.entrypoint.write_text('#!/bin/sh\nexit 99\n')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(m, '__file__', str(self.entrypoint_dir / 'unattended_control.py')).start()
        mock.patch.object(m.tempfile, 'gettempdir', return_value=str(self.root)).start()
        mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin:relative-entry'}).start()
        self.config = m.init_config(self.config_path, self.binding, self.workdir,
                                    'fixture-user', which=self.binaries.get)

    def write_config(self, **changes):
        value = copy.deepcopy(self.config)
        value.update(changes)
        self.config_path.write_text(json.dumps(value))
        return value

    def install(self, cron):
        return m.install_cron(self.config_path, runner=cron, platform='linux')

    def remove(self, cron):
        return m.remove_cron(self.config_path, runner=cron, platform='linux')


class ConfigTests(Fixture):
    def test_init_uses_private_files_and_fixed_absolute_defaults_without_touching_binding(self):
        self.assertEqual(m.load_config(self.config_path), self.config)
        self.assertEqual(self.config['format_version'], 1)
        self.assertEqual(self.config['interval_minutes'], 5)
        self.assertEqual(self.config['run_timeout_seconds'], 1800)
        self.assertEqual(self.config['max_consecutive_runs'], 12)
        self.assertEqual(self.config['reconcile_interval_seconds'], 86400)
        self.assertIsNone(self.config['codex_profile'])
        self.assertEqual(self.config['lark_profile'], 'fixture-user')
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.config['state_dir']).stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.binding.read_text(), '{"fixture": "unchanged"}\n')
        self.assertNotIn('relative-entry', self.config['path'])
        self.assertTrue(all(Path(p).is_absolute() for p in self.config['path'].split(os.pathsep)))
        self.assertFalse(any('token' in k for k in self.config))

    def test_existing_configuration_is_never_overwritten(self):
        before = self.config_path.read_bytes()
        with self.assertRaises(m.ControlError):
            m.init_config(self.config_path, self.binding, self.workdir, 'other', which=self.binaries.get)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_dangling_destination_symlink_is_not_followed(self):
        destination = self.workdir / 'linked.json'
        target = self.workdir / 'missing.json'
        destination.symlink_to(target)
        with self.assertRaises(m.ControlError):
            m.init_config(destination, self.binding, self.workdir, 'fixture-user', which=self.binaries.get)
        self.assertTrue(destination.is_symlink())
        self.assertFalse(target.exists())

    def test_init_does_not_create_config_when_a_required_tool_is_missing(self):
        destination = self.workdir / 'missing-tool.json'
        with self.assertRaises(m.ControlError):
            m.init_config(destination, self.binding, self.workdir, 'fixture-user',
                          which=lambda name: None if name == 'codex' else self.binaries[name])
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name('missing-tool-state').exists())

    def test_malformed_config_types_and_unknown_fields_are_rejected(self):
        cases = [dict(interval_minutes=True), dict(interval_minutes=7), dict(interval_minutes=0),
                 dict(interval_minutes=61), dict(run_timeout_seconds=False),
                 dict(max_consecutive_runs=-1), dict(reconcile_interval_seconds='60'),
                 dict(format_version=True), dict(format_version=2), dict(lark_profile=''),
                 dict(codex_profile=4), dict(workdir=3), dict(api_token='must-not-be-stored')]
        for change in cases:
            with self.subTest(change=change):
                self.write_config(**change)
                with self.assertRaises(m.ControlError):
                    m.load_config(self.config_path)

    def test_paths_and_profiles_cannot_inject_cron_syntax(self):
        for field, value in [('workdir', 'relative'), ('path', '/usr/bin:'),
                             ('path', '/usr/bin:relative'), ('path', '/usr/%bin'),
                             ('state_dir', str(self.root / 'bad%dir')),
                             ('state_dir', str(self.root / 'bad\ndir')),
                             ('lark_profile', 'user\n* * * * * evil')]:
            with self.subTest(field=field, value=value):
                self.write_config(**{field: value})
                with self.assertRaises(m.ControlError):
                    m.load_config(self.config_path)
        with self.assertRaises(m.ControlError):
            m.load_config(42)

    def test_state_directory_cannot_cover_checkout_or_configurations(self):
        for state in (self.workdir, self.root, self.config_path.parent):
            with self.subTest(state=state):
                self.write_config(state_dir=str(state))
                with self.assertRaises(m.ControlError):
                    m.load_config(self.config_path)

    def test_executables_must_exist_and_be_executable(self):
        executable = Path(self.config['codex_bin'])
        executable.chmod(0o600)
        with self.assertRaises(m.ControlError):
            m.load_config(self.config_path)
        executable.unlink()
        with self.assertRaises(m.ControlError):
            m.load_config(self.config_path)

    def test_normalizes_absolute_dot_paths(self):
        value = self.write_config(workdir=str(self.workdir / '.'),
                                  project_config=str(self.workdir / 'unused' / '..' / 'binding.json'))
        self.assertNotEqual(value['project_config'], self.config['project_config'])
        self.assertEqual(m.load_config(self.config_path), self.config)

    def test_resolving_npm_entrypoints_preserves_original_node_bin_on_path(self):
        node_bin = self.root / 'node installation' / 'bin'
        node_bin.mkdir(parents=True)
        package_bin = self.root / 'node installation' / 'lib' / 'node_modules' / 'fixture'
        package_bin.mkdir(parents=True)
        commands = dict(self.binaries)
        for command in ('codex', 'lark-cli'):
            target = package_bin / (command + '.js')
            target.write_text('#!/usr/bin/env node\n')
            target.chmod(0o700)
            entrypoint = node_bin / command
            entrypoint.symlink_to(target)
            commands[command] = str(entrypoint)
        with mock.patch.dict(os.environ, {'PATH': str(node_bin) + ':/usr/bin:/bin'}):
            value = m.init_config(self.workdir / 'npm runner.json', self.binding, self.workdir,
                                  'fixture-user', which=commands.get)
        self.assertEqual(value['codex_bin'], str(package_bin / 'codex.js'))
        self.assertEqual(value['lark_bin'], str(package_bin / 'lark-cli.js'))
        self.assertIn(str(node_bin), value['path'].split(os.pathsep))


class CronTests(Fixture):
    def test_preview_quotes_spaces_sets_only_command_path_and_does_not_execute(self):
        line = m.cron_preview(self.config_path)
        self.assertTrue(line.startswith('*/5 * * * * PATH='))
        self.assertTrue(line.endswith('\n'))
        tokens = shlex.split(line, comments=True)
        self.assertEqual(tokens[:5], ['*/5', '*', '*', '*', '*'])
        self.assertEqual(tokens[5], 'PATH=' + self.config['path'])
        self.assertEqual(tokens[6], 'PYTHON3=' + self.config['python_bin'])
        self.assertEqual(tokens[7:12], ['/bin/sh', str(self.entrypoint), 'tick', '--config', str(self.config_path)])
        self.assertEqual(tokens[12:], ['>>', str(Path(self.config['state_dir']) / 'cron.log'), '2>&1'])
        self.assertIn(m._marker(self.config_path), line)

    def test_all_supported_intervals_produce_valid_single_hour_cron_patterns(self):
        for interval in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60):
            with self.subTest(interval=interval):
                self.write_config(interval_minutes=interval)
                wanted = '0' if interval == 60 else '*' if interval == 1 else f'*/{interval}'
                self.assertEqual(m.cron_preview(self.config_path).split()[0], wanted)

    def test_install_preserves_other_entries_and_private_backup_then_readback(self):
        before = 'MAILTO=ops@example.invalid\n# user entry\n3 * * * * /bin/true\n'
        cron = FakeCrontab(before)
        result = self.install(cron)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(cron.text, before + m.cron_preview(self.config_path))
        backup = Path(result['backup_path'])
        self.assertEqual(backup.read_text(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(cron.reads, 3)
        self.assertEqual(len(cron.writes), 1)
        self.assertTrue(all(kwargs['env']['LC_ALL'] == 'C' for _, kwargs in cron.calls))

    def test_install_is_idempotent_and_remove_leaves_other_project_entries(self):
        unrelated = '* * * * * /bin/true # lark-dev-workflow:another-project\n'
        cron = FakeCrontab(unrelated)
        self.install(cron)
        result = self.install(cron)
        self.assertEqual(result['status'], 'unchanged')
        self.assertIsNone(result['backup_path'])
        self.assertEqual(len(cron.writes), 1)
        result = self.remove(cron)
        self.assertEqual(result['status'], 'removed')
        self.assertEqual(cron.text, unrelated)
        self.assertEqual(self.remove(cron)['status'], 'unchanged')

    def test_updates_only_own_marker_and_removes_duplicate_own_entries(self):
        marker = m._marker(self.config_path)
        cron = FakeCrontab('A=keep\n* * * * * old ' + marker + '\n' +
                           'B=also-keep\n* * * * * duplicate ' + marker + '\n')
        self.install(cron)
        self.assertEqual(cron.text, 'A=keep\n' + m.cron_preview(self.config_path) + 'B=also-keep\n')

    def test_missing_crontab_is_recognized_but_other_read_errors_are_not_empty(self):
        cron = FakeCrontab(exists=False)
        result = self.install(cron)
        self.assertFalse(result['previous_crontab_existed'])
        self.assertEqual(Path(result['backup_path']).read_text(), '')
        for failure in [(1, '', 'permission denied\n'), (2, '', 'no crontab for fixture\n'),
                        (1, 'unexpected output', 'no crontab for fixture\n'),
                        (1, '', 'no crontab for fixture\npermission denied\n')]:
            with self.subTest(failure=failure):
                broken = FakeCrontab()
                broken.read_overrides[1] = failure
                with self.assertRaises(m.ControlError):
                    self.install(broken)
                self.assertEqual(broken.writes, [])

    def test_read_change_before_write_aborts_without_overwriting(self):
        cron = FakeCrontab('initial\n')
        cron.read_overrides[2] = (0, 'someone changed this\n', '')
        with self.assertRaisesRegex(m.ControlError, 'changed before writing'):
            self.install(cron)
        self.assertEqual(cron.writes, [])
        self.assertEqual(cron.text, 'initial\n')
        backups = list((Path(self.config['state_dir']) / 'cron-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'initial\n')

    def test_unconfirmed_write_or_readback_never_claim_success_or_restore(self):
        cron = FakeCrontab('initial\n')
        cron.write_result = 1
        with self.assertRaisesRegex(m.ControlError, 'write was not confirmed'):
            self.install(cron)
        self.assertEqual(len(cron.writes), 1)
        cron = FakeCrontab('initial\n')
        cron.corrupt_after_write = 'concurrent edit\n'
        with self.assertRaisesRegex(m.ControlError, 'readback differs'):
            self.install(cron)
        self.assertEqual(len(cron.writes), 1)
        self.assertEqual(cron.text, 'concurrent edit\n')
        cron = FakeCrontab('initial\n')
        cron.read_overrides[3] = (1, '', 'permission denied')
        with self.assertRaisesRegex(m.ControlError, 'written but readback failed.*Backup:'):
            self.install(cron)
        self.assertEqual(len(cron.writes), 1)

    def test_non_linux_blocks_actual_changes_without_invoking_crontab(self):
        cron = FakeCrontab()
        for operation in (m.install_cron, m.remove_cron):
            with self.assertRaisesRegex(m.ControlError, 'only on Linux'):
                operation(self.config_path, runner=cron, platform='darwin')
        self.assertEqual(cron.calls, [])

    def test_missing_entrypoint_or_failed_subprocess_does_not_write(self):
        cron = FakeCrontab()
        self.entrypoint.unlink()
        with self.assertRaisesRegex(m.ControlError, 'entrypoint is missing'):
            self.install(cron)
        self.assertEqual(cron.calls, [])
        self.entrypoint.write_text('#!/bin/sh\n')
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('crontab', 30)):
            with self.subTest(error=error):
                cron = FakeCrontab()
                cron.read_overrides[1] = error
                with self.assertRaisesRegex(m.ControlError, 'could not complete'):
                    self.install(cron)
                self.assertEqual(cron.writes, [])


if __name__ == '__main__':
    unittest.main()
