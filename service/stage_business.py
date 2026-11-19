"""Remember a real scope or plan approval for the registered-request worker.

The approval covers the named person's decision about this row. Linked work,
reproduction, delivery, deployment and independent retest still need their own
live checks at execution time.
"""
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import event_bridge as bridge
from onboard import OnboardingError
from prepare_plan import digest, ids
import stage_action
import verify_confirmation as confirmation
import watch
import workflow_rule as rules


FIRST = {'requirements': 'req-start', 'bugs': 'bug-start', 'work': 'work-start'}
# These downstream facts are produced *after* approval. They are checked by the
# step's business adapter; changes to them cannot document or extend a human approval.
LATER = {
    'requirements': {'work', 'verification'},
    'bugs': {'work', 'candidate_build', 'reproduction_tests', 'retest_tests'},
    'work': set(),
}
OPERATIONAL = {'human_action', 'next_step', 'processing', 'summary', 'assistant_request'}
REQUIRED = {
    'requirements': {'key', 'title', 'nature', 'project', 'scope', 'criteria', 'revision',
                     'feedback', 'confirmer', 'acceptor', 'status', 'processing'},
    'bugs': {'key', 'title', 'nature', 'project', 'scope', 'criteria', 'revision', 'feedback',
             'confirmer', 'acceptor', 'expected', 'actual', 'conditions', 'baseline', 'status', 'processing'},
    'work': {'key', 'title', 'nature', 'project', 'owner', 'scope', 'deliverable', 'verification_plan', 'plan',
             'plan_revision', 'plan_confirmer', 'feedback', 'requirements', 'bugs', 'dependencies', 'status', 'processing'},
}
require = stage_action.require


def live_row(project, record, fields):
    row = project.reader.read_record(project.token, project.table, record, list(fields.values()))
    require(row.get('record_id') == record and set(row['values']) == {f['id'] for f in fields.values()},
            '事项编号或实际字段不完整。')
    return {k: row['values'][f['id']] for k, f in fields.items()}


def protected_fields(role, fields, row):
    require(REQUIRED[role] <= fields.keys(), '这张表缺少核实人工确认所需的原字段。')
    excluded = OPERATIONAL | LATER[role] | {'status'}
    values = {}
    for key, field in fields.items():
        if key in excluded:
            continue
        value = row[key]
        if field['type'] in ('link', 'user'):
            value = sorted(ids(value))
            require(len(value) == len(set(value)), '人员或关联重复，无法核实这版确认。')
        values[field['id']] = value
    return values


def validate_row(project, role, row):
    _, _, person, revision, texts = confirmation.GATES[role]
    require(watch._one_project(row, 'project', project.project)
            and row['nature'] == ['当前工作'] and row['processing'] == ['正常']
            and (not row.get('merged_into')) and type(row[revision]) is int and 0 < row[revision] < 2**53
            and all(confirmation.inline_text(row[key]) for key in texts)
            and len(ids(row[person])) == 1 and ids(row[person])[0] != project.bot,
            '这条事项的项目、确认人、当前版本或确认内容不符。')
    return row[revision], ids(row[person])[0]


def owner_transition(project, row, record, step, item, material=None):
    """A human-owned work item may already have been started by its actual owner."""
    import gate_events
    import stage_source
    require(stage_source.location(project) is not None, '此研发项的人工操作需要核实原负责人当时的真实事件。')
    if material is None:
        material = stage_source.query(project, 'prior_action', 'work', record, prior_step=step)
    _, _, source, target = rules.STAGE_REQUESTS[step]
    owner = ids(row['owner'])
    event = material.get('event')
    field = project.fields['status']['id']
    require(len(owner) == 1 and owner[0] != project.bot and material.get('method') == 'native_owner_click'
            and material.get('owner_open_id') == owner[0] and material.get('plan_revision') == row['plan_revision']
            and isinstance(event, dict) and (event.get('table_id'), event.get('record_id'), event.get('field_id'),
                event.get('operator_open_id')) == (project.table, record, field, owner[0])
            and type(event.get('event_revision')) is int and type(event.get('update_time')) is int
            and item.get('rev') == event['event_revision'] and item.get('create_time') == event['update_time']
            and isinstance(event.get('event_id'), str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event['event_id']),
            '实际负责人、计划版本或原生操作事件与本行不符。')
    if event.get('value_format') == gate_events.NATIVE_STAGE_IDS:
        require(gate_events.opaque_pair(event.get('before'), event.get('after')), '原生操作事件缺少两个节点。')
    else:
        require('value_format' not in event and (event.get('before'), event.get('after')) == (source, target),
                '原负责人操作的起点或终点不符。')
    confirmation.confirm_directory_user(project.reader, owner[0])
    confirmation.match_history(event, [item], field, row['owner'][0]['name'], set(), (source, target))


def authorized_owner_handoff(project, record, row, proof, item):
    import stage_source
    require(stage_source.location(project) is not None, '负责人变了，需要核对这次交接的授权和飞书记录。')
    material = stage_source.query(project, 'owner_handoff', 'work', record)
    owner_field = project.fields['owner']
    owners = row['owner']
    require(owner_field['type'] == 'user' and isinstance(owners, list) and len(owners) == 1
            and owners[0].get('id') == project.bot and owners[0].get('name')
            and material.get('method') == 'authorized_owner_handoff'
            and (material.get('project_record_id'), material.get('table_id'), material.get('record_id'),
                 material.get('owner_field_id'), material.get('to_open_id'), material.get('to_name')) ==
                (project.project, project.table, record, owner_field['id'], project.bot, owners[0]['name']),
            '交接记录与这个项目、这条研发项或当前唯一的负责人不一致。')
    previous = material.get('from_open_id')
    require(isinstance(previous, str) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', previous)
            and previous != project.bot and isinstance(material.get('from_name'), str)
            and material['from_name'].strip() and material['from_name'] != material['to_name']
            and material.get('plan_confirmer_open_id') == proof['operator_open_id']
            and all(type(material.get(k)) is int and material[k] == proof[v] for k, v in (
                ('plan_revision', 'business_revision'), ('confirmation_table_revision', 'table_revision'),
                ('confirmation_update_time', 'update_time'))),
            '交接记录没有对应原负责人和这版计划的本人确认。')
    require(material.get('history') == item and item.get('activity_type') == 'update'
            and item.get('operator') == material['to_name'] and item['rev'] > proof['table_revision']
            and type(item.get('create_time')) is int and item['create_time'] >= proof['update_time']
            and len(item['field_changes']) == 1, '飞书历史没有证实这次单独的负责人交接。')
    change = item['field_changes'][0]
    require((change.get('field_id'), change.get('field_type'), change.get('before'), change.get('after')) ==
            (owner_field['id'], 'User', material['from_name'], material['to_name']),
            '飞书历史里的负责人变化与授权不同。')
    return previous


def record_history(project, role, record, seal, row, current_values):
    proof = seal['proof']
    require(proof['operator_open_id'] == validate_row(project, role, row)[1], '计划或范围的确认人已经不同。')
    before, after, _, revision_key, _ = confirmation.GATES[role]
    require(proof['business_revision'] == row[revision_key], '上一次真实点击不属于当前版本。')
    history = project.history(record, proof['table_revision'])
    anchor = history[-1]
    require(anchor == seal['anchor'] and anchor['rev'] == proof['table_revision']
            and anchor['create_time'] == proof['update_time'], '保存的确认和飞书原始历史已对不上。')
    status = project.fields['status']['id']
    changes = anchor.get('field_changes')
    matches = [c for c in changes or [] if c.get('field_id') == status]
    require(anchor.get('activity_type') == 'update' and len(matches) == 1 and matches[0].get('field_type') == 'Stage'
            and (matches[0].get('before'), matches[0].get('after')) == (before, after)
            and all(c.get('field_id') not in current_values for c in changes),
            '无法从原始历史核实当时确认的节点和内容。')
    allowed = {project.fields[k]['id'] for k in (OPERATIONAL | LATER[role]) if k in project.fields}
    allowed.add(project.rule['marker_field_id'])
    edges = {(source, target) for r, _, source, target in rules.STAGE_REQUESTS.values() if r == role}
    stage = after
    owner_field = project.fields['owner']['id'] if role == 'work' else None
    previous_owner = None
    filed = {}
    note_fields = {project.fields[k]['id']: k for k in ('confirmation', 'confirmed_revision', 'details')
                   if role == 'requirements' and k in project.fields}
    for item in reversed(history[:-1]):
        changes = item.get('field_changes')
        require(item.get('activity_type') == 'update' and isinstance(changes, list) and changes,
                '确认后有一笔改动无法识别。')
        for change in changes:
            fid = change.get('field_id')
            if fid == status:
                source, target = change.get('before'), change.get('after')
                require(len(changes) == 1 and change.get('field_type') == 'Stage' and source == stage
                        and (source, target) in edges, '确认后节点被退回或跳过，需要重新核实。')
                if item.get('operator') != '多维表格助手':
                    require(role == 'work', '这一步的实际操作人无法从已核实的规则确认。')
                    key = next(k for k, (r, _, a, b) in rules.STAGE_REQUESTS.items()
                               if (r, a, b) == (role, source, target))
                    owner_transition(project, row, record, key, item)
                stage = target
            elif fid is not None and fid == owner_field:
                require(stage == after and previous_owner is None,
                        '只可核对计划确认后、开工前的一次负责人交接。')
                previous_owner = authorized_owner_handoff(project, record, row, proof, item)
            elif fid in note_fields:
                import confirmation_notes
                require(fid not in filed and len(changes) == 1, '确认后的归档字段被多次修改或和其他改动混在一起。')
                value = confirmation_notes.original_value(project, note_fields[fid], record, proof, row, change)
                filed[fid] = '' if value is None and seal['fields'].get(fid) == '' else value
            else:
                require(fid in allowed and fid not in current_values,
                        '确认后改过范围、计划、人员、归属或未登记的字段。即使改回也须重新确认。')
    require(row['status'] == [stage], '当前状态与确认之后的完整记录历史不同。')
    # A seal always records the fields at the original click. A handoff never
    # replaces that approval or changes the digest held by earlier actions.
    approved_values = {**current_values, **filed}
    if previous_owner is not None:
        approved_values[owner_field] = [previous_owner]
    return history[0], approved_values


def capture(project, role, record, row, values):
    before, after, _, _, _ = confirmation.GATES[role]
    require(row['status'] == [after], '没有留存这版的真实确认。先核实原点击，再继续这一步。')
    observations = []
    def original(*args):
        observed = confirmation.receipt(*args)
        observations.append(observed)
        return observed
    proof = confirmation.verify(project.path, role, record, receipt_reader=original)
    revision, person = validate_row(project, role, row)
    require(proof.get('status') == 'verified' and proof.get('check') == 'current_record_human_transition'
            and proof.get('role') == role and proof.get('project_record_id') == project.project
            and proof.get('record_id') == record and proof.get('table_id') == project.table
            and proof.get('field_id') == project.fields['status']['id']
            and proof.get('business_revision') == revision and proof.get('operator_open_id') == person,
            '飞书原始事件和历史尚不能证明指定的人确认了这版内容。')
    require(observations and all(value == observations[0] for value in observations),
            '核对期间原始确认事件已经改变。')
    event = observations[0]
    for event_key, proof_key in [('event_id', 'event_id'), ('event_revision', 'table_revision'),
                                 ('update_time', 'update_time'), ('operator_open_id', 'operator_open_id'),
                                 ('field_id', 'field_id'), ('table_id', 'table_id'), ('record_id', 'record_id')]:
        require(event.get(event_key) == proof[proof_key], '保存前原事件与核实过的本人点击不同。')
    history = project.history(record, proof['table_revision'])
    return dict(proof=proof, event=event, anchor=history[-1], fields=values)


def archived_click(project, role, record, row, values):
    """Recheck a native click against the original event, member cards or a prior worker archive."""
    import gate_events
    import stage_source
    material = stage_source.query(project, 'original_click', role, record)
    event = material.get('event')
    revision, person = validate_row(project, role, row)
    before, after, person_field, _, _ = confirmation.GATES[role]
    method = material.get('method')
    if method in ('official_history_profile', 'archived_runner_history'):
        # An earlier worker may have seen the decision before the listener.
        # Recheck who clicked and keep the actual source in the saved receipt.
        card_hash = material.get('history_profile_sha256')
        anchor = material.get('history')
        require(material.get('original_event_captured') is False and event is None
                and (material.get('business_revision'), material.get('operator_open_id')) == (revision, person)
                and isinstance(anchor, dict) and type(anchor.get('rev')) is int and anchor['rev'] > 0
                and type(anchor.get('create_time')) is int
                and project.history(record, anchor['rev'])[-1] == anchor,
                '原点击与当前的确认人、版本或飞书历史对不上。')
        steps = project.fields['status']['stage_contract']['steps']
        step = steps[0]
        require((step['actor'], step['person_field'], step['name']) == ('human', person_field, before)
                and steps[1]['name'] == after,
                '原项目没有绑定这个人工确认节点。')
        if method == 'official_history_profile':
            require(isinstance(card_hash, str) and re.fullmatch(r'[a-f0-9]{64}', card_hash)
                    and card_hash == material.get('confirmer_profile_sha256'), '人员名片不一致。')
            confirmation.confirm_directory_user(project.reader, person)
        else:
            import archived_identity
            archived_identity.check(project, record, person_field, person, row[person_field][0]['name'], anchor, material)
        confirmation.check_history_author(anchor, row[person_field][0]['name'])
        changes = anchor.get('field_changes')
        require(anchor.get('activity_type') == 'update' and isinstance(changes, list) and len(changes) == 1
                and changes[0].get('field_type') == 'Stage' and changes[0].get('field_id') == project.fields['status']['id']
                and (changes[0].get('before'), changes[0].get('after')) == (before, after),
                '官方历史未证明本人正确完成了本次确认。')
        proof = dict(status='verified', check=method, role=role, dispatch_checked=False,
                     content_scope='record_fields', record_id=record, table_id=project.table, field_id=changes[0]['field_id'],
                     project_record_id=project.project, business_revision=revision, operator_open_id=person,
                     event_id=None, original_event_captured=False, table_revision=anchor['rev'], update_time=anchor['create_time'])
        seal = dict(proof=proof, source_report_sha256=material['report_sha256'], anchor=anchor, fields=values)
        if method == 'official_history_profile':
            seal['official_profile_sha256'] = card_hash
        return seal
    require(isinstance(event, dict) and material.get('business_revision') == revision
            and (event.get('operator_open_id'), event.get('table_id'), event.get('record_id'), event.get('field_id'))
            == (person, project.table, record, project.fields['status']['id'])
            and type(event.get('event_revision')) is int and event['event_revision'] > 0
            and type(event.get('update_time')) is int and event['update_time'] > 0
            and isinstance(event.get('event_id'), str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event['event_id']),
            '归档事件与本表、记录、修订或指定确认人不符。')
    if event.get('value_format') == gate_events.NATIVE_STAGE_IDS:
        require(gate_events.opaque_pair(event.get('before'), event.get('after')), '归档没有原始的两个流程节点。')
    else:
        require('value_format' not in event and (event.get('before'), event.get('after')) == (before, after),
                '归档事件的起点终点与真人确认步骤不符。')
    table = project.binding['tables'][role]
    step = table['fields']['status']['stage_contract']['steps'][0]
    require(step['name'] == before and step['actor'] == 'human' and step['person_field'] == person_field
            and table['fields']['status']['stage_contract']['steps'][1]['name'] == after,
            '此项目的原生确认步骤不符。')
    confirmation.confirm_directory_user(project.reader, person)
    anchor = project.history(record, event['event_revision'])[-1]
    require(anchor['rev'] == event['event_revision'] and anchor.get('create_time') == event['update_time'],
            '原始事件与线上记录历史的时刻或修订不符。')
    confirmation.match_history(event, [anchor], event['field_id'], row[person_field][0]['name'],
                               {f['id'] for k, f in project.fields.items() if k in OPERATIONAL},
                               (before, after))
    proof = dict(status='verified', check='current_record_human_transition', role=role, dispatch_checked=False,
                 content_scope='record_fields', record_id=record, table_id=project.table,
                 field_id=event['field_id'], project_record_id=project.project, business_revision=revision,
                 operator_open_id=person, event_id=event['event_id'], table_revision=event['event_revision'],
                 update_time=event['update_time'])
    return dict(proof=proof, event=event, anchor=anchor, fields=values)


def marker_receipt(config_path, table_id, record_id, field_id, revision):
    """Read the bound receiver's own event at the exact Feishu marker revision."""
    import gate_events
    config = bridge.config_from(config_path)
    paths = bridge.runner_pins(config)
    try:
        _, rows = confirmation.receipt_rows(config, bridge.load(paths[2]), paths, table_id, record_id,
                                            field_id, marker=True)
    except (confirmation.Pending, OSError, sqlite3.Error):
        return None
    found = [row for row in rows if row['event_revision'] == revision]
    if len(found) != 1:
        return None
    row = found[0]
    if row.get('value_format') != gate_events.ASSISTANT_MARKER or row.get('operator_open_id') != config['bot_open_id']:
        return None
    return dict(row, app_id=config['app_id'], tenant_key=config['tenant_key'],
                base_token=config['base_token'], revision=revision)


def confirmation_for(config_path, role, record_id):
    """Read only Feishu; save verified clicks in the existing worker's private storage."""
    try:
        require(isinstance(role, str) and role in FIRST and isinstance(record_id, str)
                and re.fullmatch(r'rec[A-Za-z0-9]{1,128}', record_id), '请指定范围或研发计划的真实记录。')
        project = stage_action.Project(config_path, FIRST[role])
        with stage_action.locked_record(project.directory, 'stage-confirmations', project.table, record_id) as path:
            project.check_binding()
            require(not os.path.lexists(Path(project.paths[1]) / 'PAUSED'), '项目已暂停。')
            watch._fields(project.reader, project.binding, role,
                          project.binding['tables'][role], inspected={})
            fields = project.fields
            row = live_row(project, record_id, fields)
            revision, _ = validate_row(project, role, row)
            values = protected_fields(role, fields, row)
            book = bridge.load(path) if path.exists() else {'binding': project.identity, 'confirmations': []}
            require(set(book) == {'binding', 'confirmations'} and book['binding'] == project.identity
                    and isinstance(book['confirmations'], list), '已有确认属于另一份项目或字段绑定。')
            saved = book['confirmations']
            last = saved[-1] if saved else None
            require(not last or revision >= last['proof']['business_revision'], '事项版本回退，须先核实。')
            fresh = True
            if last and last['proof']['business_revision'] == revision:
                try:
                    head, original_values = record_history(project, role, record_id, last, row, values)
                    require(last['fields'] == original_values, '确认后的内容、人员或归属已经不同。')
                    seal, fresh = last, False
                except stage_action.Pending:
                    # A new, authentic native click may renew an invalidated approval.
                    # If material content changed, the project must identify it as a new version.
                    if role == 'work' and last['fields'].get(fields['owner']['id']) != values[fields['owner']['id']]:
                        raise
                    require(last['fields'] == values, '确认内容已改变，请更新版本并由指定的人重新确认。')
            if fresh:
                try:
                    seal = capture(project, role, record_id, row, values)
                except stage_action.Pending:
                    # Only a previously enrolled project archive can extend the original
                    # receiver's retention. The current row's state alone is never proof.
                    import stage_source
                    if stage_source.location(project) is None:
                        raise
                    seal = archived_click(project, role, record_id, row, values)
                require(not last or (seal['proof']['table_revision'] > last['proof']['table_revision']
                        and (not seal['proof']['event_id'] or seal['proof']['event_id'] != last['proof']['event_id'])),
                        '这次没有新的本人确认，旧确认不能恢复。')
                head, seal['fields'] = record_history(project, role, record_id, seal, row, values)
            project.check_binding()
            require(live_row(project, record_id, fields) == row and project.history(record_id)[0] == head
                    and not os.path.lexists(Path(project.paths[1]) / 'PAUSED'),
                    '核对期间事项或项目状态又有变化。')
            if fresh:
                saved.append(seal)
                stage_action.save(path, book)
            return dict(seal['proof'], confirmation_sha256=digest([project.identity, seal]),
                        delivery_evidence_checked=False, linked_work_checked=False)
    except stage_action.Pending as exc:
        return {'status': 'pending', 'reason': str(exc)}
    except (KeyError, TypeError, AttributeError, OSError, ValueError, OnboardingError, sqlite3.Error, subprocess.SubprocessError):
        return {'status': 'pending', 'reason': '本版的真实点击、项目或记录历史尚未核实。'}


def record_ids(row, key):
    found = ids(row[key])
    require(len(found) == len(set(found)) and all(re.fullmatch(r'rec[A-Za-z0-9]+', i) for i in found),
            '关联记录缺少唯一的真实编号。')
    return sorted(found)


def labels(row, key):
    value = row.get(key)
    require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], str), '事项的选项不明确。')
    return value[0]


def components(value):
    require(isinstance(value, dict) and value and all(isinstance(k, str) and k.strip()
            and isinstance(v, str) and re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', v)
            for k, v in value.items()), '未核实各仓库的完整提交或制品摘要。')
    return value


def contains_build(target, actual):
    return all(components(target).get(k) == v for k, v in components(actual).items())


class Business:
    def __init__(self, config_path, key):
        self.p = stage_action.Project(config_path, key)
        self.key = key
        self.tables = self.p.binding['tables']
        self.rows, self.live, self.approvals, self.proofs = {}, {}, {}, {}
        self.source_cache = {}

    def row(self, role, record):
        key = (role, record)
        if key not in self.rows:
            table = self.tables[role]
            require(role == 'projects' or table['id'] in self.p.config['table_ids'],
                    '本服务没有绑定关联事项所在的表。')
            if role not in self.live:
                self.live[role] = watch._fields(self.p.reader, self.p.binding, role, table, inspected={})
            fields = table['fields']
            raw = self.p.reader.read_record(self.p.token, table['id'], record, list(fields.values()))
            require(raw['record_id'] == record and set(raw['values']) == {f['id'] for f in fields.values()},
                    '没有完整读到关联事项。')
            row = {k: raw['values'][f['id']] for k, f in fields.items()}
            if role != 'projects':
                require(record_ids(row, 'project') == [self.p.project] and row['nature'] == ['当前工作']
                        and not row.get('merged_into'), '有一条关联记录属于其他项目、历史记录或已归并。')
            self.rows[key] = row
        return self.rows[key]

    def links(self, role, record, field, target):
        from onboard import link_target
        row = self.row(role, record)
        mapped = self.tables[role]['fields'][field]
        actual = self.live[role][mapped['id']]
        expect = self.tables[target]['id']
        require(mapped.get('type') == 'link' and mapped.get('target_table_id') == expect
                and actual.get('type') == 'link' and link_target(actual) == expect,
                '有一个关联字段的实际目标表已变。')
        return record_ids(row, field)

    def approved(self, role, record):
        if record not in self.approvals:
            proof = confirmation_for(self.p.path, role, record)
            require(proof.get('status') == 'verified', proof.get('reason') or '缺少关联事项的真实人工确认。')
            self.approvals[record] = (role, proof['business_revision'], proof['confirmation_sha256'])
        require(self.approvals[record][0] == role, '关联确认的记录角色冲突。')
        return self.approvals[record][1]

    def source(self, kind, role, record):
        import stage_source
        key = (kind, role, record)
        if key not in self.source_cache:
            self.source_cache[key] = stage_source.query(self.p, kind, role, record)
        return self.source_cache[key]

    def project_open(self):
        row = self.row('projects', self.p.project)
        require(row['number'] == self.p.binding.get('project_number') and row['status'] in (['准备中'], ['进行中']),
                '项目编号不符，或项目已经暂停或结束。')

    def feedback(self, role, record):
        linked = self.links(role, record, 'feedback', 'feedback')
        require(linked, '这条事项尚未关联原反馈。')
        for feedback in linked:
            row = self.row('feedback', feedback)
            require(row['status'] not in (['已归并'], ['已撤回'], ['已答复'])
                    and record in self.links('feedback', feedback, role, role),
                    '反馈已结束，或双方记录没有互相关联。')
        return linked

    def work_parents(self, work):
        row = self.row('work', work)
        require(row['processing'] == ['正常'] and len(ids(row['owner'])) == 1, '研发项暂停、受阻或负责人不明确。')
        own_feedback = set(self.feedback('work', work))
        parents = []
        parent_feedback = set()
        for role in ('requirements', 'bugs'):
            for record in self.links('work', work, role, role):
                parent = self.row(role, record)
                require(parent['processing'] == ['正常'] and work in self.links(role, record, 'work', 'work'),
                        '上游事项已暂停或没有关联回这条研发项。')
                self.approved(role, record)
                original = set(self.feedback(role, record))
                require(own_feedback & original, '研发项与上游事项没有共同来源反馈。')
                parent_feedback.update(original)
                parents.append((role, record))
        require(parents, '此项没有关联需求或 Bug。项目允许的独立维护项按原流程处理。')
        require(own_feedback <= parent_feedback, '研发项关联了无法对应到上游事项的反馈。')
        return parents

    def previous(self, work, key):
        """Verify the already recorded transition against this exact live row's history."""
        project = stage_action.Project(self.p.path, key)
        require(project.identity == self.p.identity, '关联工作属于另一个项目绑定。')
        path = project.directory / 'stage-actions' / (project.table + '-' + work + '.json')
        if not path.is_file():
            self.archived_work(project, work, key)
            return
        import stat
        stage_action.private(path.parent, stat.S_ISDIR)
        stage_action.private(path, stat.S_ISREG)
        project.check_rule()
        row = self.row('work', work)
        revision = self.approved('work', work)
        ledger = bridge.load(path)
        require(ledger.get('format') == 1, '关联研发项的动作记录格式未知。')
        matches = [i for i in ledger['intents'] if i.get('key') == key and 'result' in i and
                   i['identity'] == project.identity and i['business']['revision'] == revision and
                   i['business']['confirmation_sha256'] == self.approvals[work][2]]
        if not matches:
            require(not any(i.get('key') == key and i.get('business', {}).get('revision') == revision
                            for i in ledger['intents']), '本版研发项还有未核实或已失效的历史请求。')
            self.archived_work(project, work, key)
            return
        require(len(matches) == 1, '找不到本版研发计划的唯一开工或交付记录。')
        intent = matches[0]
        result = intent['result']
        require(intent['rule_digest'] == digest(project.rule) and result['record_id'] == work
                and result['workflow_id'] == project.rule['workflow_id']
                and result['definition_sha256'] == project.rule['definition_sha256']
                and result['status'] == 'completed', '研发项原来的动作与本版规则不符。')
        history = project.history(work, intent['head_revision'])
        window = history[-3:]
        require(len(window) == 3 and digest(window[-1]) == intent['head_digest']
                and window[0]['rev'] == result['transition_revision'] and window[1]['rev'] == result['marker_revision']
                and window[0]['operator'] == '多维表格助手' and window[1]['operator'] == project.bot_name,
                '原动作和实际行历史不一致。')
        for item, field, before, after in (
            (window[0], project.rule['stage_field_id'], project.rule['source']['name'], project.rule['target']['name']),
            (window[1], project.rule['marker_field_id'], intent['previous_marker'], intent['marker']),
        ):
            changes = item.get('field_changes')
            require(isinstance(changes, list) and len(changes) == 1 and changes[0].get('field_id') == field
                    and changes[0].get('before') == before and changes[0].get('after') == after,
                    '关联研发项的动作不是已核实的单次变化。')
        require(row['status'] in (['进行中'], ['已交付']), '关联研发项尚未开始。')
        self.proofs[(key, work)] = result['transition_revision']

    def archived_work(self, project, work, key):
        import stage_source
        require(stage_source.location(project) is not None, '关联研发项缺少可核实的实际开工或交付记录。')
        proof = stage_source.query(project, 'prior_action', 'work', work, prior_step=key)
        row = self.row('work', work)
        plan = self.approved('work', work)
        owners = ids(row['owner'])
        require(len(owners) == 1 and proof.get('plan_revision') == plan and proof.get('owner_open_id') == owners[0],
                '原执行记录与当前负责人或这版计划不符。')
        if proof.get('method') == 'native_owner_click':
            event = proof.get('event')
            require(isinstance(event, dict) and type(event.get('event_revision')) is int, '缺少原负责人当时的事件。')
            item = project.history(work, event['event_revision'])[-1]
            owner_transition(project, row, work, key, item, proof)
            self.proofs[(key, work)] = item['rev']
            return
        require(proof.get('method') == 'registered_marker' and type(proof.get('head_revision')) is int
                and proof['head_revision'] > 0, '没有核实原动作的实际来源。')
        reg = project.rule
        project.check_rule()
        history = project.history(work, proof['head_revision'])
        require(len(history) >= 3 and digest(history[-1]) == proof.get('head_sha256'), '原执行前的历史版本不符。')
        transition, marker = history[-3:-1]
        require(proof.get('workflow_id') == reg['workflow_id'] and proof.get('definition_sha256') == reg['definition_sha256']
                and proof.get('request_actor_open_id') == project.bot and proof.get('owner_open_id') == project.bot
                and proof.get('marker_revision') == marker['rev'] and proof.get('transition_revision') == transition['rev']
                and marker.get('operator') == project.bot_name and transition.get('operator') == '多维表格助手',
                '原执行人与规则、请求或节点版本不符。')
        for item, field, kind, before, after in (
            (marker, reg['marker_field_id'], 'Text', proof.get('previous_marker'), reg['marker_text']),
            (transition, reg['stage_field_id'], 'Stage', reg['source']['name'], reg['target']['name']),
        ):
            changes = item.get('field_changes')
            require(item.get('activity_type') == 'update' and isinstance(changes, list) and len(changes) == 1
                    and changes[0].get('field_id') == field and changes[0].get('field_type') == kind
                    and changes[0].get('before') == before and changes[0].get('after') == after,
                    '原执行记录未能对应实际标记和紧接的一次节点变化。')
        self.proofs[(key, work)] = transition['rev']

    def target(self, role, record, *, ready=False, baseline=False):
        import test_deployment
        t = self.source('target', role, record)
        require(t.get('revision') == self.approved(role, record) and type(t['revision']) is int
                and isinstance(t.get('build'), str) and t['build'].strip()
                and t.get('environment') in ('本地', '测试', '预发布', '生产') and components(t.get('components')),
                '缺少这版业务在约定环境中的不可变构建。')
        row = self.row(role, record)
        if role == 'bugs':
            require(t.get('reference') == row['baseline' if baseline else 'candidate_build']
                    and bool(t['reference']), '实际构建未能对应本 Bug 的复现基线或候选构建。')
        if ready:
            require(t.get('delivery_mode') in ('deployment', 'artifact'), '尚未核实本项目的验收环境或固定制品。')
            if t['delivery_mode'] == 'deployment':
                deployment = t.get('deployment')
                require(isinstance(deployment, dict) and isinstance(deployment.get('expected'), dict),
                        '未取得本次目标部署和健康检查。')
                expected = deployment['expected']
                checked = test_deployment.check(expected, deployment.get('before'), deployment.get('after'))
                require(checked['ok'] and {k: v['sha'] for k, v in expected['repositories'].items()} == t['components']
                        and deployment.get('environment_label') == t['environment'],
                        '实际部署版本、环境或健康检查不符合本次目标。')
            else:
                require(t.get('verified_artifact') is True, '没有从实际固定制品核对本次构建。')
        tests = t.get('tests')
        require(isinstance(tests, dict) and set(tests) <= {'reproduction', 'self', 'independent', 'integration', 'manual'}
                and all(isinstance(v, list) and all(isinstance(i, str) and re.fullmatch(r'rec[A-Za-z0-9]+', i)
                        for i in v) and len(v) == len(set(v)) for v in tests.values()),
                '没有核实本版需要哪些真实测试记录。')
        self.proofs[('target', role, record)] = [t['components'], t['environment'], t['build'], tests]
        return t

    def timestamp(self, test, row):
        import json
        fields = self.tables['verification']['fields']
        table = self.tables['verification']['id']
        endpoint = f'/open-apis/bitable/v1/apps/{self.p.token}/tables/{table}/records/{test}'
        result = self.p.reader.runner(['lark-cli', 'api', 'GET', endpoint, '--as', 'bot', '--profile',
                                       self.p.actor['profile']], capture_output=True, text=True, timeout=30)
        data = self.p.reader.base._envelope(result, '核实真实执行时间')
        raw = data['record']
        when = raw['fields'].get(fields['executed_at']['name'])
        require(json.loads(result.stdout).get('identity') == 'bot' and raw['record_id'] == test
                and type(when) is int and when > 0, '测试记录缺少可核实的实际执行时间。')
        return when

    def test(self, rid, purpose, result, target, subjects):
        row = self.row('verification', rid)
        p = self.source('test', 'verification', rid)
        purposes = (purpose,) if isinstance(purpose, str) else purpose
        require(labels(row, 'purpose') in purposes and row['result'] == [result] and p.get('result') == result
                and row['purpose'] == [p.get('purpose')] and row['method'] == [p.get('method')]
                and row['build'] == p.get('build') == target['build']
                and row['environment'] == [p.get('environment')] == [target['environment']]
                and p.get('components') == target['components'] and p.get('key') == row['key'],
                '测试记录与本次需验证的用途、结果、环境或构建不一致。')
        require(confirmation.inline_text(row.get('criteria_snapshot')) and confirmation.inline_text(row.get('summary'))
                and confirmation.inline_text(row.get('evidence')) and p.get('criteria_sha256') == digest(row['criteria_snapshot'])
                and p.get('evidence_sha256') == digest(row['evidence']) and row.get('executor') == p.get('executor')
                and bool(row['executor']), '原始测试材料和表里的执行者、标准或证据不一致。')
        if p.get('observation') == 'reproduced_failure':
            require(p['purpose'] == 'Bug复现' and result == '失败' and type(p.get('observed_ms')) is int
                    and self.timestamp(rid, row) == p['observed_ms'] and 'expected' in p and 'observed' in p
                    and p['expected'] != p['observed'] and p.get('verified_input') is True,
                    '原构建复现须有真实的输入、不同的预期和实际结果，以及对应的观察时间。')
        else:
            require(all(isinstance(p.get(k), str) and p[k].strip() for k in ('executor_id', 'session_id'))
                    and type(p.get('started_ms')) is int and type(p.get('ended_ms')) is int
                    and 0 < p['started_ms'] <= p['ended_ms'],
                    '测试缺少实际执行身份或完整的起止时间。')
            # Older project writers deliberately stored only whole minutes in Feishu.
            # The enrolled reader must verify the original timestamp and its encoding.
            precision = p.get('feishu_time_precision', 'millisecond')
            require(precision in ('millisecond', 'minute'), '项目没有登记这种测试时间精度。')
            recorded = p['ended_ms'] if precision == 'millisecond' else (p['ended_ms'] // 60000) * 60000
            require(self.timestamp(rid, row) == recorded, '飞书执行时间与原始测试报告不一致。')
        for (role, record), field in subjects.items():
            require(record in self.links('verification', rid, field, role)
                    and p.get('subject_revisions', {}).get(record) == self.approved(role, record),
                    '测试证据没有和本轮事项及修订对应。')
        self.proofs[('test', rid)] = [p['report_sha256'], p['components'], p.get('executor_id'), p.get('session_id'),
                                      p.get('started_ms'), p.get('ended_ms'), p.get('observed_ms')]
        return p

    def reproduction(self, bug):
        target = self.target('bugs', bug, baseline=True)
        required = target['tests'].get('reproduction', [])
        require(required and set(required) <= set(self.links('bugs', bug, 'reproduction_tests', 'verification')),
                'Bug 缺少与原构建相连的实际复现记录。')
        for test in required:
            self.test(test, 'Bug复现', '失败', target, {('bugs', bug): 'reproduced_bugs'})

    def delivery(self, work):
        target = self.target('work', work)
        required = target['tests'].get('self', [])
        require(required and confirmation.inline_text(self.row('work', work)['summary']),
                '研发项缺少实际交付说明，或没有本次构建的自测。')
        return target, [self.test(test, '研发自测', '通过', target, {('work', work): 'work'}) for test in required]

    def work(self, work):
        row = self.row('work', work)
        require(ids(row['owner']) == [self.p.bot], '这条研发项由其他人负责。')
        self.approved('work', work)
        # Check standalone work before requiring a feedback source.
        require(self.links('work', work, 'requirements', 'requirements') or self.links('work', work, 'bugs', 'bugs'),
                '这是独立维护项，继续使用项目原有的维护流程。')
        for role, record in self.work_parents(work):
            stage = labels(self.row(role, record), 'status')
            require(stage in ('可研发', '研发中') and (self.key != 'work-deliver' or stage == '研发中'),
                    '关联的需求或 Bug 还没有到这一步。')
            if role == 'bugs' and self.key == 'work-start':
                self.reproduction(record)
        deps = self.links('work', work, 'dependencies', 'work')
        for dep in deps:
            require(dep != work and self.row('work', dep)['status'] == ['已交付'], '有一条依赖研发项尚未交付。')
            self.previous(dep, 'work-deliver')
        if self.key == 'work-deliver':
            self.previous(work, 'work-start')
            self.delivery(work)

    def manual(self, test, role, record, works, target):
        row = self.row('verification', test)
        parent = self.row(role, record)
        verifier = ids(parent['acceptor'])
        plan = self.source('acceptance_plan', 'verification', test)
        subject = 'requirements' if role == 'requirements' else 'retested_bugs'
        require(len(verifier) == 1 and verifier[0] != self.p.bot, '还没有唯一的真人验收人。')
        confirmation.confirm_directory_user(self.p.reader, verifier[0])
        require(plan.get('acceptor') == verifier[0]
                and plan.get('subject_id') == record and plan.get('revision') == self.approved(role, record)
                and plan.get('key') == row['key'] and plan.get('criteria_sha256') == digest(row.get('criteria_snapshot'))
                and confirmation.inline_text(row.get('criteria_snapshot')) and row['purpose'] == [
                    '需求验收' if role == 'requirements' else 'Bug复测'] and row['result'] == ['未执行']
                and row['method'] == ['人工核验'] and row['environment'] == [target['environment']]
                and plan.get('components') == target['components'] and row['build'] == target['build']
                and record in self.links('verification', test, subject, role)
                and not any(row.get(k) for k in ('executor', 'executed_at', 'summary', 'evidence')),
                '没有给指定验收人准备好本版、尚未执行的人工验收记录。')
        work_links = set(self.links('verification', test, 'work', 'work'))
        require(work_links and work_links <= set(works), '待验收记录的研发项与本次范围不符。')
        self.proofs[('manual', test)] = [plan['criteria_sha256'], plan['acceptor'], sorted(work_links)]
        return work_links

    def parent(self, role, record):
        self.approved(role, record)
        self.feedback(role, record)
        parent = self.row(role, record)
        works = self.links(role, record, 'work', 'work')
        require(works, '还没有与这条事项互相关联的研发项。')
        acceptance = self.key.endswith('-accept')
        completed = []
        for work in works:
            child = self.row('work', work)
            require(record in self.links('work', work, role, role), '需求或 Bug 与研发项的关联没有双向对应。')
            if not acceptance and (child['status'] not in (['进行中'], ['已交付']) or child['processing'] != ['正常']):
                continue
            require(not acceptance or child['status'] == ['已交付'], '还有关联研发项没有交付。')
            self.work_parents(work)
            self.approved('work', work)
            self.previous(work, 'work-deliver' if acceptance else 'work-start')
            completed.append(work)
        require(completed, '还没有关联研发项实际开工。')
        if not acceptance:
            if role == 'bugs':
                self.reproduction(record)
            return
        target = self.target(role, record, ready=True)
        test_subject = 'requirements' if role == 'requirements' else 'retested_bugs'
        relation = 'verification' if role == 'requirements' else 'retest_tests'
        linked = set(self.links(role, record, relation, 'verification'))
        tests = target['tests']
        developers = {}
        for work in completed:
            child_target, observed = self.delivery(work)
            require(contains_build(target['components'], child_target['components']),
                    '研发项自测与本次送验构建不同。')
            developers[work] = observed
        require(type(target.get('require_independent')) is bool
                and (not (role == 'bugs' or target['require_independent']) or tests.get('independent')),
                '缺少本次需要的独立检查或 Bug 修复后复测。')
        for group in ('independent', 'integration'):
            for test in tests.get(group, []):
                require(test in linked, '项目登记的复测或集成检查没有关联当前事项。')
                work_links = self.links('verification', test, 'work', 'work')
                require(work_links and set(work_links) <= set(works), '复测或集成记录缺少适用研发项的真实关联。')
                subjects = {(role, record): test_subject, **{('work', w): 'work' for w in work_links}}
                purposes = ('Bug复测',) if role == 'bugs' else ('需求验收', '交付验证')
                result = self.test(test, purposes, '通过', target, subjects)
                if group == 'independent':
                    require(all(result['executor_id'] != d['executor_id'] and result['session_id'] != d['session_id']
                                and result['started_ms'] >= d['ended_ms'] for w in work_links for d in developers[w]),
                            '独立检查须由另一位实际执行者在其覆盖的研发项自测后完成。')
        invitations = tests.get('manual', [])
        require(invitations and set(invitations) <= linked, '尚未关联本轮的人工验收记录。')
        covered = set()
        for test in invitations:
            covered.update(self.manual(test, role, record, works, target))
        require(covered == set(works), '人工验收记录尚未覆盖所有适用的研发项。')


def verify(config_path, key, record_id):
    """Project business adapter used by the shipped three-argument stage-action CLI."""
    require(key in rules.STAGE_REQUESTS, '没有登记这种项目动作。')
    adapter = Business(config_path, key)
    adapter.project_open()
    role = rules.STAGE_REQUESTS[key][0]
    row = adapter.row(role, record_id)
    require(row['processing'] == ['正常'], '事项已暂停、受阻或在等待前置工作。')
    if role == 'work':
        adapter.work(record_id)
    else:
        adapter.parent(role, record_id)
    revision = adapter.approved(role, record_id)
    adapter.p.check_binding()
    # The current row's expected stage and marker change during submission.
    # Only the pinned authorization, relations and immutable execution evidence
    # take part in this digest, not an observation timestamp or polling counter.
    relation_state = []
    for (table_role, ident), value in sorted(adapter.rows.items()):
        if table_role in ('work', 'requirements', 'bugs', 'feedback'):
            linked = {k: record_ids(value, k) for k, f in adapter.tables[table_role]['fields'].items()
                      if f['type'] == 'link' and k in value}
            relation_state.append((table_role, ident, linked))
    facts = [adapter.p.identity, sorted(adapter.approvals.items()), relation_state, sorted(adapter.proofs.items())]
    return stage_action.BusinessEvidence(adapter.p.project, adapter.p.table, record_id, key, revision, digest(facts))
