#!/usr/bin/env python3
"""Post one native record comment for a prepared plan, then verify it by ID."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import prepare_plan as plan
from identity_handoff import sync_directory


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


def notify(project, record, person, revision):
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
        if os.path.lexists(intent_file):
            plan.check(plan.bridge.load(intent_file) == intent, '这版计划已有发出记录，但接收人、内容或位置有变化。')
            plan.check(accepted_file.exists(), '这版提醒已尝试发送，尚未拿到评论编号。请到本研发项查看。')
            receipt = plan.bridge.load(accepted_file)
        else:
            plan.check(not os.path.lexists(accepted_file) and not os.path.lexists(verified_file), '提醒文件不完整，请先核对。')
            validate_preview(project, drive(project, '+add-comment', args, preview=True), anchor, person, message)
            plan.check(project.row('work', record) == work, '发送前研发项有变化，请重新核对。')
            project.unchanged()
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
    return {'status': 'notified', 'record_id': record, 'plan_revision': revision, **receipt}


def run(path, record, confirmer, revision):
    try:
        plan.check(re.fullmatch(r'rec[A-Za-z0-9]+', record) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', confirmer)
                   and type(revision) is int and 0 < revision < 2**53, '请提供完整的研发项编号、人员编号和修订号。')
        return notify(plan.Project(path), record, confirmer, revision)
    except (plan.Conflict, plan.confirmation.Pending) as exc:
        reason = str(exc)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, subprocess.SubprocessError,
            plan.OnboardingError, plan.control.ControlError):
        reason = '尚未核实这版提醒的发送结果。已尝试发送的提醒会保留，供下次核对。'
    return {'status': 'pending', 'reason': reason, 'record_id': record, 'plan_revision': revision}


def main():
    parser = argparse.ArgumentParser(description='在已准备好的研发项里提醒计划确认人。')
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--work', required=True)
    parser.add_argument('--confirmer', required=True)
    parser.add_argument('--revision', required=True, type=int)
    args = parser.parse_args()
    result = run(args.config, args.work, args.confirmer, args.revision)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'notified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
