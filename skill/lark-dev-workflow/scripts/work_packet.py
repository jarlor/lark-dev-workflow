"""Deterministic views of complete watch snapshots, never business decisions.

The caller retains both complete snapshots and supplies their paths separately.
An absent object is only absent from this observation, never proven deleted or
finished. No author, solved-state, age, or business-status filter is applied.
"""
from __future__ import annotations

import copy
import json


INDEX_FIELDS = ('number', 'title', 'status', 'human_action', 'next_step', 'nature')
PREVIEW_LIMIT = 160


def _pointer(*parts):
    return ''.join('/' + str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def _changes(before, after, path=''):
    """Lossless object diff; lists are atomic, missing and null stay distinct."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            child = path + _pointer(key)
            if key not in before:
                result.append({'path': child, 'kind': 'added', 'after': copy.deepcopy(after[key])})
            elif key not in after:
                result.append({'path': child, 'kind': 'absent', 'before': copy.deepcopy(before[key])})
            else:
                result.extend(_changes(before[key], after[key], child))
        return result
    # JSON distinguishes booleans from numbers even though Python equality does not.
    if (type(before) is not type(after)
            or json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True)):
        return [{'path': path, 'kind': 'modified',
                 'before': copy.deepcopy(before), 'after': copy.deepcopy(after)}]
    return []


def _thread_key(comment):
    content = comment['content']
    return (f"{content['resource_type']}:{content['resource_token']}"
            f"/comment:{content['comment_id']}")


def _known_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or type(snapshot.get('format_version')) is not int
            or snapshot['format_version'] != 1):
        return False
    if not all(isinstance(snapshot.get(key), dict) for key in ('records', 'comments')):
        return False
    if not isinstance(snapshot.get('notices', {}), dict):
        return False
    for key, record in snapshot['records'].items():
        if (not isinstance(key, str) or len(key.split('/')) != 2
                or not all(key.split('/')) or not isinstance(record, dict)
                or not isinstance(record.get('role'), str)
                or not isinstance(record.get('fields'), dict)):
            return False
    for key, comment in snapshot['comments'].items():
        if not isinstance(comment, dict):
            return False
        content = comment.get('content')
        if (not isinstance(content, dict)
                or not all(isinstance(content.get(k), str) and content[k] for k in
                           ('resource_type', 'resource_token', 'comment_id', 'reply_id'))
                or not all(isinstance(comment.get(k), dict) for k in ('location', 'thread'))
                or not isinstance(comment.get('revision'), str)
                or key != _thread_key(comment) + '/reply:' + content['reply_id']):
            return False
    return True


def _mapped_tables(binding, snapshot):
    if not isinstance(binding, dict) or not isinstance(binding.get('tables'), dict):
        return None
    tables = binding['tables']
    for role, table in tables.items():
        if (not isinstance(table, dict) or not isinstance(table.get('id'), str)
                or not isinstance(table.get('fields'), dict)):
            return None
        ids = []
        for field in table['fields'].values():
            if not isinstance(field, dict) or not isinstance(field.get('id'), str):
                return None
            ids.append(field['id'])
        if len(ids) != len(set(ids)):
            return None
    if len({table['id'] for table in tables.values()}) != len(tables):
        return None
    for key, record in snapshot['records'].items():
        table = tables.get(record['role'])
        if (table is None or table['id'] != key.split('/')[0]
                or not set(record['fields']) <= {f['id'] for f in table['fields'].values()}):
            return None
    return tables


def _preview(value):
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(rendered) > PREVIEW_LIMIT:
        return {'preview': rendered[:PREVIEW_LIMIT], 'truncated': True}
    return copy.deepcopy(value)


def _record_index(snapshot, tables):
    result = {}
    for key, record in sorted(snapshot['records'].items()):
        fields = tables[record['role']]['fields']
        values = record['fields']
        entry = {'role': record['role']}
        for name in INDEX_FIELDS:
            field_id = fields.get(name, {}).get('id')
            if field_id in values:
                entry[name] = _preview(values[field_id])
        # Links are located, not traversed. A linked target may be outside the
        # collected scope; neither its identity nor its contents is guessed.
        relations = {name: {'field_id': field['id'],
                            'target_table_id': field.get('target_table_id')}
                     for name, field in sorted(fields.items()) if field.get('type') == 'link'
                     and field['id'] in values and values[field['id']] not in (None, [])}
        if relations:
            entry['relations'] = relations
        result[key] = entry
    return result


def _threads(snapshot):
    grouped = {}
    for key, comment in sorted(snapshot['comments'].items()):
        grouped.setdefault(_thread_key(comment), {})[key] = comment
    return grouped


def _thread_index(grouped):
    result = {}
    for key, replies in sorted(grouped.items()):
        locations = []
        states = []
        for reply in replies.values():
            if reply['location'] not in locations:
                locations.append(copy.deepcopy(reply['location']))
            if reply['thread'] not in states:
                states.append(copy.deepcopy(reply['thread']))
        result[key] = {'reply_count': len(replies), 'locations': locations, 'states': states}
    return result


def _record_location(comment):
    location = comment['location']
    if location.get('table_id') and location.get('record_id'):
        return f"{location['table_id']}/{location['record_id']}"
    return None


def _metadata(snapshot):
    return {k: copy.deepcopy(v) for k, v in sorted(snapshot.items())
            if k not in ('records', 'comments', 'observed_at')}


def build_work_packet(current_snapshot, previous_snapshot, binding):
    """Return a JSON-serializable work view without I/O or input mutation.

    ``previous_snapshot`` must be the complete observation identified by the
    acknowledged scheduling checkpoint, not the last scan or interrupted run.
    Pass None when that observation is unavailable. The function cannot verify
    the checkpoint hash; the caller must. Current mapped IDs explain the short
    index only; historical mapping equality is not inferred from one binding.

    ``baseline`` includes existing objects without labelling them new. ``delta``
    contains exact field changes and complete current threads for changed
    comments/records, plus indexes of *all* currently observed records/threads.
    Unsupported inputs use ``full_fallback`` with the untouched current snapshot.
    """
    packet = {
        'format_version': 1,
        'lookup': {
            'authority': 'Complete local snapshots remain authoritative; verify live values before writes.',
            'record': 'snapshot.records[record_key]; fields use binding.tables[role].fields[name].id.',
            'thread': 'Select all snapshot.comments entries whose content resource_type, resource_token '
                      'and comment_id match thread_key; include solved threads and all authors.',
            'previous': 'Read the checkpoint snapshot for previous_pointer and edited/absent originals; '
                        'older revisions remain in the local comments observation journal.',
            'scope': 'Indexes cover all currently observed records, not a completion or execution queue. '
                     'Absence does not prove deletion, resolution, or permission to follow a moved source.',
        },
        'schema_comparison': 'Only the current binding was supplied; historical mapping equality is unknown. '
                             'Snapshot schema/access/coverage changes, when present, appear in metadata.',
    }
    if not _known_snapshot(current_snapshot):
        return {**packet, 'mode': 'full_fallback', 'reason': 'unsupported_current_snapshot',
                'snapshot': copy.deepcopy(current_snapshot)}
    tables = _mapped_tables(binding, current_snapshot)
    if tables is None:
        return {**packet, 'mode': 'full_fallback', 'reason': 'unsupported_current_mapping',
                'snapshot': copy.deepcopy(current_snapshot)}
    current_threads = _threads(current_snapshot)
    packet.update(record_index=_record_index(current_snapshot, tables),
                  thread_index=_thread_index(current_threads),
                  metadata={'current': _metadata(current_snapshot)})
    if _known_snapshot(previous_snapshot):
        packet['metadata']['changes'] = _changes(_metadata(previous_snapshot), _metadata(current_snapshot))
    if previous_snapshot is None:
        reason = 'no_checkpoint_snapshot'
    elif not _known_snapshot(previous_snapshot):
        reason = 'unsupported_checkpoint_snapshot'
    elif previous_snapshot.get('binding') != current_snapshot.get('binding'):
        reason = 'snapshot_binding_changed'
    else:
        reason = None
    if reason:
        packet.update(mode='baseline' if previous_snapshot is None else 'full_fallback', reason=reason,
                      records={'current': copy.deepcopy(current_snapshot['records'])},
                      comments={'current': sorted(current_snapshot['comments'])},
                      threads=copy.deepcopy(current_threads))
        return packet

    packet.update(mode='delta', reason='compared_with_checkpoint_snapshot')
    old_records, records = previous_snapshot['records'], current_snapshot['records']
    added = sorted(records.keys() - old_records.keys())
    absent = sorted(old_records.keys() - records.keys())
    modified = {}
    for key in sorted(records.keys() & old_records.keys()):
        changes = _changes(old_records[key], records[key])
        if changes:
            modified[key] = changes
    packet['records'] = {
        'added': {key: copy.deepcopy(records[key]) for key in added},
        'modified': modified,
        'absent': {key: {'status': 'not_in_current_snapshot',
                         'previous_pointer': _pointer('records', key)} for key in absent},
    }

    old_comments, comments = previous_snapshot['comments'], current_snapshot['comments']
    added_comments = sorted(comments.keys() - old_comments.keys())
    restored_comments = sorted(set(added_comments) & previous_snapshot.get('notices', {}).keys())
    absent_comments = sorted(old_comments.keys() - comments.keys())
    modified_comments = {}
    for key in sorted(comments.keys() & old_comments.keys()):
        changes = _changes(old_comments[key], comments[key])
        if changes:
            # The exact current content appears once, in its complete thread.
            modified_comments[key] = {'changed_paths': [c['path'] for c in changes],
                                      'previous_pointer': _pointer('comments', key)}
    packet['comments'] = {
        'added': [key for key in added_comments if key not in restored_comments],
        'restored': restored_comments,
        'modified': modified_comments,
        'absent': {key: {'observation': copy.deepcopy(current_snapshot.get('notices', {}).get(
                            key, {'status': 'not_in_current_snapshot'})),
                         'previous_pointer': _pointer('comments', key)} for key in absent_comments},
    }
    changed_records = set(added) | set(modified) | set(absent)
    relevant_threads = {_thread_key(comments[key]) for key in added_comments + list(modified_comments)}
    relevant_threads.update(_thread_key(old_comments[key]) for key in absent_comments)
    for snapshot in (previous_snapshot, current_snapshot):
        relevant_threads.update(_thread_key(comment) for comment in snapshot['comments'].values()
                                if _record_location(comment) in changed_records)
    packet['threads'] = {key: copy.deepcopy(current_threads[key]) for key in sorted(relevant_threads)
                         if key in current_threads}
    return packet
