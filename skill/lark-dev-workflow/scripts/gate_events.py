"""Narrow record membership and event corroboration for a tenant-pinned bot runner."""
import hashlib
import json
import re
import time
import uuid

from onboard import atomic_json, read_json


INDEX = 'gate-event-scope.json'
BATCH = 'gate-event-batch.json'
WORK = 'gate-event-work.json'
LIMIT = 256
MAX_AGE = 86400
FIELDS = {'requirements': ('revision', 'confirmed_revision'), 'bugs': ('revision', 'confirmed_revision'),
          'work': ('plan_revision', 'confirmed_plan_revision')}


def signature(binding, tenant):
    value = {k: binding[k] for k in ('base', 'scope', 'cli_actor', 'tables')}
    return hashlib.sha256(json.dumps([tenant, value], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evidence_pins(binding, tenant):
    return dict(app_id=binding['cli_actor']['app_id'], tenant_key=tenant, base_token=binding['base']['token'],
                project_record_id=binding['scope']['project_record_id'])


def option_names(entries):
    """Unambiguous option names and IDs from the last complete live scan."""
    options = {}
    for entry in entries:
        name = entry.get('name') if isinstance(entry, dict) else entry
        identifier = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(name, str) or not 0 < len(name) <= 128 or name in options:
            return {}
        options[name] = name
        if identifier is not None:
            if not isinstance(identifier, str) or not 0 < len(identifier) <= 128 or identifier in options:
                return {}
            options[identifier] = name
    return options


def project_records(binding, snapshot):
    """Check the real project link again, independent of the event's table ID."""
    scope = binding['scope']
    if (scope.get('mode') != 'project_record' or snapshot.get('format_version') != 1
            or snapshot.get('binding', {}).get('scope') != scope
            or snapshot['binding'].get('base_token') != binding['base']['token']
            or snapshot['binding'].get('cli_actor') != binding['cli_actor']
            or not isinstance(snapshot.get('coverage'), dict)):
        return {}
    rows = snapshot['records']
    project_id = scope['project_record_id']
    if rows.get(scope['project_table_id'] + '/' + project_id, {}).get('role') != 'projects':
        return {}
    tables = {}
    for role, revision_names in FIELDS.items():
        table = binding['tables'].get(role, {})
        fields = table.get('fields', {})
        status = fields.get('status', {})
        project = fields.get('project', {})
        if (status.get('type') not in ('select', 'stage') or project.get('type') != 'link'
                or project.get('target_table_id') != scope['project_table_id']):
            continue
        table_id, status_id = table['id'], status['id']
        actual = snapshot.get('gate_status_options', {}).get(table_id + '/' + status_id, [])
        options = option_names(actual)
        if not options:
            continue
        tracked = {status_id: 'status'}
        tracked.update({fields[k]['id']: 'revision' for k in revision_names
                        if fields.get(k, {}).get('type') == 'number'})
        members = set()
        for key, row in rows.items():
            prefix, _, record = key.partition('/')
            if prefix != table_id or row.get('role') != role or not record:
                continue
            links = row['fields'].get(project['id'])
            if (isinstance(links, list) and len(links) == 1 and isinstance(links[0], dict)
                    and links[0].get('id') == project_id):
                members.add(record)
        tables[table_id] = dict(project_field=project['id'], fields=tracked, options=options, records=sorted(members))
    return tables


def publish_scope(config, binding, snapshot, directory, *, started_at=None):
    if config.get('event_tenant_key'):
        observed_at = int(time.time())
        started_at = observed_at if started_at is None else started_at
        scope = dict(signature=signature(binding, config['event_tenant_key']), scan=str(uuid.uuid4()),
                     observed_at=observed_at, started_at=started_at, tables=project_records(binding, snapshot))
        path = directory / INDEX
        previous = read_json(path) if path.is_file() else {}
        if (previous.get('signature') != scope['signature'] or
                not started_at - MAX_AGE <= previous.get('observed_at', 0) <= started_at):
            previous = {}
        for table_id, table in scope['tables'].items():
            prior_table = previous.get('tables', {}).get(table_id, {})
            prior = prior_table.get('since', {}) if prior_table.get('options') == table['options'] else {}
            table['since'] = {record: max(prior.get(record, observed_at), observed_at - MAX_AGE)
                              for record in table['records']}
        atomic_json(directory / INDEX, scope)
        return scope


def parsed_value(raw, kind, options):
    if not isinstance(raw, str) or len(raw) > 512:
        raise ValueError('Unsupported event value')
    value = json.loads(raw)
    if value is None:
        return None
    if kind == 'revision' and type(value) is int and 0 < value < 2**53:
        return value
    if kind == 'status':
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, str) and value in options:
            return options[value]
    raise ValueError('Unknown status or revision representation')


def pending(directory, scope, state):
    if not scope or not (directory / BATCH).is_file():
        return [], None
    try:
        batch = read_json(directory / BATCH)
    except (ValueError, OSError):
        state['optimization_note'] = 'Gate event observations unreadable; verify gates against original sources.'
        return [], None
    if (not isinstance(batch, dict) or batch.get('signature') != scope['signature']
            or not isinstance(batch.get('journal_id'), str) or type(batch.get('generation')) is not int
            or batch['generation'] < 0 or not isinstance(batch.get('rows'), list)):
        return [], None
    checkpoint = [batch['journal_id'], batch['generation']]
    prior = state.get('gate_event_checkpoint')
    if prior and prior[0] == checkpoint[0] and prior[1] >= checkpoint[1]:
        return [], None
    rows = []
    for item in batch['rows'][-LIMIT:]:
        if (not isinstance(item, dict) or set(item) != {'table_id', 'record_id', 'field_id', 'event_id',
                'operator_open_id', 'event_revision', 'update_time', 'before', 'after', 'generation'}
                or any(not isinstance(item[k], str) for k in
                       ('table_id', 'record_id', 'field_id', 'event_id', 'operator_open_id'))
                or not re.fullmatch(r'ou_[a-zA-Z0-9_]{1,128}', item['operator_open_id'])
                or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', item['event_id'])
                or any(type(item[k]) is not int or item[k] <= 0 for k in
                       ('event_revision', 'update_time', 'generation'))):
            continue
        table = scope['tables'].get(item['table_id'])
        if (not table or item['record_id'] not in table['records'] or item['field_id'] not in table['fields']
                or item['update_time'] <= table['since'][item['record_id']]
                or item['generation'] > checkpoint[1] or
                (prior and prior[0] == checkpoint[0] and item['generation'] <= prior[1])):
            continue
        kind = table['fields'][item['field_id']]
        try:
            previous, current = (parsed_value(json.dumps(item[k]), kind, table['options'])
                                 for k in ('before', 'after'))
        except (TypeError, ValueError):
            continue
        if previous != current:
            rows.append(dict(item, before=previous, after=current))
    return rows, checkpoint
