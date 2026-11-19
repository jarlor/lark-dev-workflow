#!/usr/bin/env python3
"""Inspect a registered Feishu workflow using only official read calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import event_bridge as bridge
import native_stage_inspection
import stage_contract
import unattended_control as control
import watch
from onboard import OnboardingError, link_target

REGISTRY = 'ai-node-rules.json'
ROLES = ('requirements', 'bugs', 'work')
CONTROLS = {'pasteUpdate', 'automationBatchUpdate', 'openAPIBatchUpdate'}
TRIGGER_FIELDS = {'table_name', 'field_watch_info', 'record_watch_info', 'trigger_control_list'}


class Unverified(ValueError):
    pass


def need(test, reason):
    if not test:
        raise Unverified(reason)


def service_pins(config, paths):
    return dict(
        service=bridge.journal_identity(config), runner=bridge.writer_identity(config['runner_config'], paths)
    )


def private_registry(config, paths):
    folder = Path(config['state_dir'])
    path = folder / REGISTRY
    for entry, kind in ((folder, stat.S_ISDIR), (path, stat.S_ISREG)):
        info = entry.lstat()
        need(
            kind(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            '请使用本服务账号的私有规则清单。',
        )
    data = bridge.load(path)
    need(
        all(data.get(k) == v for k, v in service_pins(config, paths).items()),
        '规则清单与当前服务或执行器不符。',
    )
    return data['rules']


def checked_rule(rule):
    if rule.get('purpose') == 'project_scope':
        need(
            set(rule) == {'purpose', 'role', 'workflow_id', 'table_id', 'guard'}
            and rule['role'] in ROLES
            and re.fullmatch(r'wkf[A-Za-z0-9]+', rule['workflow_id'])
            and re.fullmatch(r'tbl[A-Za-z0-9]+', rule['table_id']),
            '只读项目条件检查需要固定规则和项目表。',
        )
        guard = rule['guard']
        need(
            set(guard) == {'kind', 'field_id', 'value'}
            and guard['kind'] == 'project_link'
            and re.fullmatch(r'fld[A-Za-z0-9]+', guard['field_id'])
            and re.fullmatch(r'rec[A-Za-z0-9]+', guard['value']),
            '只读范围条件需要项目关联字段和真实项目记录。',
        )
        return [guard['field_id']]
    keys = {
        'workflow_id',
        'role',
        'table_id',
        'stage_field_id',
        'marker_field_id',
        'source',
        'target',
        'guard',
        'required_field_ids',
        'owner_field_id',
    }
    need(
        set(rule) - {'definition_sha256', 'owner_match'} == keys and rule['role'] in (*ROLES, 'diagnostic'),
        '登记的规则不完整。',
    )
    if 'owner_match' in rule:
        need(
            rule['owner_match'] == 'bound_assistant' and rule['owner_field_id'] is not None,
            '精确核对助手时，须登记负责人的字段编号。',
        )
    need(
        re.fullmatch(r'wkf[A-Za-z0-9]+', rule['workflow_id'])
        and re.fullmatch(r'tbl[A-Za-z0-9]+', rule['table_id']),
        '需要新版工作流和表的真实编号。',
    )
    for k in ('source', 'target'):
        opt = rule[k]
        need(
            set(opt) == {'id', 'name'}
            and re.fullmatch(r'opt[A-Za-z0-9]{1,64}', opt['id'])
            and bridge.nonempty(opt['name']),
            '须固定真实节点编号和名称。',
        )
    need(all(rule['source'][k] != rule['target'][k] for k in ('id', 'name')), '起点和终点必须不同。')
    guard = rule['guard']
    need(
        set(guard) == {'kind', 'field_id', 'value'}
        and guard['kind'] in ('text', 'project_link')
        and bridge.nonempty(guard['value']),
        '尚未核实这种范围条件的线上格式。',
    )
    need(isinstance(rule['required_field_ids'], list), '必填字段须按编号登记。')
    ids = [rule['stage_field_id'], rule['marker_field_id'], guard['field_id'], *rule['required_field_ids']]
    if rule['owner_field_id'] is not None:
        ids.append(rule['owner_field_id'])
    need(
        all(isinstance(i, str) and re.fullmatch(r'fld[A-Za-z0-9]+', i) for i in ids)
        and len(set(ids)) == len(ids),
        '触发、状态、范围、负责人和必填字段需要各自唯一的真实编号。',
    )
    return ids


def unique_name(rows, identifier):
    matches = [r['name'] for r in rows if r['id'] == identifier]
    need(len(matches) == 1 and bridge.nonempty(matches[0]), '登记的表或字段不存在。')
    name = matches[0]
    need(
        sum(r['name'].strip().casefold() == name.casefold() for r in rows) == 1,
        '飞书按名称返回配置，同名的表或字段无法分辨。',
    )
    return name


def condition(field_name, operator, values):
    return dict(field_name=field_name, operator=operator, value=values)


def fingerprint(workflow):
    return hashlib.sha256(
        json.dumps(workflow['steps'], sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def assistant_condition(conditions, field_name, bot_open_id):
    matches = [c for c in conditions if isinstance(c, dict) and c.get('field_name') == field_name]
    need(len(matches) == 1, '须有一个条件指定本行的助手负责人。')
    values = matches[0].get('value')
    need(isinstance(values, list) and len(values) == 1, '负责人条件须精确指定一位助手。')
    person = values[0].get('value') if isinstance(values[0], dict) else None
    need(
        isinstance(bot_open_id, str) and re.fullmatch(r'ou_[A-Za-z0-9]+', bot_open_id)
        and isinstance(person, dict) and person.get('id') == bot_open_id,
        '负责人的 open_id 与绑定的助手不符。',
    )
    expected_person = {'id': bot_open_id}
    if 'name' in person:
        need(bridge.nonempty(person['name']), '负责人显示名无效。')
        expected_person['name'] = person['name']
    return condition(field_name, 'is', [dict(value_type='user', value=expected_person)])


def check_graph(workflow, rule, table_name, names, *, bot_open_id=None):
    """Reject additional branches, writes, dynamic field values, filters, and unknown execution settings."""
    checked_rule(rule)
    need(
        set(workflow)
        <= {'workflow_id', 'title', 'status', 'steps', 'create_time', 'creator_id', 'update_time', 'updater_id'}
        and workflow['workflow_id'] == rule['workflow_id']
        and workflow.get('status') in ('enabled', 'disabled'),
        '无法核实规则编号、启用状态或其他配置。',
    )
    steps = workflow['steps']
    need(
        isinstance(steps, list)
        and len(steps) == 2
        and {s['type'] for s in steps} == {'SetRecordTrigger', 'SetRecordAction'},
        '规则必须只有一次触发和一个更新本行的动作。',
    )
    trigger, action = sorted(steps, key=lambda s: s['type'] != 'SetRecordTrigger')
    for step in steps:
        need(
            set(step) <= {'id', 'type', 'title', 'children', 'data', 'next'}
            and bridge.nonempty(step['id'])
            and step.get('children') == {'links': []},
            '规则存在分支或未核实的设置。',
        )
    need(
        trigger['id'] != action['id'] and trigger.get('next') == action['id'] and 'next' not in action,
        '触发后须直接更新本行，并在这里结束。',
    )
    td, ad = trigger['data'], action['data']
    need(
        set(td) == TRIGGER_FIELDS
        and td['table_name'] == table_name
        and td['field_watch_info'] == [{'field_name': names[rule['marker_field_id']]}],
        '只能监听指定表的一个触发字段。',
    )
    controls = td['trigger_control_list']
    need(
        isinstance(controls, list)
        and len(set(controls)) == len(controls)
        and 'openAPIBatchUpdate' in controls
        and set(controls) <= CONTROLS,
        '触发来源无法核实。',
    )
    stage, guard = names[rule['stage_field_id']], rule['guard']
    expected = [
        condition(names[guard['field_id']], 'is', [dict(value_type='text', value=guard['value'])]),
        condition(stage, 'is', [dict(value_type='option', value=rule['source'])]),
    ]
    expected.extend(condition(names[fid], 'isNotEmpty', []) for fid in rule['required_field_ids'])
    filters = td['record_watch_info']
    need(
        set(filters) == {'conjunction', 'conditions'}
        and filters['conjunction'] == 'and'
        and isinstance(filters['conditions'], list),
        '所有条件必须同时满足。',
    )
    if rule['owner_field_id']:
        owner = names[rule['owner_field_id']]
        if rule.get('owner_match') == 'bound_assistant':
            expected.append(assistant_condition(filters['conditions'], owner, bot_open_id))
        else:
            expected.append(condition(owner, 'isNotEmpty', []))
    key = lambda item: json.dumps(item, sort_keys=True)
    need(
        sorted(map(key, filters['conditions'])) == sorted(map(key, expected)),
        '范围、起点或必填条件与登记不符。',
    )
    values = [{'field_name': stage, 'value': [dict(value_type='option', value=rule['target'])]}]
    need(
        set(ad) == {'table_name', 'field_values', 'ref_info', 'filter_info', 'max_set_record_num'}
        and ad['table_name'] == table_name
        and ad['field_values'] == values
        and ad['ref_info'] == {'step_id': trigger['id']}
        and ad['filter_info'] is None
        and type(ad['max_set_record_num']) is int
        and 0 < ad['max_set_record_num'] <= 100,
        '动作必须只改触发的那一行，只把状态改为指定的下一节点。',
    )


def source_step(rule, mapped):
    status = mapped['status']
    contract = status['stage_contract']
    need(
        status['id'] == rule['stage_field_id']
        and status['type'] == 'stage'
        and not stage_contract.binding_problems(contract, mapped),
        '缺少原项目的流程约定。',
    )
    steps = contract['steps']
    names = [s['name'] for s in steps] + [contract['done']]
    need(rule['source']['name'] in names[:-1], '不是项目约定中的活动节点。')
    i = names.index(rule['source']['name'])
    step = steps[i]
    need(
        step['actor'] == 'ai' and rule['target']['name'] == names[i + 1],
        'AI 只能从自己的节点进入紧接的下一步。',
    )
    owner = mapped[step['person_field']]['id'] if step['person_field'] else None
    need(
        rule['owner_field_id'] == owner
        and {mapped[k]['id'] for k in step['required_fields']} <= set(rule['required_field_ids']),
        '遗漏或改变了原节点的负责人或必填要求。',
    )


def live_workflow(reader, token, identifier):
    response = reader.runner(
        [
            'lark-cli',
            'base',
            '+workflow-get',
            '--base-token',
            token,
            '--workflow-id',
            identifier,
            '--as',
            'bot',
            '--profile',
            reader.profile,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    envelope = json.loads(response.stdout)
    need(
        response.returncode == 0
        and envelope.get('ok') is True
        and envelope.get('identity') == 'bot'
        and isinstance(envelope.get('data'), dict),
        '专用助手无法读回这条新版工作流。',
    )
    return envelope['data']


def project_condition(reader, token, binding, rule, table_name, fields, raw, workflow):
    guard = rule['guard']
    if guard['kind'] != 'project_link':
        return False
    scope = binding['scope']
    projects = binding['tables']['projects']
    project = guard['value']
    need(
        rule['role'] in ROLES
        and scope['mode'] == 'project_record'
        and projects['id'] == scope['project_table_id']
        and project == scope['project_record_id']
        and binding['tables'][rule['role']]['fields']['project']['id'] == guard['field_id'],
        '条件没有使用本项目已绑定的关联字段与记录。',
    )
    linked, original = fields[guard['field_id']], raw[guard['field_id']]
    need(
        linked['type'] == 'link'
        and link_target(linked) == projects['id']
        and original.get('type') in (18, 21)
        and (original.get('property') or {}).get('table_id') == projects['id'],
        '该字段不是通往项目表的真实关联。',
    )
    steps = workflow['steps']
    triggers = [s for s in steps if s.get('type') == 'SetRecordTrigger']
    need(
        len(steps) == 2
        and len(triggers) == 1
        and all(s['type'] in ('SetRecordTrigger', 'SetRecordAction', 'FindRecordAction') for s in steps)
        and set(triggers[0]['data']) == TRIGGER_FIELDS
        and triggers[0]['data'].get('table_name') == table_name,
        '无法确定唯一的项目触发器。',
    )
    filters = triggers[0]['data']['record_watch_info']
    need(
        set(filters) == {'conjunction', 'conditions'}
        and filters['conjunction'] == 'and'
        and isinstance(filters['conditions'], list),
        '项目条件必须与其他触发条件同时满足。',
    )
    expected = condition(linked['name'], 'is', [dict(value_type='text', value=project)])
    need(
        [c for c in filters['conditions'] if c.get('field_name') == linked['name']] == [expected],
        '规则的项目条件没有精确匹配绑定的项目记录编号。',
    )
    number = binding.get('project_number')
    field = projects['fields']['number' if number else 'title']
    row = reader.read_record(token, projects['id'], project, [field])
    need(
        row['record_id'] == project
        and field['id'] in row['values']
        and (not number or row['values'][field['id']] == number),
        '没有读回对应的真实项目记录或编号。',
    )
    return True


def verify(config_path, rule_name, *, reader_factory=watch.Cli):
    result = dict(
        status='pending',
        enabled=None,
        definition_matches=False,
        updates_trigger_row_only=False,
        project_scope_verified=False,
        fixed_bot_owner_verified=False,
        dispatch_checked=False,
    )
    try:
        config = bridge.config_from(config_path)
        paths = bridge.runner_pins(config)
        rule = private_registry(config, paths)[rule_name]
        ids = checked_rule(rule)
        scope_only = rule.get('purpose') == 'project_scope'
        runner = control.load_config(config['runner_config'])
        binding = bridge.load(paths[2])
        actor = control.actor_for_runner(runner, binding)

        def run_cli(argv, **kwargs):
            kwargs['env'] = dict(
                kwargs.get('env', os.environ),
                PATH=runner['path'],
                LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1',
            )
            return subprocess.run(argv, **kwargs)

        reader = reader_factory(actor['profile'], runner=run_cli, executable=runner['lark_bin'], actor=actor)
        token, table = config['base_token'], rule['table_id']
        bound = rule['role'] in ROLES
        if bound:
            need(
                table in config['table_ids'] and binding['tables'][rule['role']]['id'] == table,
                '项目绑定或服务清单没有这张表。',
            )
            if not scope_only:
                source_step(rule, binding['tables'][rule['role']]['fields'])
        tables = reader.base.pages('+table-list', 'tables', '--base-token', token)
        table_name = unique_name(tables, table)
        fields = reader.fields(token, table)
        names = {fid: unique_name(fields, fid) for fid in ids}
        by_id = {f['id']: f for f in fields}
        raw = native_stage_inspection.raw_fields(reader, token, table)
        definition = live_workflow(reader, token, rule['workflow_id'])
        if definition.get('workflow_id') == rule['workflow_id'] and definition.get('status') in (
            'enabled',
            'disabled',
        ):
            result['enabled'] = definition['status'] == 'enabled'
        need(result['enabled'] is not None, '读回的规则编号或启停状态不符。')
        scope_verified = project_condition(reader, token, binding, rule, table_name, by_id, raw, definition)
        if not scope_only:
            stage = rule['stage_field_id']
            need(
                raw[stage].get('type') == 24
                and raw[stage].get('ui_type') == 'Stage'
                and by_id[stage].get('type') in ('select', 'stage'),
                '没有核实状态字段为原生流程。',
            )
            need(
                raw[rule['marker_field_id']].get('type') == 1
                and by_id[rule['marker_field_id']].get('type') == 'text',
                '触发标记须为独立的文字字段。',
            )
            opt_names = [x.get('name') if isinstance(x, dict) else x for x in by_id[stage].get('options', [])]
            need(
                all(opt_names.count(rule[k]['name']) == 1 for k in ('source', 'target')),
                '现场起点或终点名称无法唯一确定。',
            )
            need(
                opt_names.index(rule['target']['name']) == opt_names.index(rule['source']['name']) + 1,
                '现场状态选项中，终点不再是起点的下一步。',
            )
            if rule['guard']['kind'] == 'text':
                need(by_id[rule['guard']['field_id']].get('type') == 'text', '登记的文字范围字段类型已变化。')
            owner = rule['owner_field_id']
            owner_match = rule.get('owner_match', 'nonempty_only' if owner else 'none')
            if owner:
                need(by_id[owner].get('type') == 'user', '负责人条件未使用实际人员字段。')
                if owner_match == 'bound_assistant':
                    native_owner = raw[owner]
                    need(
                        by_id[owner].get('multiple') is False and native_owner.get('type') == 11
                        and native_owner.get('ui_type', 'User') == 'User'
                        and (native_owner.get('property') or {}).get('multiple', False) is False,
                        '负责人须为只允许选一人的人员字段。',
                    )
            check_graph(definition, rule, table_name, names, bot_open_id=actor['open_id'])
            digest = fingerprint(definition)
            result['definition_sha256'] = digest
            if 'definition_sha256' in rule:
                need(rule['definition_sha256'] == digest, '规则定义与该服务固定的版本不符。')
        if live_workflow(reader, token, rule['workflow_id']) != definition:
            result['enabled'] = None
            raise Unverified('核查期间飞书里的规则发生变化。')
        need(
            bridge.config_from(config_path) == config
            and bridge.runner_pins(config) == paths
            and control.load_config(config['runner_config']) == runner
            and bridge.load(paths[2]) == binding
            and private_registry(config, paths).get(rule_name) == rule,
            '核查期间规则或绑定已变化。',
        )
        result.update(
            status='checked',
            workflow_id=rule['workflow_id'],
            table_id=table,
            project_scope_verified=scope_verified,
            scope_condition=rule['guard']['kind'],
        )
        if scope_only:
            result.update(check='trigger_project_scope', action_checked=False)
            return result
        result.update(
            check='ai_node_rule',
            action_checked=True,
            definition_matches=True,
            updates_trigger_row_only=True,
            source_ai_in_project_contract=True if bound else None,
            source=rule['source'],
            target=rule['target'],
            required_nonempty_field_ids=rule['required_field_ids'],
            owner_condition=owner_match,
            fixed_bot_owner_verified=owner_match == 'bound_assistant',
            definition_pinned='definition_sha256' in rule,
        )
    except Unverified as exc:
        result['reason'] = str(exc)
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OnboardingError,
        subprocess.SubprocessError,
    ):
        result['reason'] = '未能完整核实规则、字段或专用助手。'
    return result


def main():
    parser = argparse.ArgumentParser(description='只读核对新版工作流配置')
    parser.add_argument('--config', required=True, type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--rule')
    choice.add_argument('--show-binding', action='store_true')
    args = parser.parse_args()
    if args.show_binding:
        config = bridge.config_from(args.config)
        print(json.dumps(service_pins(config, bridge.runner_pins(config))))
        return 0
    result = verify(args.config, args.rule)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'checked' else 1


if __name__ == '__main__':
    raise SystemExit(main())
