#!/usr/bin/env python3
"""Check a scope, plan or final acceptance click against the assistant's receipts and Feishu history."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time

import event_bridge as bridge
import gate_events
import watch
from onboard import OnboardingError
import unattended_control as control


# Confirming this row never confirms its upstream records, Bug reproduction or deployment.
GATES = {
    'requirements': ('待确认', '可研发', 'confirmer', 'revision', ('scope', 'criteria')),
    'bugs': ('待确认', '可研发', 'confirmer', 'revision', ('scope', 'criteria')),
    'work': ('待确认计划', '可开始', 'plan_confirmer', 'plan_revision',
             ('plan', 'scope', 'deliverable', 'verification_plan')),
}


class Pending(ValueError):
    pass


def require(test, reason):
    if not test:
        raise Pending(reason)


def receipt(config, binding, paths, table_id, record_id, field_id, before, after):
    """Only the existing service database is eligible, never worker-supplied evidence."""
    now = int(time.time())
    index = bridge.load(Path(paths[1]) / gate_events.INDEX)
    signature = gate_events.signature(binding, config['tenant_key'])
    require(index.get('signature') == signature and type(index.get('observed_at')) is int
            and now - gate_events.MAX_AGE <= index['observed_at'] <= now,
            '请先用本项目的助手重新做一次只读扫描。')
    table = index['tables'].get(table_id, {})
    since = table.get('since', {}).get(record_id)
    require(record_id in table.get('records', []) and type(since) is int
            and table.get('fields', {}).get(field_id) == 'status', '这条记录不在已核实的项目范围内。')
    directory = Path(config['state_dir'])
    path = directory / 'hints.sqlite'
    require(stat.S_ISDIR(directory.lstat().st_mode) and directory.stat().st_uid == os.geteuid()
            and not directory.stat().st_mode & 0o077 and stat.S_ISREG(path.lstat().st_mode)
            and path.stat().st_uid == os.geteuid(), '接收器的记录文件不属于当前专用服务。')
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('BEGIN')
        require(db.execute('SELECT identity FROM state WHERE singleton=1').fetchone() ==
                (bridge.journal_identity(config),) and
                db.execute('SELECT binding FROM writer WHERE singleton=1').fetchone() ==
                (bridge.writer_identity(config['runner_config'], paths),), '事件日志与项目或执行器不符。')
        barriers = db.execute("SELECT reopened_at FROM membership_barriers WHERE "
                              "(table_id=? AND record_id=?) OR (table_id='' AND record_id='')", (table_id, record_id))
        for reopened, in barriers:
            require(type(reopened) is int, '记录归属有过变化，当前仍待核实。')
            since = max(since, reopened)
        rows = db.execute('SELECT data FROM receipts WHERE binding=? AND table_id=? AND record_id=?',
                          (signature, table_id, record_id)).fetchall()
    edits = []
    for payload, in rows:
        row = json.loads(payload)
        require((row['table_id'], row['record_id']) == (table_id, record_id), '事件的记录编号不一致。')
        if row['field_id'] == field_id:
            require(type(row['event_revision']) is int and row['event_revision'] > 0
                    and type(row['update_time']) is int and row['update_time'] <= now,
                    '该条状态事件的时间或版本不完整。')
            if row['update_time'] > max(since, now - gate_events.MAX_AGE):
                edits.append(row)
    require(edits, '尚未收到能核实这条记录状态前后变化的事件。')
    latest = max(edits, key=lambda e: e['event_revision'])
    require(sum(e['event_revision'] == latest['event_revision'] or e['update_time'] >= latest['update_time']
                for e in edits) == 1, '同一时间或表版本有多个状态操作，需要再核实。')
    if latest.get('value_format') == gate_events.NATIVE_STAGE_IDS:
        require(table.get('native_stage_field_id') == field_id and gate_events.opaque_pair(latest['before'], latest['after']), '该事件缺少已核实的原生节点变化。')
    else:
        require('value_format' not in latest and (latest['before'], latest['after']) == (before, after),
                '最近一次状态操作没有完成本次确认。')
    require(isinstance(latest.get('operator_open_id'), str)
            and re.fullmatch(r'ou_[a-zA-Z0-9_]{1,128}', latest['operator_open_id'])
            and latest['operator_open_id'] != config['bot_open_id'], '这次操作缺少可核实的人员编号。')
    return latest



def confirm_directory_user(reader, person_id):
    # A Base person field may also contain an application's open_id.
    try:
        result = reader.runner(['lark-cli', 'api', 'GET', '/open-apis/contact/v3/users/' + person_id,
                                '--as', 'bot', '--profile', reader.profile,
                                '--params', '{"user_id_type":"open_id"}'],
                               capture_output=True, text=True, timeout=30)
        reply = json.loads(result.stdout)
        require(result.returncode == 0 and reply.get('ok') is True and reply.get('identity') == 'bot'
                and reply.get('data', {}).get('user', {}).get('open_id') == person_id,
                '通讯录未能证实这一编号属于人员账号。')
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError, AttributeError) as exc:
        raise Pending('助手无法从通讯录核实此人的账号类型。') from exc

def history(reader, token, table, record):
    rows, cursor = [], None
    for _ in range(100):
        args = ['--base-token', token, '--table-id', table, '--record-id', record, '--page-size', '50']
        if cursor:
            args += ['--max-version', str(cursor)]
        page = reader.base.read('+record-history-list', *args)
        items, more = page.get('items'), page.get('has_more')
        require(isinstance(items, list) and type(more) is bool, '记录历史的分页信息不完整。')
        rows.extend(items)
        if not more:
            return rows
        following = page.get('next_max_version')
        require(items and type(following) is int and following > 0 and (not cursor or following < cursor),
                '无法连续读完这条记录的历史。')
        cursor = following
    raise Pending('本次未读完这条记录的全部历史。')



def check_history_author(item, person_name):
    # This exact signature is used by native Base automation on the tested tenant.
    # A matching contact name cannot disambiguate it, even if a contact shares it.
    require(item.get('operator') == person_name and person_name.strip() != '多维表格助手',
            '记录历史的操作人不一致，或与平台自动化重名。')
    for key in ('actor_type', 'operator_type', 'source', 'source_type', 'on_behalf_of'):
        if item.get(key) not in (None, ''):
            raise Pending('这条历史带有尚未核实的操作来源或代执行标记。')


def inline_text(value):
    if not isinstance(value, str) or not value.strip():
        return False
    # An address may be the subject of the plan. Only reject an empty body or a
    # standalone pointer; other external documents are outside this row check.
    body = re.sub(r'\[[^\]]*\]\(https?://[^\s)]+\)|https?://[^\s)）]+', '', value,
                  flags=re.IGNORECASE).strip(' \t\n：:。，（）()')
    return bool(body) and not re.fullmatch(
        r'(?:详细方案|详情|完整计划)?(?:详见|参见|请见|见|请打开|打开)?(?:附件|文档|链接)?', body)

def match_history(event, items, field_id, person_name, informational, transition):
    previous_rev, previous_time = None, None
    matches = 0
    for item in items:
        rev, second = item.get('rev'), item.get('create_time')
        require(type(rev) is int and rev > 0 and type(second) is int and second > 0
                and (previous_rev is None or (rev < previous_rev and second <= previous_time)),
                '记录历史的版本或时间不完整，或有重复。')
        previous_rev, previous_time = rev, second
        if rev < event['event_revision'] and second < event['update_time']:
            continue
        changes = item.get('field_changes')
        require(item.get('activity_type') == 'update' and isinstance(changes, list) and changes,
                '确认前后有一笔无法识别的操作。')
        for change in changes:
            fid = change.get('field_id')
            require(isinstance(fid, str) and fid, '历史操作没有指明改了哪个字段。')
            if fid in informational:
                continue
            if (rev == event['event_revision'] and second == event['update_time'] and fid == field_id
                    and change.get('field_type') == 'Stage'
                    and (change.get('before'), change.get('after')) == transition):
                check_history_author(item, person_name)
                matches += 1
            else:
                raise Pending('同次、同秒或后续改过状态、确认人、关联或其他业务字段。')
    require(matches == 1, '没有找到唯一一笔与事件的时间、表版本和节点变化都一致的操作。')


def verify(config_path, role, record_id, *, acceptance_revision=None, reader_factory=watch.Cli, receipt_reader=None):
    # A trusted host may retain the original Feishu event separately from the compact journal.
    # Both reads must use the same source. All live field, history and actor checks still apply.
    read_receipt = receipt if receipt_reader is None else receipt_reader
    result = {'status': 'pending', 'role': role, 'record_id': record_id,
              'check': 'current_record_human_transition', 'content_scope': 'record_fields', 'dispatch_checked': False}
    acceptance = acceptance_revision is not None
    if acceptance:
        result.update(check='acceptance_node_click', content_scope='click_and_record_revision',
                      delivery_evidence_checked=False, independent_retest_checked=False,
                      release_checked=False, feedback_closure_checked=False)
    try:
        require(re.fullmatch(r'rec[a-zA-Z0-9]+', record_id) and role in GATES, '请指定有效的事项和记录编号。')
        if acceptance:
            require(role in ('requirements', 'bugs') and type(acceptance_revision) is int
                    and 0 < acceptance_revision < 2**53, '验收点击须指定需求或 Bug 及本次验收的业务修订号。')
        config = bridge.config_from(config_path)
        paths = bridge.runner_pins(config)
        runner = control.load_config(config['runner_config'])
        binding = bridge.load(paths[2])
        actor = control.actor_for_runner(runner, binding)
        require(runner.get('event_tenant_key') == config['tenant_key'], '执行器没有配对此组织的记录事件。')
        scope, tables = watch._binding(binding)
        table = tables[role]
        table_id, fields = table['id'], table['fields']
        require(table_id in config['table_ids'], '接收器没有绑定这张表。')
        if acceptance:
            before, after, person, revision, required = '待验收', '已验收', 'acceptor', 'revision', ()
        else:
            before, after, person, revision, required = GATES[role]
        status, token = fields['status'], binding['base']['token']
        require(status['type'] == 'stage', '此核对只支持原生流程字段。')
        event = read_receipt(config, binding, paths, table_id, record_id, status['id'], before, after)
        require((event.get('table_id'), event.get('record_id'), event.get('field_id')) ==
                (table_id, record_id, status['id']) and event.get('operator_open_id') != config['bot_open_id']
                and isinstance(event.get('event_id'), str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event['event_id']),
                '原始通知与这张表、这条事项或操作人不一致。')
        if event.get('value_format') == gate_events.NATIVE_STAGE_IDS:
            require(gate_events.opaque_pair(event['before'], event['after']), '原始通知没有两个不同的流程节点。')
        else:
            require('value_format' not in event and (event['before'], event['after']) == (before, after),
                    '原始通知中的状态变化与这次确认不一致。')
        def run_cli(argv, **kwargs):
            kwargs['env'] = dict(kwargs.get('env', os.environ), PATH=runner['path'])
            return subprocess.run(argv, **kwargs)
        reader = reader_factory(actor['profile'], runner=run_cli, executable=runner['lark_bin'], actor=actor)
        watch._fields(reader, binding, role, table, inspected={})
        steps = status['stage_contract']['steps']
        if acceptance:
            step = steps[-1]
            require(step['name'] == before and status['stage_contract']['done'] == after,
                    '绑定的原流程没有从待验收到已验收的最后一步。')
        else:
            step = steps[0]
            require(len(steps) >= 2 and (step['name'], steps[1]['name']) == (before, after)
                    and steps[1]['actor'] == 'ai', '原流程的确认步骤不匹配。')
        require(step['actor'] == 'human' and step['person_field'] == person and fields[revision]['type'] == 'number',
                '原流程约定没有绑定这一步的确认人和业务修订。')
        needed = set(required) | set(step['required_fields'])
        require(all(fields[key]['type'] == 'text' for key in required), '范围或计划字段的类型与约定不符。')
        def current():
            row = reader.read_record(token, table_id, record_id, list(fields.values()))
            require(row['record_id'] == record_id and set(row['values']) == {f['id'] for f in fields.values()},
                    '读取的记录或字段不完整。')
            return {key: row['values'][f['id']] for key, f in fields.items()}
        live = current()
        require(watch._one_project(live, 'project', scope['project_record_id']) and live['status'] == [after],
                '这条记录的所属项目或当前状态不符。')
        people = live[person]
        require(isinstance(people, list) and len(people) == 1 and isinstance(people[0], dict)
                and people[0].get('id') == event['operator_open_id']
                and isinstance(people[0].get('name'), str) and people[0]['name'].strip(),
                '没有唯一一位与实际操作人一致的指定确认人。')
        confirm_directory_user(reader, event['operator_open_id'])
        if acceptance:
            require(type(live[revision]) is int and live[revision] == acceptance_revision,
                    '当前事项的修订号与想核对的验收版本不同。')
        else:
            require(type(live[revision]) is int and 0 < live[revision] < 2**53
                    and all(inline_text(live[k]) for k in needed),
                    '这版的修订号、范围、计划或必填说明不全。')
        display_only = ('human_action', 'next_step') if acceptance else ('human_action', 'next_step', 'processing', 'summary')
        informational = {fields[k]['id'] for k in display_only if k in fields and k not in needed}
        entries = history(reader, token, table_id, record_id)
        match_history(event, entries, status['id'], people[0]['name'], informational, (before, after))
        require(live == current(), '核对期间这条记录又变了。')
        # Catch a write-and-revert during either read without scanning any other record.
        head = reader.base.read('+record-history-list', '--base-token', token, '--table-id', table_id,
                                '--record-id', record_id, '--page-size', '1')
        require(head.get('items') == entries[:1] and bridge.config_from(config_path) == config
                and bridge.load(paths[2]) == binding and control.load_config(config['runner_config']) == runner
                and read_receipt(config, binding, paths, table_id, record_id, status['id'], before, after) == event,
                '核对期间记录历史或接收器绑定有变化。')
        result.update(status='click_verified' if acceptance else 'verified', table_id=table_id, field_id=status['id'],
                      project_record_id=scope['project_record_id'], business_revision=live[revision], event_id=event['event_id'],
                      operator_open_id=event['operator_open_id'], table_revision=event['event_revision'],
                      update_time=event['update_time'])
    except Pending as exc:
        result['reason'] = str(exc)
    except (OSError, KeyError, AttributeError, IndexError, TypeError, ValueError, sqlite3.Error, OnboardingError):
        result['reason'] = '未能完整核实助手身份、事件归属、字段或记录历史。'
    return result


def main():
    parser = argparse.ArgumentParser(description='只读核对一条事项的范围确认、计划确认或最终验收点击。')
    parser.add_argument('--config', required=True, type=Path, help='已配对的长连接服务配置')
    parser.add_argument('--role', required=True, choices=GATES)
    parser.add_argument('--record-id', required=True)
    parser.add_argument('--acceptance-click', type=int, metavar='REVISION',
                        help='只核对需求或 Bug 这一修订是否由指定验收人点击「已验收」')
    args = parser.parse_args()
    result = verify(args.config, args.role, args.record_id, acceptance_revision=args.acceptance_click)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('verified', 'click_verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
