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
from gate_events import option_names
from onboard import OnboardingError, link_target

REGISTRY = 'ai-node-rules.json'
ROLES = ('requirements', 'bugs', 'work')
STAGE_REQUEST = 'assistant_stage_request'
# These labels are the six commands shared by the project adapter and the registered rules.
STAGE_REQUESTS = {
    'req-start': ('requirements', '需求开始研发', '可研发', '研发中'),
    'req-accept': ('requirements', '需求发起验收', '研发中', '待验收'),
    'bug-start': ('bugs', 'Bug开始修复', '可研发', '研发中'),
    'bug-accept': ('bugs', 'Bug发起验收', '研发中', '待验收'),
    'work-start': ('work', '研发项开始执行', '可开始', '进行中'),
    'work-deliver': ('work', '研发项交付', '进行中', '已交付'),
}
REQUEST_FIELDS = {
    'req-start': ('work',), 'req-accept': ('work', 'acceptor', 'verification'),
    'bug-start': ('work', 'baseline', 'reproduction_tests'), 'bug-accept': ('work', 'acceptor', 'retest_tests'),
    'work-start': ('plan', 'scope', 'deliverable', 'verification_plan'), 'work-deliver': ('summary',),
}
LINK_ROLES = {'work': 'work', 'verification': 'verification', 'reproduction_tests': 'verification',
              'retest_tests': 'verification'}
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


def marker_watch(config, paths, table_id, table):
    """Select only this project's explicitly registered marker field and finite values."""
    if bridge.load(config['runner_config']).get('native_stage_mode') != 'registered_requests':
        return table
    try:
        registry = private_registry(config, paths)
        fields = {}
        for rule in registry.values():
            if rule.get('purpose') != STAGE_REQUEST or rule.get('table_id') != table_id:
                continue
            checked_rule(rule)
            guard = {'kind': 'project_link', 'field_id': table['project_field'], 'value': config['project_record_id']}
            if (rule['guard'] != guard or rule['authored_by'] != config['bot_open_id']
                    or table['fields'].get(rule['stage_field_id']) != 'status' or not rule.get('definition_sha256')):
                continue
            fields.setdefault(rule['marker_field_id'], set()).add(rule['marker_text'])
        if fields:
            return dict(table, fields={k: bridge.gate_events.ASSISTANT_MARKER for k in fields}, marker_values=fields)
    except (KeyError, OSError, ValueError, TypeError, AttributeError):
        pass  # Missing or invalid enrollment cannot create a marker receipt.
    return table


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
    requested = rule.get('purpose') == STAGE_REQUEST
    if requested:
        keys.update({'purpose', 'key', 'marker_text', 'table_name', 'title', 'nature_field_id', 'nature_option',
                     'processing_field_id', 'processing_option', 'authored_by', 'owner_match'})
    optional = {'definition_sha256', 'project_filter_mode'} if requested else {'definition_sha256', 'owner_match'}
    need(
        set(rule) - optional == keys and rule['role'] in (*ROLES, 'diagnostic'),
        '登记的规则不完整。',
    )
    if requested:
        need(
            (rule['role'], rule['marker_text'], rule['source']['name'], rule['target']['name'])
            == STAGE_REQUESTS.get(rule['key'])
            and rule['guard']['kind'] == 'project_link'
            and rule.get('project_filter_mode', 'trigger_project_id') == 'trigger_project_id'
            and bridge.nonempty(rule['table_name']) and bridge.nonempty(rule['title'])
            and re.fullmatch(r'ou_[A-Za-z0-9]+', rule['authored_by']),
            '须登记这张表对应的助手请求、起点、终点和所属项目。',
        )
        work = rule['role'] == 'work'
        need(
            rule['owner_match'] == ('bound_assistant' if work else 'none')
            and (rule['owner_field_id'] is not None) == work
            and rule['nature_option']['name'] == '当前工作' and rule['processing_option']['name'] == '正常',
            '须排除历史记录和异常事项，研发项还须归本助手负责。',
        )
    elif 'owner_match' in rule:
        need(
            rule['owner_match'] == 'bound_assistant' and rule['owner_field_id'] is not None,
            '精确核对助手时，须登记负责人的字段编号。',
        )
    need(
        re.fullmatch(r'wkf[A-Za-z0-9]+', rule['workflow_id'])
        and re.fullmatch(r'tbl[A-Za-z0-9]+', rule['table_id']),
        '需要新版工作流和表的真实编号。',
    )
    for k in (('source', 'target', 'nature_option', 'processing_option') if requested else ('source', 'target')):
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
    if requested:
        ids.extend([rule['nature_field_id'], rule['processing_field_id']])
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


def assistant_values(values, bot_open_id):
    need(isinstance(values, list) and len(values) == 1, '条件须精确指定一位助手。')
    person = values[0].get('value') if isinstance(values[0], dict) else None
    need(
        isinstance(bot_open_id, str) and re.fullmatch(r'ou_[A-Za-z0-9]+', bot_open_id)
        and isinstance(person, dict) and person.get('id') == bot_open_id,
        '条件中的账号与绑定的助手不符。',
    )
    expected_person = {'id': bot_open_id}
    if 'name' in person:
        need(bridge.nonempty(person['name']), '负责人显示名无效。')
        expected_person['name'] = person['name']
    return [dict(value_type='user', value=expected_person)]


def assistant_condition(conditions, field_name, bot_open_id):
    matches = [c for c in conditions if isinstance(c, dict) and c.get('field_name') == field_name]
    need(len(matches) == 1, '须有一个条件指定本行的助手负责人。')
    return condition(field_name, 'is', assistant_values(matches[0].get('value'), bot_open_id))


def check_request_branch(steps, trigger, action, bot_open_id):
    branch, delay = steps['IfElseBranch'], steps['Delay']
    need(
        trigger.get('next') == branch['id']
        and all('next' not in step for step in (branch, action, delay))
        and branch['children'] == {'links': [{'kind': 'if_true', 'to': action['id']},
                                             {'kind': 'if_false', 'to': delay['id']}]}
        and delay['data'] == {'duration': 1} and type(delay['data']['duration']) is int,
        '助手提交的请求只能更新本行；其他人提交时仅等待一分钟后结束。',
    )
    outer = branch['data'].get('condition', {}).get('conditions')
    need(isinstance(outer, list) and len(outer) == 1, '请求人判断只能使用一组条件。')
    inner = outer[0].get('conditions')
    need(isinstance(inner, list) and len(inner) == 1, '只允许判断触发行的本次修改人。')
    expected = dict(left_value=dict(value_type='ref', value='$.' + trigger['id'] + '.recordModifiedUser'),
                    operator='is', right_value=assistant_values(inner[0].get('right_value'), bot_open_id))
    need(branch['data'] == {'condition': {'conjunction': 'or',
         'conditions': [{'conjunction': 'and', 'conditions': [expected]}]}},
         '触发行的本次修改人须为项目绑定的助手。')


def check_graph(workflow, rule, table_name, names, *, bot_open_id=None):
    """Verify the entire registered graph, including the single permitted modifier branch."""
    checked_rule(rule)
    need(
        set(workflow)
        <= {'workflow_id', 'title', 'status', 'steps', 'create_time', 'creator_id', 'update_time', 'updater_id'}
        and workflow['workflow_id'] == rule['workflow_id']
        and workflow.get('status') in ('enabled', 'disabled'),
        '无法核实规则编号、启用状态或其他配置。',
    )
    steps = workflow['steps']
    requested = rule.get('purpose') == STAGE_REQUEST
    step_types = {'SetRecordTrigger', 'SetRecordAction'}
    if requested:
        step_types.update(('IfElseBranch', 'Delay'))
        need(rule['authored_by'] == bot_open_id and rule['table_name'] == table_name
             and rule['title'] == workflow.get('title'), '登记的助手、规则或表与现场不符。')
    need(
        isinstance(steps, list)
        and len(steps) == len(step_types) and {s['type'] for s in steps} == step_types
        and len({s['id'] for s in steps}) == len(steps),
        '步骤缺失、重复，或含有登记以外的动作。',
    )
    by_type = {s['type']: s for s in steps}
    trigger, action = by_type['SetRecordTrigger'], by_type['SetRecordAction']
    for step in steps:
        need(
            set(step) <= {'id', 'type', 'title', 'children', 'data', 'next'}
            and bridge.nonempty(step['id'])
            and (step['type'] == 'IfElseBranch' or step.get('children') == {'links': []}),
            '规则存在分支或未核实的设置。',
        )
    if requested:
        need(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', trigger['id']),
             '无法把修改人引用唯一对应到触发步骤。')
        check_request_branch(by_type, trigger, action, bot_open_id)
    else:
        need(trigger.get('next') == action['id'] and 'next' not in action, '触发后须直接更新本行，并在这里结束。')
    td, ad = trigger['data'], action['data']
    marker = names[rule['marker_field_id']]
    watched = (condition(marker, 'is', [dict(value_type='text', value=rule['marker_text'])])
               if requested else {'field_name': marker})
    need(
        set(td) == TRIGGER_FIELDS
        and td['table_name'] == table_name
        and td['field_watch_info'] == [watched],
        '助手请求须在字段监听条件中精确匹配新填的文字。' if requested else '只能监听指定表的一个触发字段。',
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
    if requested:
        for key in ('nature', 'processing'):
            expected.append(condition(names[rule[key + '_field_id']], 'is',
                                      [dict(value_type='option', value=rule[key + '_option'])]))
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
    if rule.get('purpose') == STAGE_REQUEST:
        required = set(REQUEST_FIELDS[rule['key']]) | set(step['required_fields'])
        need(
            set(rule['required_field_ids']) == {mapped[k]['id'] for k in required}
            and step['person_field'] == ('owner' if rule['role'] == 'work' else None),
            '这一步的负责人或必填项与原项目对应步骤不符。',
        )


def check_request_fields(rule, binding, live, raw):
    mapped = binding['tables'][rule['role']]['fields']
    stage = rule['stage_field_id']
    native_stage_inspection.inspect_field(mapped['status'], mapped, live[stage], raw[stage])
    for source in (mapped['status'], live[stage], raw[stage].get('property') or {}):
        options = source.get('options') or []
        if any(isinstance(o, dict) and 'id' in o for o in options):
            choices = option_names(options)
            need(all(choices.get(rule[k]['id']) == rule[k]['name'] for k in ('source', 'target')),
                 '本表的流程选项编号与登记不一致。')
    marker = rule['marker_field_id']
    need(
        live[marker]['name'] == '助手请求'
        and all(field['id'] != marker for key, field in mapped.items() if key != 'assistant_request')
        and ('assistant_request' not in mapped or mapped['assistant_request']['id'] == marker),
        '请使用本表单独的助手请求字段。',
    )
    for key in ('nature', 'processing'):
        fid, option = rule[key + '_field_id'], rule[key + '_option']
        need(
            mapped[key]['id'] == fid and mapped[key]['type'] == live[fid].get('type') == 'select'
            and live[fid].get('multiple') is False and raw[fid].get('type') == 3
            and raw[fid].get('ui_type') == 'SingleSelect'
            and option_names(live[fid]['options']).get(option['name']) == option['name']
            and option_names(raw[fid]['property']['options']).get(option['id']) == option['name'],
            '记录性质或处理情况没有使用本表正确的单选值。',
        )
    for key, field in mapped.items():
        fid = field['id']
        if fid not in rule['required_field_ids']:
            continue
        if key in LINK_ROLES:
            target = binding['tables'][LINK_ROLES[key]]['id']
            need(field.get('target_table_id') == target and field['type'] == live[fid]['type'] == 'link'
                 and link_target(live[fid]) == target and raw[fid]['type'] in (18, 21)
                 and raw[fid]['property']['table_id'] == target, '必填关联没有指向约定的表。')
        elif key == 'acceptor':
            need(field['type'] == live[fid]['type'] == 'user' and live[fid].get('multiple') is False
                 and raw[fid]['type'] == 11, '验收人须使用已绑定的单人人员字段。')
        else:
            need(field['type'] == live[fid]['type'] == 'text' and raw[fid]['type'] == 1,
                 '尚未核实这项必填证据的字段类型。')


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
    requested = rule.get('purpose') == STAGE_REQUEST
    if requested:
        structure = len(steps) == 4 and {s['type'] for s in steps} == {
            'SetRecordTrigger', 'SetRecordAction', 'IfElseBranch', 'Delay'}
    else:
        structure = len(steps) == 2 and all(
            s['type'] in ('SetRecordTrigger', 'SetRecordAction', 'FindRecordAction') for s in steps)
    need(
        structure and len(triggers) == 1
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
        requested = rule.get('purpose') == STAGE_REQUEST
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
            if requested:
                check_request_fields(rule, binding, by_id, raw)
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
        if requested:
            project_fid = rule['guard']['field_id']
            result.update(
                project_condition_verified=scope_verified,
                project_scope_verified=scope_verified and by_id[project_fid].get('multiple') is False
                    and raw[project_fid]['property'].get('multiple') is False,
                trigger_modifier_verified=True,
                new_marker_value_verified=True,
                business_evidence_checked=False,
            )
        if scope_only:
            result.update(check='trigger_project_scope', action_checked=False)
            return result
        result.update(
            check=STAGE_REQUEST if requested else 'ai_node_rule',
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
