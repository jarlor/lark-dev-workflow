#!/usr/bin/env python3
"""Post one native record comment for a prepared plan, then verify it by ID."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time

import prepare_plan as plan
import gate_events
import unattended
from identity_handoff import sync_directory


WAIT_FOR_RECEIVER = '助手还没准备好接收这条事项的确认。请先用同一执行器扫描项目，再检查一次。'

def fixed_view(project):
    table = project.tables['work']
    def matching(views):
        return [v['id'] for v in views if v.get('type') == 'grid' and v.get('name') == '待确认计划']
    pinned = matching(table['views'])
    live = project.reader.base.pages('+view-list', 'views', '--base-token', project.token,
                                     '--table-id', table['id'], limit=200)
    plan.check(len(pinned) == 1 and pinned == matching(live) and re.fullmatch(r'vew[A-Za-z0-9]+', pinned[0]),
               '未找到绑定到这张研发表的“待确认计划”视图。')
    return pinned[0]


def drive(project, operation, args, *, preview=False):
    argv = ['lark-cli', 'drive', operation, *args, '--as', 'bot', '--profile', project.reader.profile]
    if preview:
        argv.append('--dry-run')
    result = project.reader.runner(argv, capture_output=True, text=True, timeout=45)
    response = json.loads(result.stdout)
    plan.check(result.returncode == 0 and response.get('ok') is True and response.get('identity') == 'bot'
               and (response.get('dry_run') is True) == preview and isinstance(response.get('data'), dict),
               '未收到当前飞书助手的成功回执。')
    return response['data']


def validate_preview(project, preview, anchor, person, message):
    expected = {'method': 'POST', 'url': '/open-apis/drive/v1/files/' + project.token + '/new_comments',
                'body': {'file_type': 'bitable', 'anchor': anchor,
                         'reply_elements': [{'type': 'mention_user', 'mention_user': person},
                                            {'type': 'text', 'text': message}]}}
    plan.check(preview.get('file_token') == project.token
               and preview.get('context', {}).get('app_id') == project.config['app_id']
               and preview.get('api') == [expected], '评论预览中的应用、接收人或记录位置不符。')


def confirm_comment(project, receipt, anchor, person, message):
    data = drive(project, '+batch-query-comments',
                 ['--token', project.token, '--type', 'bitable', '--comment-ids', receipt['comment_id']])
    items = data.get('items')
    plan.check(data.get('file_token') == project.token and data.get('file_type') == 'bitable'
               and isinstance(items, list) and len(items) == 1, '尚未在这份表格读到已发送的评论。')
    root = items[0]
    plan.check(root.get('comment_id') == receipt['comment_id'] and root.get('is_whole') is False
               and root.get('user_id') == project.open_id, '读回的评论位置或作者不符。')
    replies = root['reply_list']['replies']
    reply = replies[0]
    location = reply.get('extra', {}).get('notify_extra', {})
    elements = reply['content']['elements']
    plan.check(reply.get('reply_id') == receipt['reply_id'] and reply.get('user_id') == project.open_id
               and (location.get('table'), location.get('record'), location.get('view')) ==
               (anchor['block_id'], anchor['base_record_id'], anchor['base_view_id'])
               and len(elements) == 2 and elements[0].get('type') == 'person'
               and elements[0]['person']['user_id'] == person and elements[1].get('type') == 'text_run'
               and elements[1]['text_run']['text'] == message, '读回的事项、人员或提醒文字不符。')


def persist(path, value):
    # Exclusive creation and fsync before any remote write. A lost response is never a retry instruction.
    plan.control._new_json(path, value)
    sync_directory(path.parent)


def require_capture_ready(project, role, record):
    """Check the receiver's actual membership and epoch immediately before a new invitation."""
    now = int(time.time())
    scope = plan.bridge.load(Path(project.paths[1]) / gate_events.INDEX)
    started, observed = scope.get('started_at'), scope.get('observed_at')
    plan.check(scope.get('signature') == gate_events.signature(project.binding, project.config['tenant_key'])
               and type(started) is int and type(observed) is int and 0 < started <= observed <= now
               and observed >= now - gate_events.MAX_AGE, WAIT_FOR_RECEIVER)
    bound = project.tables[role]
    table_id = bound['id']
    table = scope['tables'].get(table_id, {})
    fields = bound['fields']
    field_id = fields['status']['id']
    since = table.get('since', {}).get(record)
    plan.check(table_id in project.config['table_ids'] and record in table.get('records', [])
               and table.get('project_field') == fields['project']['id']
               and table.get('fields', {}).get(field_id) == 'status'
               and table.get('native_stage_field_id') == field_id and type(since) is int
               and 0 < since <= observed and since < now, WAIT_FOR_RECEIVER)

    database = project.directory / 'hints.sqlite'
    metadata = database.lstat()
    plan.check(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid(), WAIT_FOR_RECEIVER)
    with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('BEGIN')
        plan.check(db.execute('SELECT identity FROM state WHERE singleton=1').fetchone() ==
                   (plan.bridge.journal_identity(project.config),) and
                   db.execute('SELECT binding FROM writer WHERE singleton=1').fetchone() ==
                   (plan.bridge.writer_identity(project.config['runner_config'], project.paths),), WAIT_FOR_RECEIVER)
        barriers = db.execute('SELECT closed_at,reopened_at FROM membership_barriers WHERE '
                              "(table_id=? AND record_id=?) OR (table_id='' AND record_id='')", (table_id, record))
        for closed, reopened in barriers:
            # The receiver reopens a closed barrier on the next event only after a later full scan.
            plan.check(type(closed) is int and 0 < closed <= now, WAIT_FOR_RECEIVER)
            if reopened is None:
                plan.check(started > closed and observed < now, WAIT_FOR_RECEIVER)
            else:
                plan.check(type(reopened) is int and closed < reopened < now, WAIT_FOR_RECEIVER)
    lock = os.open(project.directory / 'receiver.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(lock, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        plan.check(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid(), WAIT_FOR_RECEIVER)
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            epoch = handle.read(32).strip()
            plan.check(re.fullmatch(rb'[0-9]{1,20}', epoch) and 0 < int(epoch) < now, WAIT_FOR_RECEIVER)
        else:
            raise plan.Conflict(WAIT_FOR_RECEIVER)


def pending_file(project, key):
    directory = project.directory / 'pending-plan-notices'
    if not directory.exists():
        directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(project.directory)
    metadata = directory.lstat()
    plan.check(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid() and not metadata.st_mode & 0o077,
               '待发提醒的目录不属于当前服务或权限不符。')
    return directory / (key + '.json')


def requested_notice(project, intent):
    if 'rules_reference' not in project.binding.get('governance', {}):
        return intent
    _, rule_file = unattended.project_governance(project.runner, project.binding)
    return dict(intent, contents=plan.digest([intent['contents'], rule_file]))


def notify(project, record, person, revision, *, expected_request=None):
    project.unchanged()
    work = project.row('work', record)
    requirements = plan.ids(work['requirements'])
    plan.check(len(requirements) == 1 and plan.ids(work['project']) == [project.project]
               and work['status'] == ['待确认计划'] and work['nature'] == ['当前工作']
               and plan.ids(work['plan_confirmer']) == [person] and person != project.open_id
               and type(work['plan_revision']) is int and work['plan_revision'] == revision,
               '这项计划的项目、状态、确认人或修订号已变，请先核对。')
    prepared = plan.get_prepared(project.path, requirements[0], project=project)
    plan.check(prepared.get('status') == 'prepared' and prepared.get('record_id') == record
               and prepared.get('confirmer') == person and prepared.get('plan_revision') == revision
               and all(work[k] == prepared['values'][k] and plan.confirmation.inline_text(work[k]) for k in plan.TEXT),
               '这条记录还没有与当前计划内容一致的准备完成回执。')
    if project.config.get('human_digest'):
        return {'status': 'scheduled_digest', 'record_id': record, 'plan_revision': revision,
                'reason': '计划已写好，下一份个人待办会列出仍需确认的计划。'}
    plan.confirmation.confirm_directory_user(project.reader, person)
    view = fixed_view(project)
    table = project.tables['work']['id']
    plan.check(re.fullmatch(r'tbl[A-Za-z0-9]+', table), '研发表编号有误。')
    anchor = {'block_id': table, 'base_record_id': record, 'base_view_id': view}
    field_name = project.tables['work']['fields']['plan']['name']
    message = f' 请看这项的「{field_name}」（第 {revision} 版）。同意按这个做，请在「待确认计划」点「完成节点」。要改哪里，直接回复这条评论就好。'
    elements = [{'type': 'mention_user', 'text': person}, {'type': 'text', 'text': message}]
    args = ['--doc', project.token, '--type', 'bitable', '--block-id', '!'.join((table, record, view)),
            '--content', json.dumps(elements, ensure_ascii=False)]
    # Scope the send-once key to the business destination, independent of the recipient, view or bot profile.
    key = plan.digest([project.token, project.project, table, record, revision])
    prefix = project.directory / ('plan-notice-' + key)
    intent_file, accepted_file, verified_file = [prefix.with_suffix('.' + s) for s in ('intent', 'accepted', 'verified')]
    intent = {'binding': project.signature, 'anchor': anchor, 'revision': revision, 'confirmer': person,
              'contents': plan.digest([prepared['values'], message])}
    with plan.bridge.exclusive(prefix.with_suffix('.lock')):
        requested = requested_notice(project, intent)
        request = pending_file(project, key)
        queued = os.path.lexists(request)
        if queued:
            metadata = request.lstat()
            plan.check(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
                       and not metadata.st_mode & 0o077 and plan.bridge.load(request) == requested,
                       '待发提醒对应的计划、接收人或位置已变。')
        if expected_request is not None:
            plan.check(queued and expected_request == requested, '没有找到与原申请一致的待发提醒。')
        if os.path.lexists(intent_file):
            plan.check(plan.bridge.load(intent_file) == intent, '这版计划已有发出记录，但接收人、内容或位置有变化。')
            plan.check(accepted_file.exists(), '这版提醒已尝试发送，尚未拿到评论编号。请到本研发项查看。')
            receipt = plan.bridge.load(accepted_file)
        else:
            plan.check(not os.path.lexists(accepted_file) and not os.path.lexists(verified_file), '提醒文件不完整，请先核对。')
            validate_preview(project, drive(project, '+add-comment', args, preview=True), anchor, person, message)
            plan.check(project.row('work', record) == work, '发送前研发项有变化，请重新核对。')
            project.unchanged()
            plan.check(requested_notice(project, intent) == requested, '发送前项目规则有变化，请重新核对。')
            if not queued:
                persist(request, requested)
            try:
                require_capture_ready(project, 'work', record)
            except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise plan.Conflict(WAIT_FOR_RECEIVER) from exc
            persist(intent_file, intent)
            data = drive(project, '+add-comment', args)
            plan.check(data.get('file_token') == project.token and data.get('file_type') == 'bitable'
                       and data.get('comment_mode') == 'base_record'
                       and (data.get('base_block_id'), data.get('base_record_id'), data.get('base_view_id')) ==
                       (table, record, view) and all(isinstance(data.get(k), str) and data[k] for k in ('comment_id', 'reply_id')),
                       '发送结果缺少这条研发项上的评论编号，已保留本次尝试。')
            receipt = {k: data[k] for k in ('comment_id', 'reply_id')}
            persist(accepted_file, receipt)
        confirm_comment(project, receipt, anchor, person, message)
        if not verified_file.exists():
            persist(verified_file, receipt)
        plan.check(plan.bridge.load(verified_file) == receipt, '提醒回执有冲突，请先核对。')
        if os.path.lexists(request):
            plan.check(plan.bridge.load(request) == requested, '待发提醒与已核实的评论不符。')
            request.unlink()
            sync_directory(request.parent)
    return {'status': 'notified', 'record_id': record, 'plan_revision': revision, **receipt}


def run(path, record, confirmer, revision, *, expected_request=None):
    try:
        plan.check(re.fullmatch(r'rec[A-Za-z0-9]+', record) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', confirmer)
                   and type(revision) is int and 0 < revision < 2**53, '请提供完整的研发项编号、人员编号和修订号。')
        return notify(plan.Project(path), record, confirmer, revision, expected_request=expected_request)
    except (plan.Conflict, plan.confirmation.Pending) as exc:
        reason = str(exc)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, subprocess.SubprocessError, sqlite3.Error,
            plan.OnboardingError, plan.control.ControlError, unattended.RunnerError):
        reason = '尚未核实这版提醒的发送结果。已尝试发送的提醒会保留，供下次核对。'
    return {'status': 'pending', 'reason': reason, 'record_id': record, 'plan_revision': revision}


def check_before_invite(path, role, record):
    """Check a waiting row and its active receiver without posting a comment."""
    result = {'status': 'pending', 'check': 'before_invitation', 'role': role, 'record_id': record}
    try:
        plan.check(role in plan.confirmation.GATES and re.fullmatch(r'rec[A-Za-z0-9]+', record),
                   '请指定需求、Bug 或研发项的真实记录编号。')
        project = plan.Project(path)
        project.unchanged()
        row = project.row(role, record)
        before, after, person, revision, required = plan.confirmation.GATES[role]
        people = plan.ids(row[person])
        plan.check(plan.ids(row['project']) == [project.project] and row['nature'] == ['当前工作']
                   and row.get('processing') in (None, [], ['正常']) and not plan.ids(row.get('merged_into')),
                   '这条事项不属于本项目的当前工作，或已暂停、归并。')
        plan.check(row['status'] != [after], f'这条事项已在「{after}」。请核对已有的点击，跳过这次提醒。')
        plan.check(row['status'] == [before] and len(people) == 1 and people[0] != project.open_id
                   and type(row[revision]) is int and row[revision] > 0
                   and all(plan.confirmation.inline_text(row[key]) for key in required),
                   '请核对当前节点、确认人和这版需要确认的内容。')
        plan.confirmation.confirm_directory_user(project.reader, people[0])
        require_capture_ready(project, role, record)
        plan.check(project.row(role, record) == row, '检查期间事项有变化，请重新核对。')
        project.unchanged()
        status = 'scheduled_digest' if project.config.get('human_digest') else 'ready_to_invite'
        result.update(status=status, confirmer=people[0], revision=row[revision])
    except (plan.Conflict, plan.confirmation.Pending) as exc:
        result['reason'] = str(exc)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, subprocess.SubprocessError,
            sqlite3.Error, plan.OnboardingError, plan.control.ControlError, unattended.RunnerError):
        result['reason'] = '未能核实这条事项和接收服务。请保留原记录，稍后重查。'
    return result


def main():
    parser = argparse.ArgumentParser(description='检查能否收到人工确认，或提醒研发项的计划确认人。')
    parser.add_argument('--config', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--work', help='发送已准备好的计划提醒')
    mode.add_argument('--check-before-invite', choices=plan.confirmation.GATES, help='只检查接收状态')
    parser.add_argument('--record-id', help='要核对的事项记录编号')
    parser.add_argument('--confirmer')
    parser.add_argument('--revision', type=int)
    args = parser.parse_args()
    if args.check_before_invite:
        if not args.record_id or args.confirmer is not None or args.revision is not None:
            parser.error('检查接收状态时，请用 --record-id 指定待确认的事项。')
        result = check_before_invite(args.config, args.check_before_invite, args.record_id)
    else:
        if not args.confirmer or args.revision is None or args.record_id is not None:
            parser.error('发送计划提醒时，需要 --work、--confirmer 和 --revision。')
        result = run(args.config, args.work, args.confirmer, args.revision)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('notified', 'ready_to_invite', 'scheduled_digest') else 1


if __name__ == '__main__':
    raise SystemExit(main())
