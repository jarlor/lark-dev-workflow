#!/usr/bin/env python3
"""Create one draft plan for one confirmed requirement. The project worker owns write authorization."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

import event_bridge as bridge
import verify_confirmation as confirmation
import watch
from identity_handoff import sync_directory
from onboard import OnboardingError, atomic_json, option_names
import unattended_control as control


TEXT = ('plan', 'scope', 'deliverable', 'verification_plan')
WORK_TYPES = {**dict.fromkeys((*TEXT, 'title', 'key'), 'text'), 'nature': 'select', 'plan_revision': 'number',
              'plan_confirmer': 'user', 'project': 'link', 'requirements': 'link', 'feedback': 'link'}


class Conflict(ValueError):
    pass


def check(test, reason):
    if not test:
        raise Conflict(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def ids(value):
    if value is None:
        return []
    check(isinstance(value, list) and all(isinstance(e, dict) and isinstance(e.get('id'), str)
                                          and e['id'] for e in value), '关联或人员编号无法核实。')
    return [e['id'] for e in value]


def checked_draft(value):
    check(isinstance(value, dict) and set(value) == set(TEXT)
          and all(confirmation.inline_text(v) and len(v) <= 8000 for v in value.values()),
          '请提供计划、范围、交付物和验证方法四段完整说明。')
    return {k: v.strip() for k, v in value.items()}


def result(status, reason, record=None):
    return {'status': status, 'reason': reason, 'record_id': record,
            'plan_revision': 1 if status == 'prepared' else None}


class Project:
    def __init__(self, config_path, reader_factory=watch.Cli):
        self.path = config_path
        self.config = bridge.config_from(config_path)
        self.paths = bridge.runner_pins(self.config)
        self.runner = control.load_config(self.config['runner_config'])
        self.binding = bridge.load(self.paths[2])
        actor = control.actor_for_runner(self.runner, self.binding)
        check(self.config['mode'] == 'dispatch' and self.runner.get('native_stage_mode') == 'prepare'
              and self.runner.get('event_tenant_key') == self.config['tenant_key'],
              '项目执行器未启用已配对的反馈和计划模式。')
        scope, self.tables = watch._binding(self.binding)
        self.project = scope['project_record_id']
        self.token = self.binding['base']['token']
        self.open_id = actor['open_id']
        self.signature = digest([bridge.journal_identity(self.config),
                                 bridge.writer_identity(self.config['runner_config'], self.paths), self.binding])
        self.directory = Path(self.config['state_dir'])
        mode = self.directory.lstat()
        check(stat.S_ISDIR(mode.st_mode) and mode.st_uid == os.geteuid() and not mode.st_mode & 0o077,
              '计划写入记录需要服务账号自己的私有目录。')
        def run(argv, **kwargs):
            kwargs['env'] = dict(kwargs.get('env', os.environ), PATH=self.runner['path'])
            return subprocess.run(argv, **kwargs)
        self.reader = reader_factory(actor['profile'], runner=run, executable=self.runner['lark_bin'], actor=actor)
        self.live = {}

    def check_fields(self):
        for role in ('projects', 'feedback', 'requirements', 'work'):
            table = self.tables[role]
            check(table['id'] in self.config['table_ids'] or role == 'projects', '服务没有绑定这张事项表。')
            self.live[role] = watch._fields(self.reader, self.binding, role, table, inspected={})
        work = self.tables['work']['fields']
        for key, kind in WORK_TYPES.items():
            check(work[key]['type'] == kind, '研发项写入字段与项目约定不符。')
        step = work['status']['stage_contract']['steps'][0]
        check(work['status']['type'] == 'stage' and step['name'] == '待确认计划'
              and step['actor'] == 'human' and step['person_field'] == 'plan_confirmer'
              and set(step['required_fields']) == set(TEXT),
            '研发项没有绑定原生计划确认节点及四项必填说明。')
        req = self.tables['requirements']['fields']
        for role, key, target, reverse in [('requirements', 'work', 'work', 'requirements'),
                                           ('work', 'requirements', 'requirements', 'work'),
                                           ('requirements', 'feedback', 'feedback', 'requirements'),
                                           ('feedback', 'requirements', 'requirements', 'feedback'),
                                           ('feedback', 'work', 'work', 'feedback'), ('work', 'feedback', 'feedback', 'work')]:
            field = self.tables[role]['fields'][key]
            actual = self.live[role][field['id']]
            check(field['target_table_id'] == self.tables[target]['id']
                  and actual.get('bidirectional_link_field_id') == self.tables[target]['fields'][reverse]['id'],
                  '事项之间的双向关联未能核实。')
        check(req['status']['type'] == 'stage' and '当前工作' in option_names(self.live['work'][work['nature']['id']]),
              '需求状态或研发项记录性质不符。')

    def unchanged(self):
        check(bridge.config_from(self.path) == self.config and bridge.runner_pins(self.config) == self.paths
              and control.load_config(self.config['runner_config']) == self.runner
              and bridge.load(self.paths[2]) == self.binding and not (Path(self.paths[1]) / 'PAUSED').exists(),
              '服务、项目绑定或暂停状态已改变。')

    def row(self, role, record):
        table = self.tables[role]
        raw = self.reader.read_record(self.token, table['id'], record, list(table['fields'].values()))
        check(raw['record_id'] == record, '读回的事项编号不符。')
        return {k: raw['values'][f['id']] for k, f in table['fields'].items()}

    def source(self, record, revision):
        req = self.row('requirements', record)
        if req['status'] == ['待确认']:
            raise confirmation.Pending('需求还在等范围确认人点击“完成节点”。')
        check(req['status'] == ['可研发'] and req['nature'] == ['当前工作'] and req['revision'] == revision
              and req['processing'] in (None, [], ['正常']) and not ids(req['merged_into'])
              and ids(req['project']) == [self.project] and len(ids(req['confirmer'])) == 1
              and len(ids(req['feedback'])) == 1 and confirmation.inline_text(req['title'])
              and confirmation.inline_text(req['scope']) and confirmation.inline_text(req['criteria']),
              '需求修订、当前状态、范围确认人或原反馈不符。')
        feedback = self.row('feedback', ids(req['feedback'])[0])
        check(feedback['nature'] == ['当前工作'] and ids(feedback['project']) == [self.project]
              and record in ids(feedback['requirements']) and not ids(feedback['merged_into'])
              and feedback['status'] not in (['已归并'], ['已撤回'], ['已答复']),
              '原反馈已结束、已归并，或没有回连到本项目的需求。')
        project = self.row('projects', self.project)
        check(project['number'] == self.binding['project_number'] and project['status'] in (['准备中'], ['进行中']),
              '项目编号不符或项目已停止。')
        return req

    def find_key(self, key):
        table = self.tables['work']
        fields = table['fields']
        rows = self.reader.records(self.token, table['id'], [fields['key'], fields['project']], {
            'logic': 'and', 'conditions': [[fields['project']['id'], 'intersects', [{'id': self.project}]]]})
        matches = []
        for r in rows:
            check(ids(r['values'][fields['project']['id']]) == [self.project], '研发项查询超出本项目。')
            if r['values'][fields['key']['id']] == key:
                matches.append(r['record_id'])
        check(len(matches) <= 1, '同一记录键已有多个研发项，需先核对。')
        return matches[0] if matches else None

    def native_fields(self, values):
        table = self.tables['work']
        current = watch._fields(self.reader, self.binding, 'work', table, inspected={})
        names = [f['name'] for f in current.values()]
        payload = {}
        for alias, value in values.items():
            bound = table['fields'][alias]
            name = current[bound['id']]['name']
            check(name == bound['name'] and names.count(name) == 1, '研发项写入字段名称有变化或重复。')
            # Native v1 links use record IDs; personnel use open_id objects. Stage is never sent.
            payload[name] = ([e['id'] for e in value] if alias in ('requirements', 'project', 'feedback') else
                             value[0] if alias == 'nature' else value)
        return payload

    def create(self, payload, client_token):
        path = f"/open-apis/bitable/v1/apps/{self.token}/tables/{self.tables['work']['id']}/records"
        response = self.reader.runner(['lark-cli', 'api', 'POST', path, '--as', 'bot', '--profile', self.reader.profile,
                                       '--params', json.dumps({'client_token': client_token, 'user_id_type':'open_id'}),
                                       '--data', '-'], input=json.dumps({'fields': payload}, ensure_ascii=False),
                                      capture_output=True, text=True, timeout=60)
        data = self.reader.base._envelope(response, '创建研发计划')
        record = data['record']['record_id']
        check(json.loads(response.stdout).get('identity') == 'bot' and re.fullmatch(r'rec[A-Za-z0-9]+', record),
              '创建结果未能核实，请先按记录键查回。')
        return record


def verify_source_history(project, requirement, intent, attached):
    history = confirmation.history(project.reader, project.token, project.tables['requirements']['id'], requirement)
    # Match the whole pre-write head. The only permitted intervening change is one addition
    # of the expected child, checked separately against both live ends and the empty old link.
    idx = next((i for i, item in enumerate(history) if digest(item) == intent['source_head']), None)
    allowed = project.tables['requirements']['fields']['work']['id']
    check(idx is not None and idx <= int(attached), '需求在核对后有其他改动，请先核实。')
    if idx == 1:
        item = history[0]
        changes = item.get('field_changes')
        check(item.get('activity_type') == 'update' and type(item.get('rev')) is int
              and item['rev'] > history[1]['rev'] and isinstance(changes, list) and len(changes) == 1
              and changes[0].get('field_id') == allowed, '需求在核对后修改了其他内容。')


def run(project, requirement, revision, confirmer, draft, confirm, *, read_only=False):
    key = 'requirement-plan-' + digest([project.token, project.project, requirement])[:40]
    path = project.directory / (key + '.json')
    with (contextlib.nullcontext() if read_only else bridge.exclusive(path.with_suffix('.lock'))):
        intent = bridge.load(path) if path.exists() else None
        if read_only:
            if not intent or intent.get('phase') != 'sent':
                return result('pending', '尚无已发送的计划创建请求。')
            revision, confirmer = intent['revision'], intent['confirmer']
        if not intent:
            initial = confirm(project.path, 'requirements', requirement)
            if initial['status'] != 'verified':
                return result('pending', initial['reason'])
        if not project.live:
            project.check_fields()
        req = project.source(requirement, revision)
        source = digest({k: v for k, v in req.items() if k != 'work'})
        linked = ids(req['work'])
        known = project.find_key(key)
        if intent:
            check(intent['binding'] == project.signature and intent['source'] == source
                  and intent['revision'] == revision and intent['confirmer'] == confirmer,
                  '原写入对应的需求或计划确认人已改变。')
        record = (intent or {}).get('record_id') or known
        if intent and (intent.get('phase') == 'sent' or read_only):
            if not record:
                check(not linked, '需求已有其他研发项。')
                return result('pending', '已发送创建请求，尚未查到记录。请按记录键核实上次请求的结果。')
            check((not known or known == record) and linked in ([], [record]), '研发项的编号或反向关联有冲突。')
        else:
            if known or linked:
                return result('conflict', '该需求已有研发项，请先核对现有计划。', known or linked[0])
            confirmation.confirm_directory_user(project.reader, confirmer)
            check(confirmer != project.open_id, '助手不能代替计划确认人。')
            table_id = project.tables['requirements']['id']
            history = confirmation.history(project.reader, project.token, table_id, requirement)
            check(bool(history), '无法核实需求的修改历史。')
            proof = confirm(project.path, 'requirements', requirement)
            if proof['status'] != 'verified':
                return result('pending', proof['reason'])
            check(proof['record_id'] == requirement and proof['table_id'] == table_id
                  and proof['project_record_id'] == project.project and proof['business_revision'] == revision
                  and [proof['operator_open_id']] == ids(req['confirmer']), '确认与当前需求或这版范围不符。')
            if not intent:
                values = {**draft, 'title': req['title'], 'nature': ['当前工作'], 'key': key, 'plan_revision': 1,
                          'plan_confirmer': [{'id': confirmer}], 'requirements': [{'id': requirement}],
                          'feedback': [{'id': ids(req['feedback'])[0]}], 'project': [{'id': project.project}]}
                intent = dict(binding=project.signature, source=source, revision=revision, confirmer=confirmer,
                              values=values, source_head=digest(history[0]), confirmation_event=proof['event_id'],
                              client_token=str(uuid.uuid4()), phase='ready')
                atomic_json(path, intent)
            check(intent['phase'] == 'ready' and intent['confirmation_event'] == proof['event_id']
                  and req == project.source(requirement, revision), '写入前需求或确认已经改变。')
            verify_source_history(project, requirement, intent, False)
            payload = project.native_fields(intent['values'])
            project.unchanged()
            # Mark the POST before it can leave this process. An uncertain POST is looked up, never replayed.
            intent['phase'] = 'sent'
            atomic_json(path, intent)
            with path.open('rb') as saved:
                os.fsync(saved.fileno())
            sync_directory(path.parent)
            record = project.create(payload, intent['client_token'])
            intent['record_id'] = record
            atomic_json(path, intent)

        work = project.row('work', record)
        for k, expected in intent['values'].items():
            actual = work[k]
            same = ids(actual) == ids(expected) if k in ('project', 'requirements', 'feedback', 'plan_confirmer') else actual == expected
            if not same:
                return result('conflict', '研发项普通字段已变更或未完整写入，保留现有内容。', record)
        if work.get('processing') not in (None, [], ['正常']) or ids(work.get('bugs')):
            return result('conflict', '研发项已暂停、受阻或关联了其他来源，请核对现有计划。', record)
        if work['status'] != ['待确认计划']:
            return result('pending', '研发项的真实节点还没有显示“待确认计划”。', record)
        req = project.source(requirement, revision)
        check(digest({k: v for k, v in req.items() if k != 'work'}) == source, '需求在创建研发项期间改变了。')
        feedback = project.row('feedback', ids(req['feedback'])[0])
        if ids(req['work']) != [record] or record not in ids(feedback['work']):
            return result('pending', '原需求或反馈还没有回连到这条研发项。', record)
        verify_source_history(project, requirement, intent, True)
        project.unchanged()
        outcome = result('prepared', '计划已写入并读回，正在等计划确认人查看。', record)
        if read_only:
            return dict(outcome, values=intent['values'], confirmer=confirmer, requirement_revision=revision)
        intent.update(record_id=record, verified_readback=True)
        atomic_json(path, intent)
        return outcome


def get_prepared(config_path, requirement_id, *, project=None):
    """Read the original intent and recheck the current plan, its links and source. Never posts."""
    try:
        project = project or Project(config_path)
        check(Path(config_path).resolve() == Path(project.path).resolve(), '来源服务不一致。')
        return run(project, requirement_id, None, None, None, None, read_only=True)
    except Conflict as exc:
        return result('conflict', str(exc))
    except (OnboardingError, OSError, KeyError, TypeError, IndexError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return result('pending', '这份计划暂时无法核实。')


def prepare(path, requirement, revision, confirmer, draft):
    try:
        check(isinstance(requirement, str) and re.fullmatch(r'rec[A-Za-z0-9]+', requirement)
              and type(revision) is int and revision > 0 and isinstance(confirmer, str)
              and re.fullmatch(r'ou_[A-Za-z0-9_]+', confirmer), '需求、修订或确认人编号不正确。')
        draft = checked_draft(draft)
        project = Project(path)
        check(confirmer != project.open_id, '助手不能代替计划确认人。')
        return run(project, requirement, revision, confirmer, draft, confirmation.verify)
    except Conflict as exc:
        return result('conflict', str(exc))
    except confirmation.Pending as exc:
        return result('pending', str(exc))
    except (OnboardingError, OSError, KeyError, IndexError, TypeError, ValueError, AttributeError,
            subprocess.TimeoutExpired):
        return result('pending', '未能完整核实，已有写入会按记录键查回。')


def main():
    parser = argparse.ArgumentParser(description='为本项目一条已确认的需求准备研发计划。')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--requirement', required=True)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--confirmer', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(64001)
        check(len(raw) <= 64000, '计划过长。')
        output = prepare(args.config, args.requirement, args.revision, args.confirmer, json.loads(raw))
    except (ValueError, TypeError):
        output = result('conflict', '请输入只含四项说明的 JSON。')
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output['status'] == 'prepared' else 1


if __name__ == '__main__':
    sys.exit(main())
