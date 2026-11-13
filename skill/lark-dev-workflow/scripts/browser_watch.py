#!/usr/bin/env python3
"""Read the logged-in browser through the bundled, read-only collector.

Browser state remains in the browser. No cookies, tokens or application settings
are accepted here. Partial native-page data never becomes a scheduling checkpoint.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import comments
import watch
from onboard import OnboardingError

HERE = Path(__file__).resolve().parent
TYPE_NAMES = {1: 'text', 2: 'number', 3: 'select', 4: 'select', 5: 'datetime',
              7: 'checkbox', 11: 'user', 15: 'text', 17: 'attachment', 18: 'link',
              21: 'link', 24: 'stage', 1001: 'datetime', 1002: 'datetime', 1005: 'auto_number'}


def fail(message):
    raise watch.WatchError(message)


def field(raw):
    if not isinstance(raw, dict) or not comments.nonempty(raw.get('id')):
        fail('网页字段缺少真实编号。')
    kind = TYPE_NAMES.get(raw.get('type'))
    if kind is None:
        fail('网页字段类型尚不支持：' + str(raw.get('type')))
    result = {'id': raw['id'], 'name': raw.get('name'), 'type': kind}
    if raw['type'] == 15:
        # Lark's shared field contract represents a hyperlink as text + style.
        result['style'] = {'type': 'url'}
    prop = raw.get('property') or {}
    if not isinstance(prop, dict):
        fail('网页字段属性不完整。')
    if kind == 'link':
        target = prop.get('tableId')
        if not comments.nonempty(target):
            fail('网页关联字段缺少目标表编号。')
        result['link_table'] = {'id': target}
    if kind in ('select', 'stage'):
        options = prop.get('options')
        if not isinstance(options, list):
            fail('网页选项未完整返回。')
        ids, names = set(), set()
        for option in options:
            if (not isinstance(option, dict) or not comments.nonempty(option.get('id'))
                    or not comments.nonempty(option.get('name')) or option['id'] in ids
                    or option['name'] in names):
                fail('网页选项缺少编号、名称或发生重复。')
            ids.add(option['id']); names.add(option['name'])
        result.update(multiple=raw['type'] == 4, options=copy.deepcopy(options))
        if kind == 'stage':
            if raw.get('fieldUIType') != 'Stage':
                fail('网页流程字段缺少 Stage 类型标记。')
            if any(option.get('type') not in ('Default', 'EndDone', 'EndCancel')
                   or not isinstance(option.get('transitionRule'), dict)
                   or not isinstance(option.get('fieldsConfigInfo'), list) for option in options):
                fail('网页流程节点的完成条件或结束类型尚未核实。')
            result['stage_config'] = {'property': copy.deepcopy(prop)}
            if 'defaultValueRule' in raw:
                result['stage_config']['defaultValueRule'] = copy.deepcopy(raw['defaultValueRule'])
    return result


def cell_value(raw, schema):
    if raw is None:
        return None
    if not isinstance(raw, dict) or 'value' not in raw:
        fail('网页单元格未返回实际值。')
    value = raw['value']
    if value is None:
        return None
    kind = schema['type']
    if kind == 'text':
        if isinstance(value, str):
            return value
        if schema.get('style') == {'type': 'url'} and isinstance(value, dict) and isinstance(value.get('link'), str):
            return copy.deepcopy(value)
        if isinstance(value, list) and all(isinstance(x, dict) and isinstance(x.get('text'), str) for x in value):
            if all(set(x) <= {'type', 'text'} and x.get('type', 'text') == 'text' for x in value):
                return ''.join(x['text'] for x in value)
            # A link target or mention may change while the visible label stays
            # identical. Keep structured content in the change snapshot.
            return copy.deepcopy(value)
    elif kind in ('select', 'stage'):
        selected = [value] if isinstance(value, str) else value
        choices = {x['id']: x['name'] for x in schema['options']}
        if (isinstance(selected, list) and all(isinstance(x, str) and x in choices for x in selected)
                and len(selected) == len(set(selected)) and (schema['multiple'] or len(selected) <= 1)):
            return [choices[x] for x in selected]
    elif kind == 'link':
        if isinstance(value, list) and all(comments.nonempty(x) for x in value) and len(value) == len(set(value)):
            return [{'id': x} for x in value]
    elif kind == 'auto_number':
        if value == []:
            return None
        if (isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict)
                and comments.nonempty(value[0].get('number'))):
            return value[0]['number']
    elif kind in ('number', 'datetime'):
        if type(value) in (int, float):
            return value
    elif kind == 'checkbox':
        if isinstance(value, bool):
            return value
        if type(value) is int and value in (0, 1):
            return bool(value)
    elif kind == 'user':
        if isinstance(value, dict) and isinstance(value.get('users'), list):
            value = [{'id': x.get('userId'), **({'name': x['name']} if 'name' in x else {})}
                     if isinstance(x, dict) else x for x in value['users']]
        if isinstance(value, list):
            out = []
            for user in value:
                if isinstance(user, str) and user:
                    out.append({'id': user})
                elif isinstance(user, dict) and comments.nonempty(user.get('id')):
                    out.append({k: user[k] for k in ('id', 'name') if k in user})
                else:
                    fail('网页人员值缺少稳定用户编号。')
            return out
    elif kind == 'attachment':
        if isinstance(value, list):
            out = []
            for item in value:
                if not isinstance(item, dict):
                    fail('网页附件值不是对象。')
                identifier = next((item[k] for k in ('fileToken', 'file_token', 'token', 'id')
                                   if comments.nonempty(item.get(k))), None)
                if not identifier:
                    fail('网页附件缺少稳定文件编号。')
                out.append({'file_id': identifier, **{k: item[k] for k in ('name', 'size', 'mimeType', 'mime_type') if k in item}})
            return out
    fail('网页单元格格式尚未核实：' + schema['id'] + ' (' + kind + ')')


class Reader:
    """A fully checked in-memory view; reuse existing project/comment semantics."""
    profile = None

    def __init__(self, config, access, payload):
        self.config, self.token = config, config['base']['token']
        self.tables = {}
        if (not isinstance(payload, dict) or payload.get('format_version') != 1
                or payload.get('status') != 'complete'
                or payload.get('consistency') != 'two_complete_passes_equal'):
            fail('网页采集尚未完整，未推进处理进度。')
        if payload.get('user_id') != access['user_id'] or payload.get('base_token') != self.token:
            fail('网页当前账号或多维表格与绑定不一致。')
        tables = payload.get('tables')
        expected = {t['id'] for t in config['tables'].values()}
        if not isinstance(tables, dict) or set(tables) != expected:
            fail('网页未返回全部绑定表。')
        for table_id, data in tables.items():
            if not isinstance(data, dict) or data.get('stable') is not True:
                fail('网页读取期间表格有变化，稍后重试。')
            rows, schema = data.get('rows'), data.get('fields')
            total, loaded = data.get('total'), data.get('loaded')
            if (type(total) is not int or total < 0 or type(loaded) is not int or loaded != total
                    or not isinstance(rows, list) or len(rows) != total or not isinstance(schema, list)):
                fail('网页记录尚未加载完整，不能作为完整扫描。')
            normalized = [field(x) for x in schema]
            indexed = {x['id']: x for x in normalized}
            if len(indexed) != len(normalized):
                fail('网页字段编号重复。')
            mapped = next(t['fields'] for t in config['tables'].values() if t['id'] == table_id)
            if set(indexed) != {x['id'] for x in mapped.values()}:
                fail('网页字段投影不完整。')
            records = {}
            for row in rows:
                if (not isinstance(row, dict) or not comments.nonempty(row.get('record_id'))
                        or row['record_id'] in records or not isinstance(row.get('cells'), dict)
                        or set(row['cells']) != set(indexed)):
                    fail('网页记录编号或字段值不完整。')
                records[row['record_id']] = {fid: cell_value(row['cells'][fid], f) for fid, f in indexed.items()}
            self.tables[table_id] = {'fields': normalized, 'rows': records}
        comments_data = payload.get('comments')
        if (not isinstance(comments_data, dict) or comments_data.get('complete') is not True
                or comments_data.get('includes_resolved') is not True
                or comments_data.get('model_replies_complete') is not True
                or comments_data.get('method') != 'native_sdk_visible_model'
                or comments_data.get('scope') != 'current_accessible_sdk_index'
                or not isinstance(comments_data.get('threads'), list)):
            fail('网页评论是否读全还不能确认，未推进处理进度。')
        self.threads = comments_data['threads']
        self.replies = {}
        for thread in self.threads:
            cid = thread.get('comment_id') if isinstance(thread, dict) else None
            replies = thread.get('replies') if isinstance(thread, dict) else None
            if (not comments.nonempty(cid) or cid in self.replies or not isinstance(replies, list)
                    or not replies or not isinstance(thread.get('is_solved'), bool)):
                fail('网页评论缺少稳定编号、状态或完整回复。')
            self.replies[cid] = replies

    def _table(self, token, table_id):
        if token != self.token or table_id not in self.tables:
            fail('网页读取目标不在当前绑定中。')
        return self.tables[table_id]

    def fields(self, token, table_id):
        return copy.deepcopy(self._table(token, table_id)['fields'])

    def read_record(self, token, table_id, record_id, fields):
        table = self._table(token, table_id)
        if record_id not in table['rows']:
            fail('评论关联的记录未在已完整读取的表中找到。')
        values = table['rows'][record_id]
        return {'record_id': record_id, 'values': {f['id']: copy.deepcopy(values[f['id']]) for f in fields}}

    def records(self, token, table_id, fields, predicate):
        if not isinstance(predicate, dict) or set(predicate) != {'logic', 'conditions'} or predicate['logic'] != 'and':
            fail('网页读取不支持此筛选条件。')
        def matches(values):
            for condition in predicate['conditions']:
                fid, op, *arguments = condition
                if fid not in values:
                    fail('网页筛选字段未读取。')
                value = values[fid]
                if op == 'empty' and not arguments:
                    if value not in (None, []): return False
                elif op == 'intersects' and len(arguments) == 1 and isinstance(arguments[0], list):
                    if value is None: return False
                    if not isinstance(value, list): fail('网页筛选值不是列表。')
                    if not any(x in arguments[0] for x in value): return False
                else:
                    fail('网页读取不支持此筛选操作。')
            return True
        table = self._table(token, table_id)
        return [self.read_record(token, table_id, rid, fields)
                for rid, values in table['rows'].items() if matches(values)]

    def read(self, domain, command, *args):
        if domain != 'drive' or command not in ('+list-comments', '+list-replies'):
            fail('网页首版只读取 Base 及其评论，不支持此资源。')
        if '--page-token' in args:
            fail('网页内存快照没有后续页。')
        if '--url' in args and args[args.index('--url') + 1] != self.config['base']['url']:
            fail('网页评论目标与绑定不一致。')
        extra = {}
        if command == '+list-comments':
            if '--solved-status' not in args or args[args.index('--solved-status') + 1] != 'all':
                fail('网页完整扫描必须包含已解决评论。')
            items = [{k: copy.deepcopy(v) for k, v in t.items() if k != 'replies'} for t in self.threads]
        else:
            cid = args[args.index('--comment-id') + 1]
            if cid not in self.replies: fail('网页评论线程不存在。')
            items, extra = copy.deepcopy(self.replies[cid]), {'comment_id': cid}
        return {'file_token': self.token, 'file_type': 'bitable', 'items': items,
                'count': len(items), 'has_more': False, **extra}


def run_collector(argv, *, input, capture_output, text, timeout):
    """Let the owned-tab finally block run before collecting an expired child."""
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = child.communicate(input, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.terminate()
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)


def collect(config, access, *, comments_state_path, runner=run_collector):
    if config.get('documents'):
        fail('本人网页读取首版支持 Base；当前绑定含 Docx，不能跳过后继续。')
    watch._binding(config)
    request = {'base': {'token': config['base']['token'], 'url': config['base']['url']},
               'tables': [{'id': table['id'], 'field_ids': [f['id'] for f in table['fields'].values()]}
                          for table in config['tables'].values()],
               'cdp_url': access['cdp_url'], 'user_id': access['user_id']}
    try:
        result = runner([access['node_bin'], str(HERE / 'browser_collect.mjs')],
                        input=json.dumps(request, ensure_ascii=False), capture_output=True,
                        text=True, timeout=330)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail('浏览器读取未完成（' + type(exc).__name__ + '），未推进处理进度。')
    try:
        envelope = json.loads(result.stdout)
    except (ValueError, TypeError):
        fail('浏览器未返回完整的读取结果。')
    if result.returncode or not isinstance(envelope, dict) or envelope.get('status') != 'complete':
        # Only a fixed collector classification is trusted; page text is never an instruction.
        code = envelope.get('code') if isinstance(envelope, dict) else None
        messages = {'LOGIN_REQUIRED': '飞书网页登录已失效，需要本人重新登录。',
                    'ACCOUNT_MISMATCH': '飞书当前账号已改变，已停止读取。',
                    'PERMISSION_DENIED': '飞书页面拒绝访问，已停止读取。',
                    'COMMENTS_INCOMPLETE': '尚不能确认网页评论读全，已停止本轮。',
                    'RECORDS_INCOMPLETE': '网页记录尚未加载完整，已停止本轮。',
                    'SNAPSHOT_CHANGED': '读取期间表格或评论有变化，本轮未提交，稍后重试。',
                    'PAGE_INTERFACE_CHANGED': '飞书网页的数据格式与已验证版本不同，本轮已停止。',
                    'NODE_VERSION': '本人网页读取需要 Node.js 22 或更新版本。',
                    'browser_navigation_blocked': '飞书跳转到了绑定页面之外；请检查本人登录，本轮已停止。',
                    'browser_connect_failed': '原有本人浏览器当前不可连接，本轮已停止。',
                    'browser_timeout': '网页读取超过本轮 5 分钟上限，已停止；原进度保留。',
                    'browser_cleanup_failed': '无法确认本轮临时标签已关闭；保留原进度，请核对浏览器。',
                    'BROWSER_UNAVAILABLE': '未连接到原有本人浏览器，需要恢复该浏览器。'}
        fail(messages.get(code, '浏览器读取失败；查看本机检查结果，未推进处理进度。'))
    reader = Reader(config, access, envelope)
    snapshot = watch.collect_with_reader(
        config, reader, comments_state_path=comments_state_path,
        reader_identity={'mode': 'browser', 'user_id': access['user_id'], 'cdp_url': access['cdp_url']})
    snapshot['coverage']['access'] = 'personal_browser'
    snapshot['coverage']['comments'] = 'current_accessible_sdk_index_and_loaded_replies'
    snapshot['coverage']['consistency'] = 'two_complete_passes_equal'
    return snapshot
