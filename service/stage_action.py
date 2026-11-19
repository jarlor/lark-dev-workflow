"""A bounded stage transaction for an authorized project worker.

The caller supplies an installed Python business adapter, never a model-supplied
decision. verify() authenticates the same scope/plan/build at either end of a
transition. Its digest must exclude the marker and the expected status change.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import event_bridge as bridge
from identity_handoff import sync_directory
from onboard import OnboardingError, atomic_json
from prepare_plan import digest
import unattended_control as control
import watch
import workflow_rule as rules


class Pending(ValueError):
    pass


def require(test, message):
    if not test:
        raise Pending(message)


@dataclass(frozen=True)
class BusinessEvidence:
    project_record_id: str
    table_id: str
    record_id: str
    key: str
    revision: int
    evidence_sha256: str


def private(path, kind):
    info = path.lstat()
    require(kind(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            '动作记录须放在服务账号自己的私有目录。')


@contextmanager
def locked_record(directory, namespace, table, record):
    private(directory, stat.S_ISDIR)
    folder = directory / namespace
    folder.mkdir(mode=0o700, exist_ok=True)
    private(folder, stat.S_ISDIR)
    sync_directory(directory)
    path = folder / (table + '-' + record + '.json')
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                '该事项的动作锁不属于当前服务。')
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(path):
            private(path, stat.S_ISREG)
        yield path


@contextmanager
def row_ledger(directory, table, record):
    with locked_record(directory, 'stage-actions', table, record) as path:
        ledger = bridge.load(path) if path.exists() else {'format': 1, 'intents': []}
        require(isinstance(ledger, dict) and ledger.get('format') == 1 and isinstance(ledger.get('intents'), list),
                '该事项原有的动作记录无法核实。')
        yield path, ledger


def save(path, ledger):
    atomic_json(path, ledger)
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)


class Project:
    def __init__(self, config_path, name):
        self.path = config_path
        self.config = bridge.config_from(config_path)
        require(self.config['mode'] == 'dispatch', '本服务只收消息，没有执行授权。')
        self.paths = bridge.runner_pins(self.config)
        self.runner = control.load_config(self.config['runner_config'])
        require(self.runner.get('native_stage_mode') == 'registered_requests'
                and self.runner.get('event_tenant_key') == self.config['tenant_key']
                and isinstance(self.runner.get('stage_service_config'), str)
                and Path(self.runner['stage_service_config']).resolve() == Path(self.path).resolve(),
                '当前执行器没有启用本项目的六项受限动作。')
        self.binding = bridge.load(self.paths[2])
        self.actor = control.actor_for_runner(self.runner, self.binding)
        self.directory = Path(self.config['state_dir'])
        registry = rules.private_registry(self.config, self.paths)
        registered = [r['key'] for r in registry.values() if r.get('purpose') == rules.STAGE_REQUEST]
        require(sorted(registered) == sorted(rules.STAGE_REQUESTS),
                '本项目须唯一登记六个受限动作。')
        key = name if name in rules.STAGE_REQUESTS else registry.get(name, {}).get('key')
        require(name not in registry or registry[name].get('key') == key, '规则名与另一项动作重名。')
        found = [(n, r) for n, r in registry.items()
                 if r.get('purpose') == rules.STAGE_REQUEST and r.get('key') == key]
        require(key in rules.STAGE_REQUESTS and len(found) == 1 and name in (key, found[0][0]),
                '本项目没有唯一登记这项动作。')
        self.name, self.rule = found[0]
        rules.checked_rule(self.rule)
        self.key, self.table = key, self.rule['table_id']
        self.token, self.project = self.config['base_token'], self.config['project_record_id']
        self.bot = self.config['bot_open_id']
        self.fields = self.binding['tables'][self.rule['role']]['fields']
        require(self.table in self.config['table_ids'] and self.binding['tables'][self.rule['role']]['id'] == self.table
                and self.fields['status']['id'] == self.rule['stage_field_id']
                and self.fields['project']['id'] == self.rule['guard']['field_id']
                and self.rule['guard']['value'] == self.project and self.rule['authored_by'] == self.bot, '规则属于另一张表、项目或助手。')
        self.identity = digest([rules.service_pins(self.config, self.paths), self.binding])
        self.bot_name = None

        def run(argv, **kwargs):
            kwargs['env'] = dict(kwargs.get('env', os.environ), PATH=self.runner['path'])
            response = subprocess.run(argv, **kwargs)
            # The reader already checks whoami and this official bot-info response.
            if argv[1:4] == ['api', 'GET', '/open-apis/bot/v3/info'] and response.returncode == 0:
                envelope = json.loads(response.stdout)
                data = envelope.get('data', envelope)
                bot = data.get('bot', data)
                if bot.get('open_id') == self.bot:
                    self.bot_name = bot.get('app_name')
            return response

        self.reader = watch.Cli(self.actor['profile'], runner=run, executable=self.runner['lark_bin'], actor=self.actor)

    def check_rule(self, *, sending=False):
        proof = rules.verify(self.path, self.name)
        require(proof.get('status') == 'checked' and proof.get('check') == rules.STAGE_REQUEST
                and all(proof.get(k) is True for k in ('definition_pinned', 'definition_matches',
                    'updates_trigger_row_only', 'source_ai_in_project_contract', 'new_marker_value_verified',
                    'project_condition_verified', 'trigger_modifier_verified'))
                and (self.rule['role'] != 'work' or proof.get('fixed_bot_owner_verified') is True)
                and proof.get('workflow_id') == self.rule['workflow_id'] and proof.get('table_id') == self.table
                and proof.get('definition_sha256') == self.rule.get('definition_sha256'),
                '没有核实本项目登记的原生规则和助手身份。')
        if sending:
            require(isinstance(self.bot_name, str) and self.bot_name.strip() and self.bot_name != '多维表格助手',
                    '无法区分本助手和平台自动化在历史中的名称。')
            require(proof.get('enabled') is True and not os.path.lexists(Path(self.paths[1]) / 'PAUSED'),
                    '项目已暂停或这条云端规则未启用。')
        self.check_binding()

    def check_binding(self):
        require(bridge.config_from(self.path) == self.config and bridge.runner_pins(self.config) == self.paths
                and control.load_config(self.config['runner_config']) == self.runner
                and bridge.load(self.paths[2]) == self.binding
                and rules.private_registry(self.config, self.paths).get(self.name) == self.rule,
                '核对期间项目或登记的规则发生变化。')

    def row(self, record):
        keys = ['project', 'status', 'nature', 'processing']
        if self.rule['role'] == 'work':
            keys.append('owner')
        marker = self.rule['marker_field_id']
        fields = [self.fields[k] for k in keys] + [{'id': marker}]
        row = self.reader.read_record(self.token, self.table, record, fields)
        values = row['values']
        require(row['record_id'] == record and set(values) == {f['id'] for f in fields}
                and watch._one_project(values, self.fields['project']['id'], self.project)
                and values[self.fields['nature']['id']] == ['当前工作']
                and values[self.fields['processing']['id']] == ['正常']
                and ('owner' not in keys or watch._one_project(values, self.fields['owner']['id'], self.bot)),
                '事项的项目、负责人或处理状态不符合这项动作。')
        stage = values[self.fields['status']['id']]
        require(isinstance(stage, list) and len(stage) == 1
                and (values[marker] is None or isinstance(values[marker], str)), '当前状态或助手请求无法识别。')
        return stage[0], values[marker] or ''

    def history(self, record, anchor=None):
        rows, cursor, previous = [], None, None
        for _ in range(100):
            args = ['--base-token', self.token, '--table-id', self.table, '--record-id', record,
                    '--page-size', '50' if anchor is not None else '1']
            if cursor is not None:
                args += ['--max-version', str(cursor)]
            page = self.reader.base.read('+record-history-list', *args)
            items = page.get('items')
            require(isinstance(items, list) and items and type(page.get('has_more')) is bool,
                    '飞书尚未返回完整的本行历史。')
            for item in items:
                rev = item['rev']
                require(type(rev) is int and rev > 0 and (previous is None or rev < previous),
                        '本行历史的版本号不唯一或次序不清。')
                rows.append(item)
                previous = rev
                if anchor is None or rev == anchor:
                    return rows
                require(rev > anchor, '飞书记录历史缺少提交前的版本。')
            following = page.get('next_max_version')
            require(page['has_more'] and type(following) is int and following > 0
                    and (cursor is None or following < cursor), '飞书记录历史未回溯到提交前的版本。')
            cursor = following
        raise Pending('飞书记录历史未回溯到提交前的版本。')

    def send_marker(self, record):
        field, text = self.rule['marker_field_id'], self.rule['marker_text']
        argv = ['lark-cli', 'base', '+record-upsert', '--base-token', self.token, '--table-id', self.table,
                '--record-id', record, '--as', 'bot', '--profile', self.actor['profile'],
                '--json', json.dumps({field: text}, ensure_ascii=False)]
        result = self.reader.runner(argv, capture_output=True, text=True, timeout=45)
        data = self.reader.base._envelope(result, '写入助手请求')
        # The tested CLI returns updated + record.update, without record_id or revision.
        require(json.loads(result.stdout).get('identity') == 'bot' and data.get('updated') is True
                and data['record'].get('update') == {'助手请求': text}
                and data['record'].get('record_id', record) == record, '尚未核实单行请求的应答。')


def evidence(business, project, record):
    # The row's actual native approval is always checked by this service.
    # The trusted project adapter additionally checks all related rows and builds.
    import stage_business
    role = rules.STAGE_REQUESTS[project.key][0]
    approval = stage_business.confirmation_for(project.path, role, record)
    require(approval.get('status') == 'verified', approval.get('reason') or '尚未核实本人对这一版的确认。')
    proof = business.verify(project.path, project.key, record)
    require(isinstance(proof, BusinessEvidence) and
            (proof.project_record_id, proof.table_id, proof.record_id, proof.key) ==
            (project.project, project.table, record, project.key)
            and type(proof.revision) is int and 0 < proof.revision < 2**53
            and isinstance(proof.evidence_sha256, str) and re.fullmatch('[0-9a-f]{64}', proof.evidence_sha256),
            '这版事项的人工确认、关联或测试依据仍未核实。')
    require(approval.get('business_revision') == proof.revision and approval.get('record_id') == record
            and approval.get('table_id') == project.table and approval.get('project_record_id') == project.project,
            '本项业务依据与真实点击的事项或版本不一致。')
    return dict(asdict(proof), confirmation_sha256=approval['confirmation_sha256'])


def receipt_matches(witness, project, record, intent, marker):
    return (isinstance(witness, dict) and all(witness.get(k) == v for k, v in {
        'app_id': project.config['app_id'], 'tenant_key': project.config['tenant_key'],
        'base_token': project.token, 'table_id': project.table, 'record_id': record,
        'field_id': project.rule['marker_field_id'], 'operator_open_id': project.bot,
        'revision': marker['rev'], 'update_time': marker['create_time'],
        'before': intent['previous_marker'], 'after': project.rule['marker_text'],
    }.items()) and type(witness['revision']) is int and type(witness['update_time']) is int and isinstance(witness.get('event_id'), str)
            and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', witness['event_id']))


def reconcile(project, business, record, intent):
    reg, anchor = project.rule, intent['head_revision']
    history = project.history(record, anchor)
    require(digest(history[-1]) == intent['head_digest'], '提交前的历史版本与保存的依据不同。')
    require(len(history) == 3, '尚未找到请求和紧随其后的唯一状态变化，保留原请求继续核对。')
    transition, marker = history[:2]
    for item, field, kind, before, after in (
        (marker, reg['marker_field_id'], 'Text', intent['previous_marker'], reg['marker_text']),
        (transition, reg['stage_field_id'], 'Stage', reg['source']['name'], reg['target']['name']),
    ):
        changes = item.get('field_changes')
        require(item.get('activity_type') == 'update' and isinstance(changes, list) and len(changes) == 1,
                '期间有其他改动，无法确认此次请求只推进了一步。')
        change = changes[0]
        require(change.get('field_type') == kind and change.get('field_id') == field
                and change.get('before') == before and change.get('after') == after,
                '飞书记录历史与本次标记、起点或终点不一致。')
    require(transition.get('operator') == '多维表格助手', '状态变化的来源与实测的飞书自动化不同。')
    require(project.bot_name and project.bot_name != '多维表格助手' and marker.get('operator') == project.bot_name,
            '标记历史与本项目绑定的助手不符。')
    if not intent['accepted']:
        reader = getattr(business, 'marker_receipt', None)
        witness = reader(project.path, project.table, record, reg['marker_field_id'], marker['rev']) if reader else None
        require(receipt_matches(witness, project, record, intent, marker),
                '飞书已出现节点变化，但缺少本助手写入这次请求的原始通知。请保留当前事项，不要重发。')
    require(evidence(business, project, record) == intent['business'], '原确认、关联或构建依据已变化。')
    project.check_rule()
    require(project.row(record) == (reg['target']['name'], reg['marker_text'])
            and project.history(record)[0] == transition, '读回时状态、标记或版本又有变化。')
    return {'status': 'completed', 'key': project.key, 'record_id': record,
            'business_revision': intent['business']['revision'], 'workflow_id': reg['workflow_id'],
            'definition_sha256': reg['definition_sha256'], 'marker_revision': marker['rev'],
            'transition_revision': transition['rev'], 'target': reg['target']['name']}


def transact(project, business, record):
    reg = project.rule
    with row_ledger(project.directory, project.table, record) as (path, ledger):
        entries = ledger['intents']
        require(all(x['identity'] == project.identity for x in entries), '已有请求属于另一份项目绑定。')
        pending = [x for x in entries if 'result' not in x]
        if pending:
            require(len(pending) == 1 and pending[0]['key'] == project.key
                    and pending[0]['rule_digest'] == digest(reg), '该事项还有另一项请求要先核对。')
            intent = pending[0]
        else:
            project.check_rule()
            state, marker = project.row(record)
            completed = [x for x in entries if x['key'] == project.key]
            if state != reg['source']['name']:
                require(completed and completed[-1]['rule_digest'] == digest(reg), '没有本次请求，事项也未处于起点。')
                prior = completed[-1]
                if state == reg['target']['name']:
                    require(evidence(business, project, record) == prior['business'],
                            '这项动作有历史回执，但本版的业务依据已变化。')
                return dict(prior['result'], status='already_completed', historical=True)
            proof = evidence(business, project, record)
            if completed:
                require(proof['revision'] > completed[-1]['business']['revision'],
                        '这版事项已经推进过。请核对为何又回到了起点。')
            head = project.history(record)[0]
            if marker:
                require(entries and entries[-1]['result']['target'] == state
                        and entries[-1]['marker'] == marker and marker != reg['marker_text'],
                        '本行已有其他请求，先核实已有操作。')
                # Do not overwrite a marker somebody edited and restored since the previous action.
                anchor = entries[-1]['result']['transition_revision']
                since = project.history(record, anchor)
                require(all(isinstance(h.get('field_changes'), list) and h['field_changes']
                            and all(c.get('field_id') not in (reg['marker_field_id'], reg['stage_field_id'])
                                    for c in h['field_changes']) for h in since[:-1]),
                        '上一动作完成后，状态或标记又被改过。')
            require(evidence(business, project, record) == proof, '提交前业务依据发生变化。')
            project.check_rule(sending=True)
            require(project.row(record) == (state, marker) and project.history(record)[0] == head,
                    '提交前事项或记录历史发生变化。')
            intent = dict(identity=project.identity, key=project.key, rule_digest=digest(reg),
                          business=proof, previous_marker=marker, marker=reg['marker_text'],
                          head_revision=head['rev'], head_digest=digest(head), accepted=False)
            entries.append(intent)
            save(path, ledger)
            project.send_marker(record)
            intent['accepted'] = True
            save(path, ledger)
        result = reconcile(project, business, record, intent)
        intent['result'] = result
        save(path, ledger)
        return result


def _run(config_path, rule_name_or_key, record_id, *, business):
    """Trusted worker entry only. The sole write is one registered marker on one record."""
    try:
        require(isinstance(record_id, str) and re.fullmatch(r'rec[A-Za-z0-9]{1,128}', record_id)
                and isinstance(rule_name_or_key, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', rule_name_or_key),
                '请指定已登记的动作和真实事项编号。')
        return transact(Project(config_path, rule_name_or_key), business, record_id)
    except Pending as exc:
        return {'status': 'pending', 'reason': str(exc)}
    except (KeyError, ValueError, AttributeError, TypeError, OSError, subprocess.SubprocessError, OnboardingError):
        return {'status': 'pending', 'reason': '尚未查实本次动作。已保存的请求下次只查结果。'}


def main(argv=None):
    import argparse
    import stage_business
    parser = argparse.ArgumentParser(description='让本项目的研发助手处理一条已登记的动作。')
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--action', required=True)
    parser.add_argument('--record-id', required=True)
    args = parser.parse_args(argv)
    result = _run(args.config, args.action, args.record_id, business=stage_business)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('completed', 'already_completed') else 1


if __name__ == '__main__':
    # Keep a single dataclass/type identity when stage_business imports this file.
    import sys
    sys.modules.setdefault('stage_action', sys.modules[__name__])
    raise SystemExit(main())
