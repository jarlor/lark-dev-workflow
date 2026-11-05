#!/usr/bin/env python3
"""Bounded, cron-driven Codex worker. Scheduling checkpoints are not business acknowledgements."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from onboard import OnboardingError, atomic_json, read_json
import comments
import unattended_control as control
import watch

HERE = Path(__file__).resolve().parent
RESULT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['run_id', 'snapshot_id', 'outcome', 'summary', 'next_step', 'retry_after_seconds'],
    'properties': {
        'run_id': {'type': 'string'}, 'snapshot_id': {'type': 'string'},
        'outcome': {'type': 'string', 'enum': ['continue', 'waiting', 'idle', 'paused']},
        'summary': {'type': 'string'}, 'next_step': {'type': 'string'},
        'retry_after_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 86400},
    },
}


class RunnerError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def stable_snapshot(snapshot):
    return {key: value for key, value in snapshot.items() if key != 'observed_at'}


def identity(config, binding):
    return digest({'binding': comments.binding_identity(binding),
                   'tables': binding.get('tables'), 'project_number': binding.get('project_number'),
                   'workdir': config['workdir'], 'lark_profile': config['lark_profile'],
                   'codex_profile': config.get('codex_profile')})


@contextmanager
def lock_workspace(config):
    # All workers sharing a checkout serialize, even when they use different state directories.
    path = Path(config['workdir']) / '.lark-dev-workflow' / 'unattended.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield stream.fileno()
        finally:
            # Do not explicitly unlock: a surviving Codex child inherits the lock descriptor.
            # Closing our descriptor leaves its lock in place until that child exits.
            pass


def state_paths(config):
    directory = Path(config['state_dir'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory, directory / 'state.json', directory / 'PAUSED'


def load_state(path, expected_identity):
    state = read_json(path) if path.exists() else {
        'format_version': 1, 'identity': expected_identity,
        'checkpoint': None, 'session_id': None, 'idle_wakes': 0,
    }
    if state.get('format_version') != 1 or state.get('identity') != expected_identity:
        raise RunnerError('绑定、账号或工作目录已改变；请核对旧运行状态，不会自动改绑。')
    return state


def should_run(state, snapshot_id, timestamp, reconcile_seconds):
    if state.get('checkpoint') != snapshot_id:
        return True, 'changed' if state.get('checkpoint') else 'initial'
    due = state.get('next_due_at')
    if due is not None and timestamp >= due:
        return True, 'continuation'
    if timestamp - state.get('last_success_epoch', 0) >= reconcile_seconds:
        return True, 'reconcile'
    return False, 'unchanged'


def validate_result(result, run_id, snapshot_id):
    if not isinstance(result, dict) or set(result) != set(RESULT_SCHEMA['required']):
        raise RunnerError('Codex 未返回完整的本轮运行结果。')
    if result['run_id'] != run_id or result['snapshot_id'] != snapshot_id:
        raise RunnerError('Codex 结果对应其他轮次或快照。')
    if result['outcome'] not in RESULT_SCHEMA['properties']['outcome']['enum']:
        raise RunnerError('Codex 返回了未知的后续状态。')
    delay = result['retry_after_seconds']
    if type(delay) is not int or not 0 <= delay <= 86400:
        raise RunnerError('下次检查时间无效。')
    if not all(isinstance(result[key], str) and result[key].strip() for key in ['summary', 'next_step']):
        raise RunnerError('运行结果缺少说明或下一步。')
    return result


def project_governance(config, binding):
    """Read explicit project rules locally; a configured but unreadable source is fatal."""
    governance = binding.get('governance', {})
    if not isinstance(governance, dict):
        raise RunnerError('项目 governance 必须是对象。')
    if 'rules_reference' not in governance:
        return governance, None
    reference = governance['rules_reference']
    if not isinstance(reference, str) or not reference.strip() or '://' in reference:
        raise RunnerError('governance.rules_reference 必须是可读取的本地文件路径。')
    source = Path(reference)
    if not source.is_absolute():
        source = Path(config['workdir']) / source
    try:
        source = source.resolve(strict=True)
        if not source.is_file():
            raise OSError('引用不是普通文件')
        content = source.read_text(encoding='utf-8')
    except (OSError, ValueError, RuntimeError) as exc:
        raise RunnerError(f'无法读取项目规则 governance.rules_reference={reference!r}（{source}）：{exc}') from exc
    return governance, {'source': str(source), 'content': content}


def build_prompt(config, state, run_id, snapshot_id, snapshot_file, reason, expected_binding=None):
    instructions = (HERE.parent / 'references/unattended-worker.md').read_text(encoding='utf-8')
    binding = read_json(config['project_config'])
    if expected_binding is not None and (
            identity(config, binding) != identity(config, expected_binding)
            or watch.feedback_intake_mode(binding) != watch.feedback_intake_mode(expected_binding)):
        raise RunnerError('本轮采集期间项目绑定已改变；请核对配置与快照，不会执行旧项目快照。')
    governance, rules = project_governance(config, binding)
    context = {
        'governance': governance, 'project_rules': rules,
        'run_id': run_id, 'snapshot_id': snapshot_id, 'reason': reason,
        'skill_file': str(HERE.parent / 'SKILL.md'), 'project_config': config['project_config'],
        'lark_profile': config['lark_profile'], 'snapshot_file': str(snapshot_file),
        'previous_result': state.get('last_result'),
        'recovery_note': state.get('recovery_note'),
        'previous_interrupted_run': (state.get('recoveries') or [{}])[-1].get('inflight'),
    }
    return instructions + '\n\n本轮本地配置与输入位置：\n' + json.dumps(context, ensure_ascii=False, indent=2)


def codex_command(config, state, run_dir):
    argv = [config['codex_bin']]
    if config.get('codex_profile'):
        argv += ['--profile', config['codex_profile']]
    # Suppress terminal approval prompts without widening the configured sandbox/permissions.
    argv += ['-c', 'approval_policy="never"', 'exec']
    if state.get('session_id'):
        argv += ['resume', state['session_id']]
    argv += ['--json', '--output-schema', str(run_dir / 'result-schema.json'),
             '--output-last-message', str(run_dir / 'result.json'), '-']
    return argv


def terminate_child(child):
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def read_events(path, expected_session=None):
    session_id, completed, failed = expected_session, False, False
    if not path.exists():
        return session_id, completed, failed
    with path.open(encoding='utf-8') as stream:
        for raw in stream:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'thread.started':
                value = event.get('thread_id')
                try:
                    uuid.UUID(value)
                except (ValueError, TypeError, AttributeError):
                    raise RunnerError('Codex 返回的会话编号无效。')
                if session_id and session_id != value:
                    raise RunnerError('恢复运行时返回了其他会话。')
                session_id = value
            if event.get('type') == 'turn.completed':
                completed = True
            if event.get('type') == 'turn.failed':
                failed = True
    return session_id, completed, failed


def execute(config, state, run_dir, prompt, lock_fd, save_state):
    atomic_json(run_dir / 'result-schema.json', RESULT_SCHEMA)
    (run_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')
    env = dict(os.environ, PATH=config['path'], PYTHON3=config['python_bin'],
               LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
    interrupted_run = False
    with (run_dir / 'events.jsonl').open('w') as events, (run_dir / 'stderr.log').open('w') as errors:
        child = subprocess.Popen(codex_command(config, state, run_dir), cwd=config['workdir'], env=env,
                                 stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True,
                                 start_new_session=True, pass_fds=(lock_fd,))
        try:
            state['inflight'].update(pid=child.pid, phase='running')
            save_state()
        except Exception:
            terminate_child(child)
            raise
        old_handler = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise KeyboardInterrupt('收到停止信号')
        signal.signal(signal.SIGTERM, interrupted)
        try:
            child.communicate(prompt, timeout=config['run_timeout_seconds'])
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            terminate_child(child)
            interrupted_run = True
        finally:
            signal.signal(signal.SIGTERM, old_handler)
    session_id, completed, failed = read_events(run_dir / 'events.jsonl', state.get('session_id'))
    if session_id:
        state['session_id'] = session_id
        save_state()
    if interrupted_run:
        raise RunnerError('本轮已中断或超时；可能有部分写入，需核对后恢复。')
    if child.returncode or failed:
        raise RunnerError('Codex 执行失败；已保留会话编号、日志和未处理快照。')
    if not completed or not session_id:
        raise RunnerError('缺少 Codex 本轮完成事件或会话编号。')
    return read_json(run_dir / 'result.json'), session_id


def collect_snapshot(config, binding, directory, collector):
    # Use the selected executable itself and retain PATH for its runtime (for example Node).
    old_path = os.environ.get('PATH')
    os.environ['PATH'] = config['path']
    try:
        return collector(binding, config['lark_profile'],
                         comments_state_path=directory / 'comments-state.json', lark_bin=config['lark_bin'])
    finally:
        if old_path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = old_path


def scan_only(config_path, *, collector=watch.collect):
    config = control.load_config(config_path)
    directory, _, _ = state_paths(config)
    with lock_workspace(config) as lock_fd:
        if lock_fd is None:
            return {'status': 'busy', 'message': '上一轮仍在运行，本轮跳过。'}
        snapshot = collect_snapshot(config, read_json(config['project_config']), directory, collector)
        atomic_json(directory / 'last-scan.json', snapshot)
        return {'status': 'scanned', 'records': len(snapshot.get('records', {})),
                'comments': len(snapshot.get('comments', {})), 'notices': len(snapshot.get('notices', {})),
                'coverage': snapshot.get('coverage', {}),
                'codex_started': False, 'checkpoint_advanced': False}


def tick(config_path, *, collector=watch.collect, executor=execute, clock=time.time):
    config = control.load_config(config_path)
    directory, state_path, paused = state_paths(config)
    if paused.exists():
        return {'status': 'paused', 'reason': paused.read_text(encoding='utf-8').strip()}
    with lock_workspace(config) as lock_fd:
        if lock_fd is None:
            return {'status': 'busy', 'message': '上一轮仍在运行，本轮跳过。'}
        binding = read_json(config['project_config'])
        state = load_state(state_path, identity(config, binding))
        save = lambda: atomic_json(state_path, state)
        if state.get('inflight'):
            paused.write_text('发现未收尾的运行。检查 runs 目录后使用 recover 重试。\n', encoding='utf-8')
            return {'status': 'recovery_required', 'run_id': state['inflight']['run_id']}
        try:
            project_governance(config, binding)
            snapshot = collect_snapshot(config, binding, directory, collector)
            snapshot_id = digest(stable_snapshot(snapshot))
            timestamp = clock()
            state['last_scan_at'] = now()
            state['last_observed_snapshot'] = snapshot_id
            run, reason = should_run(state, snapshot_id, timestamp, config['reconcile_interval_seconds'])
            if not run:
                state['idle_wakes'] = 0
                state.pop('last_error', None)
                save()
                return {'status': 'unchanged', 'message': '没有变化，本轮不启动 Codex。'}
            if paused.exists():
                return {'status': 'paused'}
            if state.get('idle_wakes', 0) >= config['max_consecutive_runs'] and reason == 'changed':
                paused.write_text('多轮没有可推进事项却持续出现变化。请检查重复回执，待处理快照已保留。\n', encoding='utf-8')
                save()
                return {'status': 'recovery_required', 'message': '已暂停连续空转，未跳过变化。'}
            run_id = str(uuid.uuid4())
            run_dir = directory / 'runs' / run_id
            run_dir.mkdir(parents=True, mode=0o700)
            atomic_json(run_dir / 'snapshot.json', snapshot)
            prompt = build_prompt(config, state, run_id, snapshot_id, run_dir / 'snapshot.json',
                                  reason, binding)
            state['inflight'] = {'run_id': run_id, 'snapshot_id': snapshot_id,
                                 'started_at': now(), 'directory': str(run_dir), 'phase': 'starting'}
            save()
            result, session_id = executor(config, state, run_dir, prompt, lock_fd, save)
            validate_result(result, run_id, snapshot_id)
            # Only the START snapshot is delivered. Changes made during execution remain visible.
            state.update(checkpoint=snapshot_id, session_id=session_id,
                         last_result=result, last_success_at=now(), last_success_epoch=clock())
            delay = result['retry_after_seconds']
            if result['outcome'] == 'continue':
                delay = max(delay, config['interval_minutes'] * 60)
            state['next_due_at'] = clock() + delay if delay else None
            state['idle_wakes'] = (state.get('idle_wakes', 0) + 1
                                   if reason == 'changed' and result['outcome'] in ('idle', 'waiting') else 0)
            state.pop('inflight', None)
            state.pop('last_error', None)
            state.pop('recovery_note', None)
            save()
            if result['outcome'] == 'paused':
                paused.write_text(result['summary'] + '\n', encoding='utf-8')
            return {'status': 'ran', 'run_id': run_id, 'outcome': result['outcome'], 'summary': result['summary']}
        except (RunnerError, OnboardingError, OSError, ValueError, TypeError, KeyError) as exc:
            state['last_error'] = {'at': now(), 'message': str(exc)}
            if state.get('inflight'):
                paused.write_text('本轮执行结果不完整，请核对后使用 recover。\n', encoding='utf-8')
            save()
            return {'status': 'failed', 'message': str(exc), 'checkpoint_advanced': False}


def control_state(config_path, action):
    config = control.load_config(config_path)
    directory, state_path, paused = state_paths(config)
    if action == 'status':
        return {'status': 'paused' if paused.exists() else 'enabled',
                'pause_reason': paused.read_text(encoding='utf-8').strip() if paused.exists() else None,
                'state': read_json(state_path) if state_path.exists() else None,
                'note': 'enabled 仅表示本地未暂停，不证明已安装 cron 或业务全部完成。'}
    if action == 'pause':
        paused.write_text('用户暂停后续运行；正在执行的轮次不会被强行终止。\n', encoding='utf-8')
        return {'status': 'paused', 'active_run_terminated': False}
    with lock_workspace(config) as lock_fd:
        if lock_fd is None:
            raise RunnerError('上一轮仍在运行，不能恢复或清理执行状态。')
        state = read_json(state_path) if state_path.exists() else None
        if action == 'resume' and state and state.get('inflight'):
            raise RunnerError('存在未收尾的运行。核对其日志与实际写入后使用 recover。')
        if action == 'resume' and state and state.get('last_result', {}).get('outcome') == 'paused':
            state['next_due_at'] = 0
            atomic_json(state_path, state)
        if action == 'recover' and state:
            inflight = state.get('inflight')
            if inflight:
                session_id, _, _ = read_events(Path(inflight['directory']) / 'events.jsonl', state.get('session_id'))
                if session_id:
                    state['session_id'] = session_id
            state.setdefault('recoveries', []).append({'at': now(), 'inflight': state.pop('inflight', None)})
            state['idle_wakes'] = 0
            state['next_due_at'] = 0
            # Preserve checkpoint and session; retry asks Codex to reconcile existing effects first.
            state['recovery_note'] = '上一轮结果未知，先检查已经生效的操作，再补未完成部分。'
            atomic_json(state_path, state)
        if paused.exists():
            paused.unlink()
        return {'status': 'enabled', 'message': '后续轮次可继续，未立即启动 Codex。'}


def parser():
    result = argparse.ArgumentParser(description='Linux 定时检查飞书项目并运行 Codex。')
    sub = result.add_subparsers(dest='command', required=True)
    for name in ['init', 'check', 'scan', 'tick', 'status', 'pause', 'resume', 'recover', 'cron-preview', 'install-cron', 'remove-cron']:
        child = sub.add_parser(name)
        child.add_argument('--config', required=True, type=Path)
        if name == 'init':
            child.add_argument('--project-config', required=True, type=Path)
            child.add_argument('--workdir', required=True, type=Path)
            child.add_argument('--lark-profile', required=True)
            child.add_argument('--codex-profile')
            child.add_argument('--interval-minutes', type=int, default=5)
    return result


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.command == 'init':
            output = control.init_config(args.config, args.project_config, args.workdir,
                                         args.lark_profile, args.interval_minutes, args.codex_profile)
        elif args.command == 'check':
            config = control.load_config(args.config)
            binding = read_json(config['project_config'])
            project_governance(config, binding)
            watch._binding(binding)
            output = {'status': 'local_config_valid', 'linux': sys.platform.startswith('linux'),
                      'coverage': watch.scan_coverage(binding),
                      'remote_access_verified': False, 'cron_installed_verified': False}
        elif args.command in ['tick', 'scan']:
            if not sys.platform.startswith('linux'):
                raise RunnerError('首版定时执行仅支持 Linux。')
            output = tick(args.config) if args.command == 'tick' else scan_only(args.config)
        elif args.command in ['status', 'pause', 'resume', 'recover']:
            output = control_state(args.config, args.command)
        elif args.command == 'cron-preview':
            print(control.cron_preview(args.config))
            return 0
        elif args.command == 'install-cron':
            output = control.install_cron(args.config)
        else:
            output = control.remove_cron(args.config)
        print(json.dumps(output, ensure_ascii=False))
        return 1 if output.get('status') in ('failed', 'recovery_required') else 0
    except (RunnerError, OnboardingError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'failed', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
