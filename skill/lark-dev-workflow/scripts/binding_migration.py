"""Explicit, paused migration of one project's local binding and runner state.

The complete read verifies the candidate against Feishu; it does not approve
business work. Old checkpoints, comments and run artifacts remain evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


def file_hash(data):
    return hashlib.sha256(data).hexdigest()


def _replace_bytes(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def migrate(config_path, candidate_path, expected_binding_sha256,
            expected_candidate_sha256, handoff_path, *, runtime, collector):
    """Validate, archive and install a candidate; leave the runner paused.

Only schemas of the same project may change. Existing table/field identities
cannot be remapped or removed. A failed or interrupted two-file commit remains
paused and has before/after files for explicit recovery; no rollback is guessed.
"""
    error = runtime.RunnerError
    config_path, candidate_path = Path(config_path).resolve(), Path(candidate_path).resolve()
    config_bytes = config_path.read_bytes()
    config = runtime.control.load_config(config_path)
    directory, state_path, paused = runtime.state_paths(config)
    binding_path = Path(config['project_config'])
    if candidate_path in (config_path, binding_path, state_path):
        raise error('候选绑定必须是独立文件，不能覆盖运行配置或现有绑定。')
    with runtime.lock_workspace(config) as lock_fd:
        if lock_fd is None:
            raise error('上一轮仍在运行；迁移没有开始。')
        if not paused.is_file():
            raise error('迁移前先暂停后续运行；本命令不会自动暂停或恢复。')
        paused_bytes = paused.read_bytes()
        old_bytes, candidate_bytes = binding_path.read_bytes(), candidate_path.read_bytes()
        if file_hash(old_bytes) != expected_binding_sha256:
            raise error('现有绑定与提供的 SHA256 不一致；没有改动。')
        if file_hash(candidate_bytes) != expected_candidate_sha256:
            raise error('候选绑定与提供的 SHA256 不一致；没有改动。')
        old, candidate = json.loads(old_bytes), json.loads(candidate_bytes)
        runtime.watch._binding(candidate)
        runtime.project_governance(config, candidate)
        if (runtime.comments.binding_identity(old) != runtime.comments.binding_identity(candidate)
                or old.get('project_key') != candidate.get('project_key')
                or old.get('project_number') != candidate.get('project_number')
                or runtime.watch.feedback_intake_mode(old) != runtime.watch.feedback_intake_mode(candidate)
                or set(old.get('tables', {})) != set(candidate['tables'])):
            raise error('迁移只能更新同一项目的字段，不能更换 Base、项目、文档或扫描范围。')
        for role, table in old['tables'].items():
            new = candidate['tables'][role]
            if (table['id'] != new['id'] or any(
                    field['id'] != new['fields'].get(key, {}).get('id')
                    or field.get('target_table_id') != new['fields'].get(key, {}).get('target_table_id')
                    for key, field in table['fields'].items())):
                raise error('迁移不能移除或换绑已有表和字段；请先单独核对这些变化。')
        if old == candidate:
            raise error('候选绑定没有变化，不需要迁移。')
        if not state_path.is_file():
            raise error('没有已有运行状态；请按首次接入流程处理。')
        state_bytes = state_path.read_bytes()
        state = runtime.load_state(state_path, runtime.identity(config, old))
        if state.get('inflight') or state.get('recovery_note'):
            raise error('上一轮仍有未核实的操作；先核对实际结果，再迁移。')
        handoff = None
        handoff_source = None
        if state.get('session_id'):
            result = state.get('last_result') or {}
            run_id = result.get('run_id')
            if (not handoff_path or not isinstance(run_id, str)
                    or Path(run_id).name != run_id
                    or result.get('outcome') not in ('continue', 'waiting', 'idle', 'paused')):
                raise error('旧会话缺少可核对的交接记录，不能清除旧会话继续运行。')
            handoff_source = Path(handoff_path).resolve()
            expected_path = (directory / 'runs' / run_id / 'handoff.json').resolve()
            if handoff_source != expected_path:
                raise error('交接文件必须来自最后一轮运行目录中的 handoff.json。')
            handoff = runtime.run_context.validate_handoff(runtime.read_json(handoff_source), result)
            if not handoff['can_start_fresh'] or handoff['active_operations']:
                raise error('交接记录仍有进行中的操作，不能迁移并更换会话。')
        migration_id = 'migration-' + uuid.uuid4().hex
        archive = directory / 'binding-migrations' / migration_id
        archive.mkdir(parents=True, mode=0o700)
        for name, data in [('binding-before.json', old_bytes), ('binding-after.json', candidate_bytes),
                           ('state-before.json', state_bytes), ('runner-config.json', config_bytes),
                           ('pause-before.txt', paused_bytes)]:
            _replace_bytes(archive / name, data)
        for name in ('comments-state.json', 'browser-comments-state.json', 'checkpoint-snapshot.json'):
            source = directory / name
            if source.is_file():
                _replace_bytes(archive / name, source.read_bytes())
        if handoff is not None:
            runtime.atomic_json(archive / 'handoff.json', handoff)
        journal = {'format_version': 1, 'migration_id': migration_id, 'status': 'validating',
                   'at': runtime.now(), 'binding_path': str(binding_path),
                   'before_sha256': expected_binding_sha256,
                   'after_sha256': expected_candidate_sha256,
                   'old_identity': state['identity'], 'new_identity': runtime.identity(config, candidate),
                   'handoff_source': str(handoff_source) if handoff_source else None,
                   'checkpoint_advanced': False, 'business_work_approved': False}
        journal_path = archive / 'migration.json'
        runtime.atomic_json(journal_path, journal)
        try:
            validation = archive / 'validation'
            validation.mkdir(mode=0o700)
            for name in ('comments-state.json', 'browser-comments-state.json'):
                if (archive / name).is_file():
                    shutil.copyfile(archive / name, validation / name)
                    os.chmod(validation / name, 0o600)
            # A separate journal prevents this read from consuming observations
            # in the active runtime. Candidate field types are checked live.
            snapshot = runtime.collect_snapshot(config, candidate, validation, collector)
            runtime.atomic_json(archive / 'validation-snapshot.json', snapshot)
            for table in candidate['tables'].values():
                for field in table['fields'].values():
                    if field.get('type') != 'stage':
                        continue
                    observed = snapshot.get('stage_fields', {}).get(f"{table['id']}/{field['id']}")
                    if (not isinstance(field.get('stage_config'), dict) or not observed
                            or observed.get('stage_config') != field['stage_config']
                            or observed.get('options') != field.get('options')):
                        raise error('线上流程配置与候选绑定不一致；先重新核对节点和完成条件。')
            if (config_path.read_bytes() != config_bytes or binding_path.read_bytes() != old_bytes
                    or state_path.read_bytes() != state_bytes
                    or candidate_path.read_bytes() != candidate_bytes
                    or not paused.is_file() or paused.read_bytes() != paused_bytes):
                raise error('验证期间配置、暂停标记或运行状态发生变化；没有安装候选绑定。')
            updated = copy.deepcopy(state)
            updated['identity'] = journal['new_identity']
            updated['next_due_at'] = 0
            updated['idle_wakes'] = 0
            updated['session_runs'] = 0
            if state.get('session_id'):
                updated.setdefault('session_history', []).append({
                    'session_id': state['session_id'], 'last_run_id': state['last_result']['run_id'],
                    'handoff_file': str(handoff_source), 'migration_id': migration_id})
                updated['session_handoff'] = {'previous_session_id': state['session_id'],
                    'source': str(handoff_source), 'content': copy.deepcopy(handoff)}
            updated['session_id'] = None
            updated.setdefault('binding_migrations', []).append({
                'id': migration_id, 'evidence': str(journal_path), 'at': journal['at']})
            runtime.atomic_json(archive / 'state-after.json', updated)
            journal['status'] = 'prepared'
            runtime.atomic_json(journal_path, journal)
            _replace_bytes(binding_path, candidate_bytes)
            runtime.atomic_json(state_path, updated)
            if (binding_path.read_bytes() != candidate_bytes or runtime.read_json(state_path) != updated):
                raise error('迁移写入后的核对不一致；保持暂停，请按备份核对。')
            journal['status'] = 'complete'
            runtime.atomic_json(journal_path, journal)
        except Exception as exc:
            journal['status'] = 'failed'
            journal['failure_type'] = type(exc).__name__
            if isinstance(exc, error):
                journal['failure_message'] = str(exc)
            # A non-cooperating editor may remove PAUSED while validation is in
            # progress. Keep its original evidence and restore a migration pause
            # without overwriting any newer pause instruction.
            try:
                journal['pause_present_after_failure'] = paused.is_file()
                if paused.is_file():
                    _replace_bytes(archive / 'pause-after-failure.txt', paused.read_bytes())
                else:
                    descriptor = os.open(paused, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(('迁移未完成：' + migration_id + '。核对备份后再恢复运行。\n').encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                pause_verified = paused.is_file()
            except OSError:
                pause_verified = paused.is_file()
            journal['pause_verified'] = pause_verified
            try:
                runtime.atomic_json(journal_path, journal)
            except OSError:
                pass
            message = '迁移未完成，保持暂停。' if pause_verified else '迁移未完成，无法确认暂停标记；请先停止调度并核对。'
            raise error(message + '旧绑定、运行状态和核对记录保存在：' + str(archive)) from exc
        return {'status': 'migrated_paused', 'migration_id': migration_id, 'backup_path': str(archive),
                'checkpoint_advanced': False, 'codex_started': False,
                'message': '已更新绑定并保留旧记录；仍暂停，核对部署结果后再恢复。'}
