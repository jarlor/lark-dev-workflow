#!/usr/bin/env python3
"""Project-local configuration and opt-in Linux crontab management.

This module does not start Codex or contact Feishu. Cron writes are explicit and
only replace the line marked for the selected configuration file.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


class ControlError(ValueError):
    pass


CONFIG_FIELDS = frozenset({
    'format_version', 'project_config', 'workdir', 'state_dir', 'python_bin',
    'codex_bin', 'lark_bin', 'path', 'lark_profile', 'codex_profile',
    'interval_minutes', 'run_timeout_seconds', 'max_consecutive_runs',
    'reconcile_interval_seconds',
})


def _text(value, name, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ControlError(f'{name} must be a nonempty string without outer whitespace.')
    if any(c in value for c in ('\n', '\r', '\0', '%')):
        raise ControlError(f'{name} must not contain newlines, NUL or %.')
    return value


def _absolute(value, name, *, base=None):
    try:
        text = _text(os.fspath(value), name)
    except TypeError as exc:
        raise ControlError(f'{name} must be a path string.') from exc
    path = Path(text).expanduser()
    if not path.is_absolute():
        if base is None:
            raise ControlError(f'{name} must be an absolute path.')
        path = base / path
    result = path.resolve()
    _text(str(result), name)
    return result


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ControlError(f'{name} must be a positive integer.')
    return value


def _interval(value):
    _positive_int(value, 'interval_minutes')
    if 60 % value:
        raise ControlError('interval_minutes must divide 60 exactly.')
    return value


def _inside(path, directory):
    return path == directory or directory in path.parents


def _validate(value, config_path):
    if not isinstance(value, dict) or set(value) != CONFIG_FIELDS:
        missing = sorted(CONFIG_FIELDS - set(value)) if isinstance(value, dict) else sorted(CONFIG_FIELDS)
        unknown = sorted(set(value) - CONFIG_FIELDS) if isinstance(value, dict) else []
        raise ControlError(f'Configuration fields differ: missing={missing}, unknown={unknown}.')
    if type(value['format_version']) is not int or value['format_version'] != 1:
        raise ControlError('Only runner configuration format_version=1 is supported.')
    result = dict(value)
    for name in ('project_config', 'workdir', 'state_dir', 'python_bin', 'codex_bin', 'lark_bin'):
        if not isinstance(value[name], str):
            raise ControlError(f'{name} must be an absolute path string.')
        result[name] = str(_absolute(value[name], name))
    project = Path(result['project_config'])
    workdir = Path(result['workdir'])
    state = Path(result['state_dir'])
    if config_path == project:
        raise ControlError('Runner configuration cannot replace the project binding.')
    if not project.is_file():
        raise ControlError('project_config must name an existing project binding file.')
    if not workdir.is_dir():
        raise ControlError('workdir must name an existing directory.')
    if state.exists() and not state.is_dir():
        raise ControlError('state_dir exists and is not a directory.')
    if _inside(workdir, state) or _inside(project, state) or _inside(config_path, state):
        raise ControlError('state_dir must not contain workdir or either configuration file.')
    for name in ('python_bin', 'codex_bin', 'lark_bin'):
        executable = Path(result[name])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ControlError(f'{name} must name an executable file.')
        if executable in (project, config_path) or _inside(executable, state):
            raise ControlError(f'{name} conflicts with configuration or private state paths.')
    result['lark_profile'] = _text(value['lark_profile'], 'lark_profile')
    result['codex_profile'] = _text(value['codex_profile'], 'codex_profile', optional=True)
    result['interval_minutes'] = _interval(value['interval_minutes'])
    for name in ('run_timeout_seconds', 'max_consecutive_runs', 'reconcile_interval_seconds'):
        result[name] = _positive_int(value[name], name)
    entries = _text(value['path'], 'path').split(os.pathsep)
    result['path'] = os.pathsep.join(str(_absolute(item, 'PATH entry')) for item in entries)
    return result


def load_config(path):
    """Load and validate the complete runner configuration; never mutate it."""
    config_path = _absolute(path, 'config_path', base=Path.cwd())
    try:
        value = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ControlError('Cannot read a valid runner configuration.') from exc
    return _validate(value, config_path)


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _new_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # link is atomic and refuses an existing destination, including symlinks.
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ControlError('Runner configuration already exists; it was not overwritten.') from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def init_config(path, project_config, workdir, lark_profile, interval_minutes=5,
                codex_profile=None, *, which=shutil.which):
    """Create a new private configuration without touching the project binding."""
    # Reject a dangling link too: resolving first could otherwise create its target.
    try:
        if Path(path).expanduser().is_symlink():
            raise ControlError('Runner configuration already exists; it was not overwritten.')
    except TypeError as exc:
        raise ControlError('config_path must be a path string.') from exc
    config_path = _absolute(path, 'config_path', base=Path.cwd())
    if config_path.exists() or config_path.is_symlink():
        raise ControlError('Runner configuration already exists; it was not overwritten.')
    binaries = {}
    for name, command in (('python_bin', 'python3'), ('codex_bin', 'codex'), ('lark_bin', 'lark-cli')):
        found = which(command)
        if not found:
            raise ControlError(f'{command} was not found on PATH.')
        binaries[name] = str(_absolute(found, name, base=Path.cwd()))
    directories = [str(Path(value).parent) for value in binaries.values()]
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry and Path(entry).is_absolute():
            directories.append(str(_absolute(entry, 'PATH entry')))
    directories.extend(['/usr/local/bin', '/usr/bin', '/bin'])
    fixed_path = os.pathsep.join(dict.fromkeys(directories))
    value = {
        'format_version': 1,
        'project_config': str(_absolute(project_config, 'project_config', base=Path.cwd())),
        'workdir': str(_absolute(workdir, 'workdir', base=Path.cwd())),
        'state_dir': str(config_path.parent / (config_path.stem + '-state')),
        **binaries,
        'path': fixed_path,
        'lark_profile': lark_profile,
        'codex_profile': codex_profile,
        'interval_minutes': interval_minutes,
        'run_timeout_seconds': 1800,
        'max_consecutive_runs': 12,
        'reconcile_interval_seconds': 86400,
    }
    value = _validate(value, config_path)
    _private_directory(Path(value['state_dir']))
    _new_json(config_path, value)
    return value


def _marker(config_path):
    suffix = hashlib.sha256(str(config_path).encode('utf-8')).hexdigest()[:16]
    return '# lark-dev-workflow:' + suffix


def cron_preview(config_path):
    """Return only this configuration's cron line; do not read or change cron."""
    path = _absolute(config_path, 'config_path', base=Path.cwd())
    config = load_config(path)
    entrypoint = _absolute(Path(__file__).with_name('unattended.sh'), 'entrypoint')
    interval = config['interval_minutes']
    minute = '0' if interval == 60 else ('*' if interval == 1 else f'*/{interval}')
    command = '/bin/sh ' + shlex.quote(str(entrypoint)) + ' tick --config ' + shlex.quote(str(path))
    logfile = _absolute(Path(config['state_dir']) / 'cron.log', 'cron_log')
    return (f'{minute} * * * * PATH={shlex.quote(config["path"])} '
            f'PYTHON3={shlex.quote(config["python_bin"])} '
            f'{command} >> {shlex.quote(str(logfile))} 2>&1 {_marker(path)}\n')


def _run(runner, argv, *, input=None):
    try:
        return runner(argv, input=input, capture_output=True, text=True, timeout=30,
                      env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError('The crontab command could not complete; no success is assumed.') from exc


def _read_crontab(runner):
    result = _run(runner, ['crontab', '-l'])
    if result.returncode == 0 and isinstance(result.stdout, str):
        return result.stdout, True
    stderr = result.stderr if isinstance(result.stderr, str) else ''
    if (result.returncode == 1 and not result.stdout and
            re.fullmatch(r'(?:crontab:\s*)?no crontab for [^\r\n]+\s*', stderr.strip())):
        return '', False
    raise ControlError('Cannot read the current crontab; it was not treated as empty or overwritten.')


def _replace_own(text, marker, replacement):
    result, replaced = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip().endswith(marker):
            if replacement is not None and not replaced:
                result.append(replacement)
            replaced = True
        else:
            result.append(line)
    updated = ''.join(result)
    if replacement is not None and not replaced:
        if updated and not updated.endswith('\n'):
            updated += '\n'
        updated += replacement
    return updated


@contextmanager
def _cron_lock(config):
    # Shared by all project configs of this user, avoiding parallel installers.
    lock_dir = Path(tempfile.gettempdir()) / f'lark-dev-workflow-cron-{os.getuid()}'
    try:
        lock_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if lock_dir.is_symlink() or not lock_dir.is_dir() or lock_dir.stat().st_uid != os.getuid():
        raise ControlError('Cannot safely use the per-user cron installation lock.')
    if lock_dir.stat().st_mode & 0o077:
        raise ControlError('The per-user cron installation lock directory is not private.')
    lockfile = lock_dir / 'install.lock'
    try:
        descriptor = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ControlError('Cannot safely open the cron installation lock.') from exc
    with os.fdopen(descriptor, 'a+b') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ControlError('Another cron installation is in progress; retry later.') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _backup(config, previous):
    directory = Path(config['state_dir']) / 'cron-backups'
    _private_directory(directory)
    descriptor, filename = tempfile.mkstemp(prefix='crontab-', suffix='.txt', dir=directory)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(previous)
        stream.flush()
        os.fsync(stream.fileno())
    return filename


def _change_cron(config_path, *, remove, runner, platform):
    if platform != 'linux':
        raise ControlError('Installing or removing cron entries is supported only on Linux.')
    path = _absolute(config_path, 'config_path', base=Path.cwd())
    config = load_config(path)
    marker = _marker(path)
    replacement = None if remove else cron_preview(path)
    if not remove and not Path(__file__).with_name('unattended.sh').is_file():
        raise ControlError('The bundled unattended.sh entrypoint is missing.')
    with _cron_lock(config):
        before, existed = _read_crontab(runner)
        after = _replace_own(before, marker, replacement)
        if after == before:
            return {'status': 'unchanged', 'changed': False, 'marker': marker, 'backup_path': None}
        _private_directory(Path(config['state_dir']))
        backup = _backup(config, before)
        if _read_crontab(runner) != (before, existed):
            raise ControlError(f'Crontab changed before writing; no write attempted. Backup: {backup}')
        result = _run(runner, ['crontab', '-'], input=after)
        if result.returncode != 0:
            raise ControlError(f'Crontab write was not confirmed; inspect current state. Backup: {backup}')
        try:
            actual, _ = _read_crontab(runner)
        except ControlError as exc:
            raise ControlError(f'Crontab was written but readback failed; inspect current state. Backup: {backup}') from exc
        if actual != after:
            raise ControlError(f'Crontab readback differs; no automatic rollback attempted. Backup: {backup}')
    return {'status': 'removed' if remove else 'installed', 'changed': True,
            'marker': marker, 'backup_path': backup, 'previous_crontab_existed': existed}


def install_cron(config_path, *, runner=subprocess.run, platform=sys.platform):
    return _change_cron(config_path, remove=False, runner=runner, platform=platform)


def remove_cron(config_path, *, runner=subprocess.run, platform=sys.platform):
    return _change_cron(config_path, remove=True, runner=runner, platform=platform)
