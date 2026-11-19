"""Read the native field type and its visible CLI projection for inspection only."""
import json
import os
import re
import subprocess

from gate_events import option_names
from onboard import OnboardingError
from stage_contract import binding_problems


def raw_fields(cli, token, table):
    if cli.base.operator != 'bot' or any(not re.fullmatch(r'[A-Za-z0-9]+', s) for s in (token, table)):
        raise OnboardingError('请使用已绑定的助手账号和真实表格编号。')
    endpoint = f'/open-apis/bitable/v1/apps/{token}/tables/{table}/fields'
    fields, cursors = {}, set()
    params = {'page_size': 100}
    total = None
    while True:
        try:
            result = cli.runner(['lark-cli', 'api', 'GET', endpoint, '--as', 'bot', '--profile', cli.profile,
                                 '--params', json.dumps(params)], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                                         LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OnboardingError('读取原生字段清单失败，请用同一助手重试。') from exc
        data = cli.base._envelope(result, '读取原生流程字段')
        if json.loads(result.stdout).get('identity') != 'bot':
            raise OnboardingError('原生字段清单未确认由绑定的助手读取。')
        items = data.get('items')
        count, more = data.get('total'), data.get('has_more')
        if (type(count) is not int or count < 0 or total not in (None, count)
                or type(more) is not bool or not isinstance(items, list)):
            raise OnboardingError('原生字段清单不完整或在读取时有变化。')
        total = count
        for item in items:
            key = item.get('field_id') if isinstance(item, dict) else None
            if not isinstance(key, str) or not key or key in fields:
                raise OnboardingError('原生字段缺少编号或重复。')
            fields[key] = item
        if not more and len(fields) == total:
            return fields
        cursor = data.get('page_token')
        if not more or not items or len(fields) >= total or not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise OnboardingError('原生字段分页不完整。')
        cursors.add(cursor)
        params['page_token'] = cursor


def inspect_field(bound, mapped, actual, native):
    if (not actual or not native or type(native.get('type')) is not int or native['type'] != 24
            or native.get('ui_type') != 'Stage' or actual.get('type') not in ('select', 'stage')
            or actual.get('multiple') is not False or actual.get('id') != native.get('field_id')
            or actual['id'] != bound['id']):
        raise OnboardingError('无法同时核实同一字段的原生流程类型（24 / Stage）和单值投影。')
    contract = bound.get('stage_contract')
    issues = binding_problems(contract, mapped)
    if issues:
        raise OnboardingError(' '.join(issues))
    expected = [s['name'] for s in contract['steps']] + [contract['done'], contract['cancelled']]
    for source in (bound, actual):
        options = source.get('options')
        if (not isinstance(options, list)
                or [o.get('name') if isinstance(o, dict) else o for o in options] != expected):
            raise OnboardingError('实际状态、绑定和原流程约定的选项名称与顺序必须一致。')
    if not option_names(actual['options']):
        raise OnboardingError('当前流程的选项名称或编号无法唯一对应。')
    return {'native_type': 24, 'ui_type': 'Stage', 'cli_type': actual['type'], 'options': expected}
