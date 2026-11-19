#!/usr/bin/env python3
"""Bounded, cron-driven Codex worker. Scheduling checkpoints are not business acknowledgements."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
import run_context
import work_packet
import own_effects
import gate_events
import registered_requests

HERE = Path(__file__).resolve().parent
# These are workflow limits for the existing worker, not operating-system permissions.
PREPARE_SCOPE = {
    'native_stage_mode': 'prepare',
    'allowed_work': ['feedback_clarification', 'requirements_or_bugs_for_confirmation',
                     'work_plans_after_verified_scope'],
    'native_stage_writes': False,
    'implementation_or_dispatch': False,
}
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
    control.actor_for_runner(config, binding)
    value = {'binding': comments.binding_identity(binding),
             'tables': binding.get('tables'), 'project_number': binding.get('project_number'),
             'workdir': config['workdir'], 'codex_profile': config.get('codex_profile')}
    if config['format_version'] != 2:
        # Keep existing CLI identities byte-for-byte compatible with saved state.
        value['lark_profile'] = config['lark_profile']
    else:
        value['access'] = config['access']
    if config.get('native_stage_mode') == 'prepare':
        value.update(native_stage_mode='prepare', event_tenant_key=config['event_tenant_key'])
    if config.get('native_stage_mode') == registered_requests.MODE:
        value.update(native_stage_mode=registered_requests.MODE, event_tenant_key=config['event_tenant_key'],
                     stage_service_config=config['stage_service_config'])
    return digest(value)


def access_context(config, binding):
    actor = control.actor_for_runner(config, binding)
    if config['format_version'] != 2:
        return {'mode': 'cli', 'lark_bin': config['lark_bin'], 'lark_profile': config['lark_profile'],
                **({k: actor[k] for k in ('identity', 'app_id', 'open_id')} if actor else {})}
    return dict(config['access'])


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


def execution_directory(config):
    root = Path(config['state_dir'])
    return root / 'native-stage-prepare' if config.get('native_stage_mode') == 'prepare' else root


def state_paths(config):
    root = Path(config['state_dir'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = execution_directory(config)
    if directory != root:
        directory.mkdir(exist_ok=True, mode=0o700)
    return directory, directory / 'state.json', root / 'PAUSED'


def other_mode_state(config, binding):
    root = Path(config['state_dir'])
    other = root if config.get('native_stage_mode') == 'prepare' else root / 'native-stage-prepare'
    path = other / 'state.json'
    if path.exists():
        state = read_json(path)
        if not isinstance(state, dict):
            raise RunnerError('另一种运行方式的记录无法核实。')
        if state.get('inflight') or state.get('recovery_note'):
            raise RunnerError('另一种运行方式仍有未核实的写入。请用原方式核对并收尾，再切换。')
        other_config = dict(config)
        if other == root:
            other_config.pop('native_stage_mode', None)
        elif config['format_version'] == 3 and config.get('event_tenant_key'):
            other_config['native_stage_mode'] = 'prepare'
            other_config.pop('stage_service_config', None)
        else:
            return None
        if state.get('format_version') == 1 and state.get('identity') == identity(other_config, binding):
            return path
    return None


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
    control.actor_for_runner(config, binding)
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


def build_prompt(config, state, run_id, snapshot_id, snapshot_file, reason, expected_binding=None, *, policy=None):
    instructions = (HERE.parent / 'references/unattended-worker.md').read_text(encoding='utf-8')
    binding = read_json(config['project_config'])
    if expected_binding is not None and (
            identity(config, binding) != identity(config, expected_binding)
            or watch.feedback_intake_mode(binding) != watch.feedback_intake_mode(expected_binding)):
        raise RunnerError('本轮采集期间项目绑定已改变；请核对配置与快照，不会执行旧项目快照。')
    governance, rules = project_governance(config, binding)
    prepared_at = datetime.now(timezone.utc)
    # Prompt preparation precedes process startup: this is a conservative finish target,
    # not a claim about the child's exact start time. Recalculate it for every invocation.
    run_budget = {
        'timeout_seconds': config['run_timeout_seconds'],
        'prepared_at': prepared_at.isoformat(),
        'finish_before': (prepared_at + timedelta(seconds=config['run_timeout_seconds'])).isoformat(),
    }
    context = {
        'governance': governance, 'project_rules': rules,
        'run_id': run_id, 'snapshot_id': snapshot_id, 'reason': reason,
        'run_budget': run_budget,
        'skill_file': str(HERE.parent / 'SKILL.md'), 'project_config': config['project_config'],
        'access': access_context(config, binding), 'snapshot_file': str(snapshot_file),
        'work_packet_file': (str(Path(snapshot_file).with_name('work-packet.json'))
                             if Path(snapshot_file).with_name('work-packet.json').exists() else None),
        'previous_snapshot_file': (str(Path(snapshot_file).with_name('previous-snapshot.json'))
                                   if Path(snapshot_file).with_name('previous-snapshot.json').exists() else None),
        'comments_journal_file': str(execution_directory(config) / (
            'browser-comments-state.json' if config['format_version'] == 2 else 'comments-state.json')),
        'optional_effects_file': str(Path(snapshot_file).with_name('effects.json')),
        'session_policy': {
            'max_successful_runs': config.get('session_max_runs', 4),
            'handoff_due': run_context.session_runs(state, config.get('session_max_runs', 4)) + 1
                           >= config.get('session_max_runs', 4),
            'handoff_file': str(Path(snapshot_file).with_name('handoff.json')),
        },
        'session_handoff': state.get('session_handoff'),
        'previous_result': state.get('last_result'),
        'recovery_note': state.get('recovery_note'),
        'previous_interrupted_run': (state.get('recoveries') or [{}])[-1].get('inflight'),
    }
    if config.get('native_stage_mode') == 'prepare':
        context['execution_scope'] = PREPARE_SCOPE
    if config.get('native_stage_mode') == registered_requests.MODE:
        if policy is None or registered_requests.selected(config, binding) != policy:
            raise RunnerError('采集后已登记的助手规则发生变化，请重新读取项目。')
        context['execution_scope'] = policy
        context['stage_action'] = str(registered_requests.SERVICE / 'stage_action.py')
        context['stage_service_config'] = config['stage_service_config']
    if reason == 'mode_changed':
        prior = other_mode_state(config, binding)
        context['previous_mode_state_file'] = str(prior) if prior else None
    if state.get('actor_handoff') and not state.get('checkpoint'):
        context['actor_handoff'] = state['actor_handoff']
    if config['format_version'] != 2:
        context['lark_profile'] = config['lark_profile']
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
        state['inflight']['attempt_session_id'] = session_id
        save_state()
    if interrupted_run:
        raise RunnerError('本轮已中断或超时；可能有部分写入，需核对后恢复。')
    if child.returncode or failed:
        raise RunnerError('Codex 执行失败；已保留会话编号、日志和未处理快照。')
    if not completed or not session_id:
        raise RunnerError('缺少 Codex 本轮完成事件或会话编号。')
    return read_json(run_dir / 'result.json'), session_id


def collect_snapshot(config, binding, directory, collector, *, inspect_native_stages=False, prepare=False,
                     registered_policy=None):
    control.actor_for_runner(config, binding)
    # Use the selected executable itself and retain PATH for its runtime (for example Node).
    old_path = os.environ.get('PATH')
    os.environ['PATH'] = config['path']
    try:
        if config['format_version'] == 2:
            from browser_watch import collect as browser_collect
            return browser_collect(binding, config['access'],
                                   comments_state_path=directory / 'browser-comments-state.json')
        inspect = inspect_native_stages or prepare or registered_policy is not None
        options = {'inspect_native_stages': True} if inspect else {}
        snapshot = collector(binding, config['lark_profile'], comments_state_path=directory / 'comments-state.json',
                             lark_bin=config['lark_bin'], **options)
        partial = snapshot.get('coverage', {}).get('native_stage_inspection') is not None
        if partial != inspect or bool(snapshot.get('native_stage_inspection')) != inspect:
            raise RunnerError('只读流程检查与完整执行器扫描不能混用。')
        if prepare:
            if (config.get('native_stage_mode') != 'prepare'
                    or snapshot['coverage']['native_stage_inspection'] !=
                    {'node_settings_verified': False, 'dispatch_verified': False}):
                raise RunnerError('反馈与计划模式未核实原生流程类型和可见状态。')
            snapshot['coverage']['execution_scope'] = PREPARE_SCOPE
        if registered_policy is not None:
            expected = {t['id'] + '/' + t['fields']['status']['id']
                        for role, t in binding['tables'].items() if role in ('requirements', 'bugs', 'work')}
            if (config.get('native_stage_mode') != registered_requests.MODE or len(expected) != 3
                    or set(snapshot['native_stage_inspection']) != expected
                    or snapshot['coverage']['native_stage_inspection'] !=
                    {'node_settings_verified': False, 'dispatch_verified': False}):
                raise RunnerError('无法核实三张事项表的原生状态字段，本轮未取得办理动作的依据。')
            snapshot['coverage']['execution_scope'] = registered_policy
        return snapshot
    finally:
        if old_path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = old_path


def scan_only(config_path, *, collector=watch.collect, inspect_native_stages=False):
    config = control.load_config(config_path)
    directory, _, _ = state_paths(config)
    with lock_workspace(config) as lock_fd:
        if lock_fd is None:
            return {'status': 'busy', 'message': '上一轮仍在运行，本轮跳过。'}
        binding = read_json(config['project_config'])
        root = Path(config['state_dir'])
        prepare = config.get('native_stage_mode') == 'prepare' and not inspect_native_stages
        registered = config.get('native_stage_mode') == registered_requests.MODE and not inspect_native_stages
        policy = registered_requests.selected(config, binding, runner_path=config_path) if registered else None
        output_dir = directory
        if inspect_native_stages:
            if config['format_version'] != 3 or not control.actor_for_runner(config, binding):
                raise RunnerError('原生流程只读检查只适用于专用助手执行器。')
            output_dir = root / 'native-stage-inspection'
            output_dir.mkdir(mode=0o700, exist_ok=True)
        scan_started = int(time.time())
        snapshot = collect_snapshot(config, binding, output_dir, collector,
                                    inspect_native_stages=inspect_native_stages, prepare=prepare, registered_policy=policy)
        if (inspect_native_stages or prepare or registered) and (control.load_config(config_path) != config
                                                   or read_json(config['project_config']) != binding):
            raise RunnerError('检查期间项目绑定已变更。请使用当前配置重新检查。')
        if policy is not None and registered_requests.selected(config, binding, runner_path=config_path) != policy:
            raise RunnerError('采集期间助手规则已变，请重新检查。')
        atomic_json(output_dir / 'last-scan.json', snapshot)
        scope = gate_events.publish_scope(config, binding, snapshot, root, started_at=scan_started)
        status = 'scanned'
        details = {}
        if prepare:
            status = 'scanned_for_preparation'
            details = {'snapshot_file': str(output_dir / 'last-scan.json')}
        if registered:
            status = 'scanned_for_registered_requests'
            details = {'snapshot_file': str(output_dir / 'last-scan.json')}
        if inspect_native_stages:
            status = 'inspected'
            details = {'message': '已核对记录、评论和流程状态名称。本次未核实线上节点的完成人及必填规则。',
                       'snapshot_file': str(output_dir / 'last-scan.json'),
                       'inspected_stage_fields': len(snapshot['native_stage_inspection']),
                       'event_scope_registered': bool(scope and scope['tables'])}
        return {'status': status, **details, 'records': len(snapshot.get('records', {})),
                'comments': len(snapshot.get('comments', {})), 'notices': len(snapshot.get('notices', {})),
                'coverage': snapshot.get('coverage', {}),
                'codex_started': False, 'checkpoint_advanced': False}


def matches_own_effects(directory, state, baseline, snapshot_id):
    """Only a complete subsequent scan matching the exact predicted changes can skip a wake."""
    result = state.get('last_result') or {}
    run_id = result.get('run_id')
    if (baseline is None or not isinstance(run_id, str) or Path(run_id).name != run_id
            or state.get('effects_consumed_run') == run_id):
        return False


    path = Path(directory) / 'runs' / run_id / 'effects.json'
    if not path.exists():
        return False
    try:
        expected = own_effects.expected_snapshot(baseline, read_json(path), run_id,
                                                result.get('snapshot_id'))
        return digest(stable_snapshot(expected)) == snapshot_id
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # An optimization artifact cannot block business or consume unverified input.
        state['optimization_note'] = f'Own-write shortcut ignored: {type(exc).__name__}.'
        return False


def optional_json(path, value, state):
    """Optimization output is expendable; the run's source snapshot/state are not."""
    try:
        atomic_json(path, value)
        return True
    except (OSError, ValueError, TypeError):
        state['optimization_note'] = f'Optional {Path(path).name} unavailable; use original inputs.'
        return False


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
            root = Path(config['state_dir'])
            prepare = config.get('native_stage_mode') == 'prepare'
            prior_mode_state = other_mode_state(config, binding)
            mode = config.get('native_stage_mode', 'strict')
            policy = (registered_requests.selected(config, binding, runner_path=config_path)
                      if mode == registered_requests.MODE else None)
            mode_file = root / 'last-execution-mode.json'
            previous_mode = read_json(mode_file) if mode_file.exists() else 'strict'
            if previous_mode not in ('strict', 'prepare', registered_requests.MODE):
                raise RunnerError('上次运行方式无法核实。请核对原记录。')
            scan_started = int(time.time())
            snapshot = collect_snapshot(config, binding, directory, collector, prepare=prepare, registered_policy=policy)
            current_config = control.load_config(config_path)
            current_binding = read_json(current_config['project_config'])
            if (identity(current_config, current_binding) != identity(config, binding)
                    or current_config.get('event_tenant_key') != config.get('event_tenant_key')
                    or ((prepare or policy) and (current_config != config or current_binding != binding))
                    or (policy is not None and registered_requests.selected(config, binding, runner_path=config_path) != policy)):
                raise RunnerError('本轮采集期间登录方式、账号或运行绑定已改变；不会继续使用旧快照。')
            snapshot_id = digest(stable_snapshot(snapshot))
            timestamp = clock()
            state['last_scan_at'] = now()
            state['last_observed_snapshot'] = snapshot_id
            baseline = run_context.checkpoint_snapshot(directory, state)
            scope = gate_events.publish_scope(config, binding, snapshot, root, started_at=scan_started)
            gate_rows, gate_checkpoint = gate_events.pending(root, scope, state)
            run, reason = should_run(state, snapshot_id, timestamp, config['reconcile_interval_seconds'])
            own_only = (reason == 'changed' and matches_own_effects(directory, state, baseline, snapshot_id))
            if own_only:
                # Test due work against the predicted checkpoint; do not consume a whole END scan.
                due_state = dict(state, checkpoint=snapshot_id)
                run, reason = should_run(due_state, snapshot_id, timestamp,
                                         config['reconcile_interval_seconds'])
            if previous_mode != mode and (prior_mode_state or mode_file.exists()):
                run, reason, baseline = True, 'mode_changed', None
            if gate_rows and not run:
                run, reason = True, 'gate_event_evidence'
            if (not run and policy is not None and previous_mode == mode
                    and (state.get('last_result') or {}).get('outcome') == 'waiting'
                    and state.get('registered_start_check_revision', 1) < registered_requests.START_CHECK_REVISION):
                run, reason = True, 'registered_start_check_updated'
            if not run:
                state['idle_wakes'] = 0
                state.pop('last_error', None)
                if own_only:
                    state['checkpoint'] = snapshot_id
                    state['effects_consumed_run'] = state['last_result']['run_id']
                    optional_json(directory / 'checkpoint-snapshot.json', snapshot, state)
                save()
                return {'status': 'unchanged', 'own_writes_verified': own_only,
                        'message': '仅有已核实的自身写入，本轮不启动 Codex。' if own_only
                                   else '没有变化，本轮不启动 Codex。'}
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
            packet_baseline = baseline
            if baseline is not None and not optional_json(run_dir / 'previous-snapshot.json', baseline, state):
                packet_baseline = None
            packet = work_packet.build_work_packet(snapshot, packet_baseline, binding)
            if prepare:
                packet['execution_scope'] = PREPARE_SCOPE
            if policy is not None:
                packet['execution_scope'] = policy
                clicks = registered_requests.retain_clicks(config, binding, snapshot, gate_rows)
                if clicks:
                    packet['confirmation_checks'] = clicks
            if gate_rows:
                # The active worker owns this file until it finishes. Archived packets
                # keep its path and run ID, never an unbounded copy of observed events.
                evidence_path = directory / gate_events.WORK
                atomic_json(evidence_path, {**gate_events.evidence_pins(binding, config['event_tenant_key']),
                                            'run_id': run_id, 'items': gate_rows})
                packet['gate_event_observations_file'] = str(evidence_path)
                packet['gate_event_run_id'] = run_id
                atomic_json(run_dir / 'work-packet.json', packet)
                packet_written = True
            else:
                packet_written = optional_json(run_dir / 'work-packet.json', packet, state)
            previous_session = state.get('session_id')
            session_plan = run_context.prepare_session(state, directory, config['session_max_runs'])
            state['session_runs'] = run_context.session_runs(state, config['session_max_runs'])
            prompt = build_prompt(config, state, run_id, snapshot_id, run_dir / 'snapshot.json',
                                  reason, binding, **({'policy': policy} if policy is not None else {}))
            state['inflight'] = {'run_id': run_id, 'snapshot_id': snapshot_id,
                                 'started_at': now(), 'directory': str(run_dir), 'phase': 'starting',
                                 'session_mode': session_plan['mode'],
                                 'previous_session_id': previous_session,
                                 'attempt_session_id': state.get('session_id')}
            optional_json(run_dir / 'input-stats.json', {
                'snapshot_bytes': (run_dir / 'snapshot.json').stat().st_size,
                'work_packet_bytes': (len((json.dumps(packet, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
                                      if packet_written else None),
                'prompt_bytes': len(prompt.encode('utf-8')),
                'session_mode': session_plan['mode'],
                'note': 'File sizes, not model token counts or a billing estimate.',
            }, state)
            save()
            result, session_id = executor(config, state, run_dir, prompt, lock_fd, save)
            validate_result(result, run_id, snapshot_id)
            if previous_mode != mode:
                atomic_json(mode_file, mode)
            if gate_checkpoint:
                state['gate_event_checkpoint'] = gate_checkpoint
            # Only the START snapshot is delivered. Changes made during execution remain visible.
            state.update(checkpoint=snapshot_id, session_id=session_id,
                         last_result=result, last_success_at=now(), last_success_epoch=clock())
            if policy is not None:
                state['registered_start_check_revision'] = registered_requests.START_CHECK_REVISION
            state['session_runs'] = run_context.session_runs(state, config['session_max_runs']) + 1
            state.pop('session_handoff', None)
            optional_json(directory / 'checkpoint-snapshot.json', snapshot, state)
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
        other_mode_state(config, read_json(config['project_config']))
        state = read_json(state_path) if state_path.exists() else None
        if action == 'resume' and state and state.get('inflight'):
            raise RunnerError('存在未收尾的运行。核对其日志与实际写入后使用 recover。')
        if action == 'resume' and state and state.get('last_result', {}).get('outcome') == 'paused':
            state['next_due_at'] = 0
            atomic_json(state_path, state)
        if action == 'recover' and state:
            inflight = state.get('inflight')
            if inflight:
                expected_session = inflight.get('attempt_session_id', state.get('session_id'))
                session_id, _, _ = read_events(Path(inflight['directory']) / 'events.jsonl', expected_session)
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


def migrate_binding(config_path, candidate, expected_binding_sha256,
                    expected_candidate_sha256, handoff=None, *, collector=watch.collect):
    import binding_migration
    return binding_migration.migrate(config_path, candidate, expected_binding_sha256,
                                     expected_candidate_sha256, handoff,
                                     runtime=sys.modules[__name__], collector=collector)


def parser():
    result = argparse.ArgumentParser(description='Linux 定时检查飞书项目并运行 Codex。')
    sub = result.add_subparsers(dest='command', required=True)
    for name in ['init', 'check', 'scan', 'tick', 'status', 'pause', 'resume', 'recover', 'migrate-binding', 'cron-preview', 'install-cron', 'remove-cron']:
        child = sub.add_parser(name)
        child.add_argument('--config', required=True, type=Path)
        if name == 'scan':
            child.add_argument('--inspect-native-stages', action='store_true',
                               help='专用助手只读核对原生流程类型和状态名称，单独保存检查结果。')
        if name == 'migrate-binding':
            child.add_argument('--candidate', required=True, type=Path)
            child.add_argument('--expected-binding-sha256', required=True)
            child.add_argument('--expected-candidate-sha256', required=True)
            child.add_argument('--handoff', type=Path)
        if name == 'init':
            child.add_argument('--project-config', required=True, type=Path)
            child.add_argument('--workdir', required=True, type=Path)
            child.add_argument('--access', choices=('cli', 'browser', 'bot'), default='cli')
            child.add_argument('--lark-profile')
            child.add_argument('--event-tenant-key', help='Paired tenant for scoped event evidence (bot only).')
            child.add_argument('--stage-service-config', type=Path, help='本项目长连接服务的配置文件。')
            child.add_argument('--native-stage-mode', choices=('prepare', registered_requests.MODE),
                               help='prepare 可准备反馈和计划。registered_requests 可按本项目已登记的六条规则办理事项。')
            child.add_argument('--browser-cdp-url')
            child.add_argument('--browser-user-id')
            child.add_argument('--node-bin')
            child.add_argument('--codex-profile')
            child.add_argument('--interval-minutes', type=int, default=5)
    return result


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.command == 'init':
            output = control.init_config(args.config, args.project_config, args.workdir,
                                         args.lark_profile, args.interval_minutes, args.codex_profile,
                                         access=args.access, browser_cdp_url=args.browser_cdp_url,
                                         browser_user_id=args.browser_user_id, node_bin=args.node_bin,
                                         event_tenant_key=args.event_tenant_key, native_stage_mode=args.native_stage_mode,
                                         stage_service_config=args.stage_service_config)
        elif args.command == 'check':
            config = control.load_config(args.config)
            binding = read_json(config['project_config'])
            project_governance(config, binding)
            watch._binding(binding)
            output = {'status': 'local_config_valid', 'linux': sys.platform.startswith('linux'),
                      'access': access_context(config, binding),
                      'coverage': watch.scan_coverage(binding),
                      'remote_access_verified': False, 'cron_installed_verified': False}
            if config.get('native_stage_mode') == 'prepare':
                output['execution_scope'] = PREPARE_SCOPE
            if config.get('native_stage_mode') == registered_requests.MODE:
                output['execution_scope'] = registered_requests.selected(config, binding, runner_path=args.config)
        elif args.command in ['tick', 'scan']:
            if not sys.platform.startswith('linux'):
                raise RunnerError('首版定时执行仅支持 Linux。')
            output = (tick(args.config) if args.command == 'tick' else
                      scan_only(args.config, inspect_native_stages=args.inspect_native_stages))
        elif args.command in ['status', 'pause', 'resume', 'recover']:
            output = control_state(args.config, args.command)
        elif args.command == 'migrate-binding':
            output = migrate_binding(args.config, args.candidate, args.expected_binding_sha256,
                                     args.expected_candidate_sha256, args.handoff)
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
