#!/usr/bin/env python3
"""Prepare a paused bot runner from the same project's successful personal runner.

Run unattended.py scan on the unscheduled bot candidate first. Preview with
--source OLD --candidate NEW; repeat with --apply <preview digest> to freeze both
runners and prepare the new state. No cron edits, Feishu calls or workers here.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'))
import unattended as runtime
from onboard import semantic_config


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_pair(source, candidate):
    old, new = (runtime.control.load_config(path) for path in (source, candidate))
    require(old['format_version'] in (1, 2) and new['format_version'] == 3,
            'Only a personal runner to a separately configured assistant is supported.')
    require(all(old.get(key) == new.get(key) for key in ('workdir', 'codex_profile', 'codex_bin')),
            'Work directory, Codex profile and executable must remain the same.')
    old_dir, new_dir = (Path(config['state_dir']) for config in (old, new))
    require(old_dir != new_dir and new_dir not in old_dir.parents and old_dir not in new_dir.parents
            and old['project_config'] != new['project_config'],
            'Use distinct bindings and separate, non-nested state directories.')
    return old, new


def preview(old, new, reviewed_at):
    source_dir, target_dir = (Path(config['state_dir']) for config in (old, new))
    require(not any(os.path.lexists(target_dir / name) for name in ('runs', 'checkpoint-snapshot.json')),
            'Candidate must be unscheduled and never have run a worker.')
    source_binding, target_binding = (runtime.read_json(config['project_config']) for config in (old, new))
    actor = runtime.control.actor_for_runner(new, target_binding)
    runtime.control.actor_for_runner(old, source_binding)
    require(semantic_config(source_binding) == semantic_config(
        {key: value for key, value in target_binding.items() if key != 'cli_actor'}),
        'Candidate must keep the original project, governance, documents, tables, scope and mapping.')
    old_rules = runtime.project_governance(old, source_binding)
    require(old_rules == runtime.project_governance(new, target_binding), 'Project rules differ.')
    require((source_dir / 'state.json').is_file(), 'Missing old execution state.')
    state = runtime.load_state(source_dir / 'state.json', runtime.identity(old, source_binding))
    require(not any(state.get(key) for key in ('inflight', 'last_error', 'recovery_note')),
            'Old operations are failed, in progress or not yet reconciled.')
    last = state.get('last_result') or {}
    require(last.get('outcome') in ('continue', 'waiting', 'idle') and state.get('checkpoint'),
            'Old runner has no successful, unpaused checkpoint.')
    run_id = str(uuid.UUID(last['run_id']))
    require(run_id == last['run_id'] and isinstance(state.get('session_id'), str)
            and str(uuid.UUID(state['session_id'])) == state['session_id'], 'Invalid original run or session ID.')
    run_dir = source_dir / 'runs' / run_id
    require(runtime.read_json(run_dir / 'result.json') == last, 'Last result does not match original run.')
    if (run_dir / 'handoff.json').is_file():
        note = runtime.run_context.validate_handoff(runtime.read_json(run_dir / 'handoff.json'), last)
        require(not note['active_operations'], 'Original handoff still lists unfinished external operations.')
    previous = runtime.run_context.checkpoint_snapshot(source_dir, state)
    require(previous is not None, 'Original checkpoint cannot be verified.')
    scan_file = target_dir / 'last-scan.json'
    require(0 <= reviewed_at - scan_file.stat().st_mtime <= 1800, 'Run a fresh candidate scan (within 30 minutes).')
    current = runtime.read_json(scan_file)
    require(current.get('binding') == runtime.comments.binding_identity(target_binding)
            and current.get('coverage') == previous.get('coverage') == runtime.watch.scan_coverage(target_binding),
            'Candidate scan does not identify the bound assistant and full configured project scope.')
    journal = runtime.comments.load_state(target_dir / 'comments-state.json', target_binding)
    require(journal.get('last_full_reconciliation'), 'No completed full comment scan under the new identity.')
    for field in ('records', 'comments'):
        require(isinstance(current.get(field), dict) and isinstance(previous.get(field), dict)
                and set(previous[field]).issubset(current[field]),
                'Previously visible ' + field + ' are missing. Reconcile visibility before handoff.')
    summary = {'session_id': state['session_id'], 'last_run_id': run_id, 'app_id': actor['app_id'],
               'visible_records': len(current['records']), 'visible_comments': len(current['comments']),
               'old_state_dir': str(source_dir), 'candidate_state_dir': str(target_dir)}
    digest = runtime.digest([old, new, source_binding, target_binding, old_rules, state,
                             runtime.digest(current), journal['last_full_reconciliation']])
    return summary, digest, state, target_binding


TARGET_PAUSE = '身份交接已预留；替换旧定时任务、核实权限后方可只恢复本候选。'
SOURCE_PAUSE = '身份交接冻结，保留此目录及原始证据；请勿同步运行两个身份。'


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_pause(path, text):
    # Reuse a complete private staging file if the process stopped before recording intent.
    if not os.path.lexists(path):
        with tempfile.NamedTemporaryFile(dir=path.parent) as stream:
            stream.write((text + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.link(stream.name, path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and path.read_text(encoding='utf-8') == text + '\n',
            'Staged pause differs; reconcile before retrying.')
    sync_directory(path.parent)
    return [info.st_dev, info.st_ino]


def move_pause(staged, paused):
    # Moving (rather than copying) consumes the private file. If both names go missing
    # later, an operator resumed the runner. The no-replace flag preserves concurrent stops.
    libc = ctypes.CDLL(None, use_errno=True)
    source, destination = os.fsencode(staged), os.fsencode(paused)
    if sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, source, -100, destination, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        result = libc.renamex_np(source, destination, 4)  # RENAME_EXCL
    else:
        raise OSError('Atomic no-replace rename unavailable on this platform.')
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(paused))


def pause_pending(staged, text, expected_inode):
    paused = staged.with_name('PAUSED')
    pending = os.path.lexists(staged)
    require(pending != os.path.lexists(paused),
            'Pause was removed or set separately; preserve the operator decision.')
    path = staged if pending else paused
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and [info.st_dev, info.st_ino] == expected_inode
            and path.read_text(encoding='utf-8') == text + '\n',
            'Unrecognized pause; preserve the operator decision.')
    return pending


def handoff(source, candidate, apply=None):
    old, new = load_pair(source, candidate)
    with runtime.lock_workspace(old) as lock:
        require(lock is not None, 'Existing worker is busy; no takeover.')
        require((old, new) == load_pair(source, candidate), 'Runner configuration changed.')
        source_dir, target_dir = (Path(config['state_dir']) for config in (old, new))
        state_path = target_dir / 'state.json'
        intent_path = target_dir / 'identity-handoff.intent.json'
        request = [str(Path(path).resolve()) for path in (source, candidate)]
        intent = None
        reviewed_at = time.time()
        if os.path.lexists(intent_path):
            require(stat.S_ISREG(intent_path.lstat().st_mode), 'Invalid handoff intent.')
            intent = runtime.read_json(intent_path)
            require(intent['request'] == request, 'Another handoff was requested for this candidate.')
            reviewed_at = intent['reviewed_at']
        else:
            require(not any(os.path.lexists(directory / 'PAUSED') for directory in (source_dir, target_dir)),
                    'A runner is paused; preserve its stop and reconcile before changing identities.')
            require(not os.path.lexists(state_path), 'Candidate already has execution state.')
        summary, digest, state, target_binding = preview(old, new, reviewed_at)
        require((intent is None or intent['review_sha256'] == digest) and apply in (None, digest),
                'Review digest has changed; handoff stopped.')
        if intent is None and apply is None:
            return dict(summary, status='preview', review_sha256=digest)

        pending_name = '.identity-handoff-' + digest + '.pending'
        markers = [(target_dir / pending_name, TARGET_PAUSE), (source_dir / pending_name, SOURCE_PAUSE)]
        state_exists = os.path.lexists(state_path)
        require(not state_exists or stat.S_ISREG(state_path.lstat().st_mode), 'Invalid candidate state file.')
        target = runtime.load_state(state_path, runtime.identity(new, target_binding))
        if not state_exists:
            target.update({key: state[key] for key in ('session_id', 'session_runs', 'session_history',
                          'session_handoff', 'last_result') if key in state})
            target['actor_handoff'] = {'at': datetime.fromtimestamp(reviewed_at, timezone.utc).isoformat(),
                                       'previous_state_dir': str(source_dir), 'review_sha256': digest,
                                       'instruction': '仅参考旧目录中的处理记录及待办，不使用旧身份或历史登录，不重放已做写入。以本次完整扫描核实当前状态和人的确认。'}
        if intent is None:
            intent = {'request': request, 'reviewed_at': reviewed_at, 'review_sha256': digest,
                      'target_sha256': runtime.digest(target),
                      'inodes': [stage_pause(path, text) for path, text in markers]}
            runtime.control._new_json(intent_path, intent)
        require(runtime.digest(target) == intent['target_sha256'],
                'Candidate state changed; preserve its execution history.')
        require(len(intent['inodes']) == len(markers), 'Invalid handoff pause markers.')
        pending = [pause_pending(path, text, inode)
                   for (path, text), inode in zip(markers, intent['inodes'])]
        require(not (pending[0] and not pending[1]) and not (state_exists and any(pending)),
                'Unexpected handoff order; reconcile first.')
        if apply is None:
            return dict(summary, status='preview', review_sha256=digest)
        # A retry may see a file installed just before an interrupted directory fsync.
        sync_directory(target_dir)
        for (staged, text), inode in zip(markers, intent['inodes']):
            if pause_pending(staged, text, inode):
                move_pause(staged, staged.with_name('PAUSED'))
            sync_directory(staged.parent)
        if not state_exists:
            require(all(not pause_pending(path, text, inode)
                        for (path, text), inode in zip(markers, intent['inodes'])),
                    'Runner stop changed before state installation.')
            runtime.control._new_json(state_path, target)
        sync_directory(target_dir)
        require(runtime.read_json(state_path) == target and all(
            not pause_pending(path, text, inode) for (path, text), inode in zip(markers, intent['inodes'])),
            'Readback failed; reconcile both stops.')
        return dict(summary, status='prepared_both_paused', cron_changed=False, checkpoint_advanced=False)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--apply', metavar='REVIEW_SHA256')
    args = parser.parse_args()
    try:
        print(json.dumps(handoff(args.source, args.candidate, args.apply), ensure_ascii=False))
    except (ValueError, TypeError, OSError, KeyError, runtime.RunnerError, runtime.OnboardingError) as error:
        sys.exit(str(error))
