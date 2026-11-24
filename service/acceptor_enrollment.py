"""Check who can fill an empty acceptance field on a previously confirmed item."""
import argparse
import json
import re
import sqlite3
import subprocess

import stage_action
import stage_business
import stage_source
import verify_confirmation as confirmation
from onboard import OnboardingError
from prepare_plan import ids

require = stage_action.require
ROLES = ('requirements', 'bugs')


def native_creator(project, table, record):
    url = f'/open-apis/bitable/v1/apps/{project.token}/tables/{table}/records/{record}'
    result = project.reader.runner([project.runner['lark_bin'], 'api', 'GET', url,
        '--params', '{"automatic_fields":true,"user_id_type":"open_id"}', '--as', 'bot',
        '--profile', project.reader.profile], capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, '助手未能读取反馈的原始创建人。')
    envelope = json.loads(result.stdout)
    raw = envelope.get('data', {}).get('record', {})
    require(envelope.get('ok') is True and envelope.get('identity') == 'bot' and raw.get('record_id') == record
            and isinstance(raw.get('created_by'), dict) and type(raw.get('created_time')) is int
            and raw['created_time'] > 0, '飞书没有返回这条反馈的原始创建人和创建时间。')
    return raw['created_by'], raw['created_time'] // 1000


def confirmed_subject(adapter, role, record, row, proof):
    """The original scope, its confirmer, project and linked feedback must still match."""
    project = adapter.p
    adapter.project_open()
    require(role in ROLES and proof.get('status') == 'verified' and proof.get('role') == role
            and proof.get('check') in stage_business.HUMAN_PROOF_CHECKS
            and (proof.get('project_record_id'), proof.get('table_id'), proof.get('record_id'),
                 proof.get('field_id'), proof.get('business_revision')) ==
                (project.project, project.table, record, project.fields['status']['id'], row.get('revision'))
            and project.fields['acceptor']['type'] == 'user' and project.fields['confirmer']['type'] == 'user'
            and adapter.row(role, record) == row and row.get('processing') == ['正常'],
            '当前项目、记录或需求版本与原确认不一致。')
    person = proof['operator_open_id']
    people = row['confirmer']
    require(isinstance(person, str) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', person)
            and person != project.bot and ids(people) == [person] and isinstance(people[0].get('name'), str)
            and people[0]['name'].strip(), '当前确认人和原来亲自确认的人不一致。')
    name = people[0]['name']
    linked = adapter.feedback(role, record)
    require(linked and len(linked) == len(set(linked)), '原反馈缺失或关联重复，暂时无法确定验收人。')
    return person, name, linked


def check_origin(adapter, role, record, row, proof):
    project = adapter.p
    person, name, linked = confirmed_subject(adapter, role, record, row, proof)
    feedback = adapter.tables['feedback']
    author = feedback['fields']['author']
    require(author['type'] in ('user', 'text'), '这张反馈表没有可核实的原作者字段。')
    for fid in linked:
        source = adapter.row('feedback', fid)
        submitted = source.get('author')
        require((submitted == name if author['type'] == 'text' else ids(submitted) == [person]),
                '关联反馈的原作者与该事项的确认人不同或不唯一。')
        creator, timestamp = native_creator(project, feedback['id'], fid)
        require(creator.get('id') == person and creator.get('name') == name,
                '飞书记录的反馈创建人与原确认人不同；请核实是否有人代为录入。')
        history = confirmation.history(project.reader, project.token, feedback['id'], fid)
        require(history and history[-1].get('activity_type') == 'create'
                and history[-1].get('create_time') == timestamp and timestamp <= proof['update_time'],
                '没有找到本人确认前提交这条原反馈的记录。')
        confirmation.check_history_author(history[-1], name)
        require(all(type(i.get('rev')) is int and i['rev'] > 0 and type(i.get('create_time')) is int for i in history)
                and all(a['rev'] > b['rev'] and a['create_time'] >= b['create_time'] for a, b in zip(history, history[1:])),
                '反馈历史的版本或时间有缺失、重复或乱序。')
        edits = []
        for item in history[:-1]:
            changes = item.get('field_changes')
            require(item.get('activity_type') == 'update' and isinstance(changes, list),
                    '这条反馈的历史不完整，暂时无法核实原作者。')
            edits.extend(change for change in changes if change['field_id'] == author['id'])
        require(not edits or (len(edits) == 1 and edits[0].get('before') in ('', None)
                and edits[0].get('after') == name and edits[0].get('field_type') ==
                ('Text' if author['type'] == 'text' else 'User')), '反馈的原作者曾被替换。')
    return person, linked


def assigned_person(adapter, role, record, row, proof):
    """An explicit project assignment can fill an empty cell, never approve acceptance."""
    project = adapter.p
    confirmer, _, linked = confirmed_subject(adapter, role, record, row, proof)
    grant = stage_source.query(project, 'acceptor_assignment', role, record)
    require(grant.get('method') == 'project_test_acceptor'
            and (grant.get('project_record_id'), grant.get('table_id'), grant.get('record_id'),
                 grant.get('field_id'), grant.get('role'), grant.get('revision')) ==
                (project.project, project.table, record, project.fields['acceptor']['id'], role, row['revision'])
            and type(grant.get('revision')) is int and grant.get('authorizer_open_id') == grant.get('confirmer_open_id') == confirmer
            and type(grant.get('confirmation_revision')) is int and grant['confirmation_revision'] == proof['table_revision']
            and grant.get('feedback_ids') == sorted(linked), '项目的验收分工没有对应这条已确认的事项。')
    person = grant.get('acceptor_open_id')
    require(isinstance(person, str) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', person)
            and person not in (project.bot, confirmer), '指定的验收人须是分工中的另一位真人。')
    confirmation.confirm_directory_user(project.reader, person)
    return person, linked


def checked_change(project, role, record, row, proof, item):
    people = row['acceptor']
    changes = item.get('field_changes')
    require(isinstance(changes, list) and len(changes) == 1 and isinstance(people, list) and len(people) == 1
            and isinstance(people[0], dict) and isinstance(people[0].get('name'), str) and people[0]['name'].strip()
            and changes[0].get('field_type') == 'User' and changes[0].get('before') in ('', None)
            and changes[0].get('field_id') == project.fields['acceptor']['id']
            and changes[0].get('after') == people[0]['name'] and type(item.get('create_time')) is int
            and item['create_time'] >= proof['update_time'] and item['rev'] > proof['table_revision'],
            '只能补上空的验收人，不能换掉已分配的人。')
    adapter = stage_business.Business(project.path, project.key)
    if ids(people) == [proof['operator_open_id']]:
        require(ids(row['confirmer']) == ids(people) and people[0]['name'] == row['confirmer'][0]['name'],
                '补登原反馈人时，姓名和本人编号都必须一致。')
        check_origin(adapter, role, record, row, proof)
    else:
        person, _ = assigned_person(adapter, role, record, row, proof)
        require(ids(people) == [person], '实际填入的人与已登记的项目验收分工不同。')


def check(config_path, role, record, *, assigned=False):
    """Read-only in Feishu. Use the worker's existing ledger to verify the original approval."""
    require(role in ROLES and isinstance(record, str) and re.fullmatch(r'rec[A-Za-z0-9]+', record),
            '请指定需求或 Bug 的记录编号。')
    adapter = stage_business.Business(config_path, stage_business.FIRST[role])
    project = adapter.p
    head = project.history(record)[0]
    row = adapter.row(role, record)
    require(row['status'] in (['可研发'], ['研发中']), '只有确认后、送验前才可补登验收人。')
    proof = stage_business.confirmation_for(config_path, role, record)
    require(proof.get('status') == 'verified', proof.get('reason') or '这版范围还没有核实过的本人确认。')
    person, linked = (assigned_person if assigned else check_origin)(adapter, role, record, row, proof)
    current = ids(row['acceptor'])
    require(current in ([], [person]), '该事项已经有其他验收人，请保留原分配。')
    project.check_binding()
    require(project.history(record)[0] == head and stage_business.live_row(project, record, project.fields) == row,
            '核对期间事项发生了变化，请重新查看。')
    return dict(status='already_assigned' if current else 'ready_to_fill', table_id=project.table,
                record_id=record, field_id=project.fields['acceptor']['id'], person_open_id=person,
                revision=proof['business_revision'], table_revision=head['rev'], feedback_ids=linked)


def main():
    parser = argparse.ArgumentParser(description='核实原反馈人能否补登为验收人，只读飞书。')
    parser.add_argument('--config', required=True)
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--record-id', required=True)
    parser.add_argument('--assigned', action='store_true', help='按项目已登记的明确分工核实验收人。')
    args = parser.parse_args()
    try:
        result = check(args.config, args.role, args.record_id, assigned=args.assigned)
    except (stage_action.Pending, confirmation.Pending) as exc:
        result = dict(status='pending', reason=str(exc))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError, OnboardingError):
        result = dict(status='pending', reason='未能核实原反馈、确认人或现有验收人。')
    print(json.dumps(result, ensure_ascii=False))
    return int(result['status'] == 'pending')


if __name__ == '__main__':
    raise SystemExit(main())
