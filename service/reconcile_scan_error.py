#!/usr/bin/env python3
"""Recheck an interrupted personal-browser read before handing its project to a new identity."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import uuid

from identity_handoff import require, require_full_scan, runtime, sync_directory
import browser_watch

AUDIT = 'read_error_reconciliations'


def timestamp(value):
    date = datetime.fromisoformat(value)
    require(date.tzinfo is not None, '原执行时间缺少时区。')
    return date.timestamp()


def last_cron_receipt(path):
    # The legacy runner had no typed field in state. Corroborate its exact,
    # machine-emitted result in its own scheduler log. Ignore wrapper diagnostics.
    with path.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - 65536))
        if stream.tell():
            stream.readline()
        for line in reversed(stream.read().splitlines()):
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(item, dict) and 'status' in item:
                return item
    raise ValueError('原执行器的日志里没有这次失败回执。')


def finished_worker(path, session):
    """One bounded Codex invocation must end after every started item has closed."""
    turn = 'awaiting_thread'
    pending = {}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event['type']
            if kind == 'thread.started':
                require(turn == 'awaiting_thread' and event.get('thread_id') == session
                        and str(uuid.UUID(session)) == session, '原执行会话不符。')
                turn = 'ready'
            elif kind == 'turn.started':
                require(turn == 'ready', '原执行回执之后还有一次运行，需先核实。')
                turn = 'running'
            elif kind == 'turn.completed':
                require(turn == 'running' and not pending, '原执行回执仍有未结束的操作。')
                turn = 'completed'
            elif kind in ('item.started', 'item.updated', 'item.completed'):
                item = event['item']
                key = item.get('id')
                item_type = item.get('type')
                require(turn == 'running' and isinstance(key, str) and key
                        and isinstance(item_type, str) and item_type
                        and pending.get(key, item_type) == item_type, '原操作编号、类型或运行阶段不符。')
                if kind == 'item.completed':
                    # Messages can arrive as a single completed event. An unsuccessful
                    # command can also be terminal inside an otherwise successful turn.
                    require(item.get('status') in (None, 'completed', 'failed', 'cancelled', 'canceled', 'declined'),
                            '原操作仍在等待，或缺少可识别的结束状态。')
                    pending.pop(key, None)
                else:
                    require(kind != 'item.started' or key not in pending, '原操作重复开始。')
                    pending[key] = item_type
            else:
                raise ValueError('原执行回执含失败或无法核实的事件。')
    require(turn == 'completed' and not pending, '原执行会话缺少有效的完成回执。')


def source_evidence(config, binding, state, directory, clock):
    error = state.get('last_error')
    require(isinstance(error, dict) and set(error) == {'message', 'at'}
            and error['message'] == browser_watch.RECORDS_INCOMPLETE,
            '只支持原浏览器在启动 Codex 之前未能读完整记录的错误。')
    require(not any(state.get(k) for k in ('inflight', 'recovery_note', 'active_operations', 'pending_effects')),
            '原执行器有在途或未核实的操作。')
    runtime.other_mode_state(config, binding)
    failed = timestamp(error['at'])
    success = timestamp(state['last_success_at'])
    log = directory / 'cron.log'
    require(success < failed <= clock and timestamp(state['last_scan_at']) < failed
            and log.stat().st_mtime_ns >= (directory / 'state.json').stat().st_mtime_ns
            and last_cron_receipt(log) == dict(status='failed', message=error['message'], checkpoint_advanced=False),
            '原执行器的读取时间、检查点或本次读表失败回执对不上。')
    previous = runtime.run_context.checkpoint_snapshot(directory, state)
    require(previous is not None, '找不到和原检查点完全一致的快照。')
    require_full_scan(previous, binding, browser=True)
    last = state['last_result']
    run_id = str(uuid.UUID(last['run_id']))
    require(run_id == last['run_id'] and last['outcome'] in ('waiting', 'continue', 'idle'),
            '找不到成功完成的原执行轮次。')
    run = directory / 'runs' / run_id
    original = runtime.read_json(run / 'snapshot.json')
    runtime.validate_result(last, run_id, runtime.run_context.snapshot_digest(original))
    require(last == runtime.read_json(run / 'result.json'), '原始执行回执和保存的结果不同。')
    if state['checkpoint'] != last['snapshot_id']:
        require(state.get('effects_consumed_run') == run_id, '原检查点没有对应的执行或写入回执。')
        after_writes = runtime.own_effects.expected_snapshot(
            original, runtime.read_json(run / 'effects.json'), run_id, last['snapshot_id'])
        require(runtime.run_context.snapshot_digest(after_writes) == state['checkpoint'],
                '原检查点和已核实的实际写入不一致。')
    events = run / 'events.jsonl'
    finished_worker(events, state['session_id'])
    # Compare file times to each other. On some Linux volumes kernel write
    # timestamps lag the application clock by several milliseconds.
    require(not any(f.parent != run and f.stat().st_mtime_ns >= events.stat().st_mtime_ns
                    for f in (directory / 'runs').glob('*/events.jsonl')),
            '最后一次成功之后还有其他执行会话的记录，需先核实。')
    handoff = run / 'handoff.json'
    if handoff.is_file():
        note = runtime.run_context.validate_handoff(runtime.read_json(handoff), last)
        require(not note['active_operations'], '最后交接仍有进行中的外部操作。')
    report = run / 'REPORT.json'
    if report.is_file():
        data = runtime.read_json(report)
        require(isinstance(data, dict) and data.get('run_id', run_id) == run_id
                and data.get('snapshot_id', last['snapshot_id']) == last['snapshot_id']
                and not data.get('active_operations') and not data.get('pending_effects'),
                '最后执行报告仍有未收尾的操作。')
    return previous


def read_original(config, binding, directory):
    # The browser collector can update its local comment index. Isolate this
    # check from the original journal and retain its view of earlier comments.
    with tempfile.TemporaryDirectory(prefix='lark-verify-original-read-') as scratch:
        journal = directory / 'browser-comments-state.json'
        if journal.exists():
            shutil.copyfile(journal, Path(scratch) / journal.name)
        return runtime.collect_snapshot(config, binding, Path(scratch), runtime.watch.collect)


def sync_state(path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def reconcile(source, apply=None):
    config = runtime.control.load_config(source)
    require(config['format_version'] == 2, '需要原本人网页执行器的配置。')
    require(apply is None or re.fullmatch('[0-9a-f]{64}', apply), '核对编号无效。')
    directory = Path(config['state_dir'])
    state_path = directory / 'state.json'
    require(stat.S_ISREG(state_path.lstat().st_mode), '找不到原执行状态文件。')
    with runtime.lock_workspace(config) as lock:
        require(lock is not None, '原工作区正在使用，稍后再试。')
        binding = runtime.read_json(config['project_config'])
        state = runtime.load_state(state_path, runtime.identity(config, binding))
        journal = state.get(AUDIT, [])
        require(isinstance(journal, list) and all(isinstance(entry, dict) for entry in journal), '原核对记录无法识别。')
        source_path = str(Path(source).resolve())
        if not state.get('last_error') and apply:
            require(any(i.get('review_sha256') == apply and i.get('source_config') == source_path
                        and i.get('config_sha256') == runtime.digest(config)
                        and i.get('source_identity') == state['identity'] for i in journal),
                    '核对编号与此原配置不符。')
            sync_state(state_path)
            return dict(status='already_reconciled', review_sha256=apply)
        require(not os.path.lexists(directory / 'PAUSED'), '原执行器已暂停，请保留原暂停约定。')
        rules = runtime.project_governance(config, binding)
        previous = source_evidence(config, binding, state, directory, time.time())
        current = read_original(config, binding, directory)
        require_full_scan(current, binding, browser=True)
        for field in ('records', 'comments'):
            require(isinstance(current.get(field), dict) and isinstance(previous.get(field), dict)
                    and previous[field].keys() <= current[field].keys(), '完整复查缺少原本可见的记录或评论。')
        require(runtime.control.load_config(source) == config and runtime.read_json(config['project_config']) == binding
                and runtime.project_governance(config, binding) == rules and runtime.read_json(state_path) == state
                and not os.path.lexists(directory / 'PAUSED'), '复查期间原配置、执行状态或暂停约定已改变。')
        source_evidence(config, binding, state, directory, time.time())
        proof = dict(source_config=source_path, config_sha256=runtime.digest(config), source_identity=state['identity'],
                     last_result_sha256=runtime.digest(state['last_result']),
                     checkpoint=state['checkpoint'], scan_sha256=runtime.run_context.snapshot_digest(current),
                     records=len(current['records']), comments=len(current['comments']))
        review = runtime.digest([str(Path(source).resolve()), config, binding, rules, state, proof])
        if apply is None:
            return dict(status='preview', review_sha256=review, **proof)
        require(apply == review, '原状态或完整复查结果已变化，请重新核对。')
        # The audit and error removal are one atomic state replacement. A retry
        # sees either the whole original state or a durable completed review.
        state[AUDIT] = [*journal, dict(review_sha256=review, at=runtime.now(),
                                       original_error=state.pop('last_error'), **proof)]
        runtime.atomic_json(state_path, state)
        sync_state(state_path)
        require(runtime.read_json(state_path) == state, '复查记录未完整保存。')
        return dict(status='reconciled', review_sha256=review, checkpoint_advanced=False)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--apply', metavar='REVIEW_SHA256')
    args = parser.parse_args()
    try:
        print(json.dumps(reconcile(args.source, args.apply), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, runtime.RunnerError, runtime.OnboardingError) as exc:
        parser.exit(1, str(exc) + '\n')
