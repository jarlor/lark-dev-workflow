"""Derive an expected observation from exact, read-back worker writes.

Receipt v1 has exactly ``format_version``, ``run_id``, ``snapshot_id``,
``record_fields`` and ``comments``. Field entries have ``record_key``,
``field_id``, ``before``, ``after`` and ``readback_evidence``. Comment entries
have ``event_key``, ``before`` (the complete old watch value, or null for an
addition), ``after`` (the complete new watch value), and ``readback_evidence``.

Evidence is a nonempty reference to a private readback file or tool result.
The producer MUST include only writes it actually performed and read back in
this run; it must never infer its writes from a start-to-end snapshot diff.
In particular, a shared account does not prove worker authorship.
This pure function checks the receipt's consistency, NOT the truth, authorship,
existence or accessibility of its evidence. It grants no write authorization.

The caller may suppress a change wake only after a subsequent COMPLETE scan
equals this derived snapshot (ignoring only top-level observed_at). Never use
an end-of-run snapshot as the baseline, filter by author, or partially consume
a rejected receipt. No receipt means the caller must skip this mechanism.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math


class OwnEffectsError(ValueError):
    """The entire receipt is unusable; keep the original checkpoint."""


_RECEIPT_KEYS = {'format_version', 'run_id', 'snapshot_id', 'record_fields', 'comments'}
_FIELD_KEYS = {'record_key', 'field_id', 'before', 'after', 'readback_evidence'}
_COMMENT_KEYS = {'event_key', 'before', 'after', 'readback_evidence'}
_CONTENT_KEYS = {
    'resource_type', 'resource_token', 'comment_id', 'reply_id', 'root_reply',
    'author', 'body', 'text', 'create_time', 'update_time', 'raw_reply_extra',
}
_IDENTITY_KEYS = {
    'resource_type', 'resource_token', 'comment_id', 'reply_id', 'root_reply',
    'author', 'create_time',
}


def _require(condition, message):
    if not condition:
        raise OwnEffectsError(message)


def _nonempty(value):
    return type(value) is str and bool(value.strip())


def _object(value, keys, name):
    _require(type(value) is dict and set(value) == keys,
             name + ': missing or unknown properties.')


def _json_value(value):
    """Reject Python-only values, non-string keys and non-finite numbers."""
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float:
        _require(math.isfinite(value), 'Non-finite JSON number.')
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif type(value) is dict:
        _require(all(type(key) is str for key in value), 'JSON object keys must be strings.')
        for item in value.values():
            _json_value(item)
    else:
        raise OwnEffectsError('Expected a JSON value.')


def _canonical(value):
    # Unlike Python ==, this distinguishes true from 1 and 1 from 1.0.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _same(left, right):
    return _canonical(left) == _canonical(right)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _timestamp(value, *, optional=False):
    return ((optional and value is None) or type(value) is int or _nonempty(value))


def _comment(event_key, value):
    _require(_nonempty(event_key), 'Comment event_key is required.')
    _object(value, {'revision', 'context', 'content', 'location', 'thread'}, 'Comment')
    content = value['content']
    _object(content, _CONTENT_KEYS, 'Comment content')
    _require(all(_nonempty(content[key]) for key in (
        'resource_type', 'resource_token', 'comment_id', 'reply_id', 'author')),
        'Comment identities are required.')
    _require(content['resource_type'] in ('bitable', 'docx'), 'Unsupported comment resource.')
    expected_key = (f"{content['resource_type']}:{content['resource_token']}"
                    f"/comment:{content['comment_id']}/reply:{content['reply_id']}")
    _require(event_key == expected_key, 'Comment stable identity does not match event_key.')
    _require(type(content['root_reply']) is bool, 'Comment root_reply must be boolean.')
    _require(_timestamp(content['create_time'], optional=True)
             and _timestamp(content['update_time']), 'Invalid comment timestamps.')
    _require(type(content['body']) is dict and type(content['body'].get('elements')) is list,
             'Comment body.elements must be complete.')
    elements = content['body']['elements']
    fragments = []
    for element in elements:
        if type(element) is dict and type(element.get('text_run')) is dict:
            text = element['text_run'].get('text', '')
            _require(type(text) is str, 'Comment text_run.text must be a string.')
            fragments.append(text)
    _require(type(content['text']) is str and content['text'] == ''.join(fragments),
             'Comment text differs from its normalized body.')
    _require(type(value['location']) is dict and value['location'].get('status') == 'in_scope'
             and not {'document_revision', 'mapping_evidence'} & set(value['location']),
             'Comment location must be a normalized in-scope observation.')
    _object(value['thread'], {'is_solved', 'solver_user_id'}, 'Comment thread')
    _require(value['thread']['is_solved'] is None
             or type(value['thread']['is_solved']) is bool, 'Invalid comment thread state.')
    _require(value['thread']['solver_user_id'] is None
             or type(value['thread']['solver_user_id']) is str, 'Invalid comment solver identity.')
    _require(value['revision'] == _digest({key: content[key]
             for key in ('author', 'body', 'update_time')}), 'Comment revision is inconsistent.')
    _require(value['context'] == _digest({'thread': value['thread'], 'location': value['location']}),
             'Comment context is inconsistent.')


def expected_snapshot(start_snapshot, receipt, run_id, snapshot_id):
    """Apply one valid receipt to a copy; raise OwnEffectsError on any defect.

    Only existing record fields and exact comment additions/content edits are
    supported. Deletions, record creation, ownership moves, thread resolution,
    coverage changes and whole-record replacement cannot be acknowledged here.
    ``snapshot_id`` is the runner's SHA-256 of the starting snapshot with only
    its top-level ``observed_at`` removed.
    """
    try:
        return _expected_snapshot(start_snapshot, receipt, run_id, snapshot_id)
    except (RecursionError, UnicodeError) as exc:
        raise OwnEffectsError('Receipt or snapshot is not valid finite JSON.') from exc


def _expected_snapshot(start, receipt, run_id, snapshot_id):
    _json_value(start)
    _json_value(receipt)
    _require(type(start) is dict and type(start.get('format_version')) is int
             and start['format_version'] == 1, 'A format-1 starting snapshot is required.')
    _require({'records', 'comments'} <= set(start)
             and set(start) <= {'format_version', 'records', 'comments', 'binding',
                               'notices', 'coverage', 'observed_at'},
             'Unsupported starting snapshot properties.')
    _require(all(type(start[key]) is dict for key in
                 ('records', 'comments', 'binding', 'notices', 'coverage') if key in start),
             'Snapshot collections must be objects.')
    _require(_nonempty(run_id) and _nonempty(snapshot_id), 'Current run and snapshot IDs are required.')
    _object(receipt, _RECEIPT_KEYS, 'Receipt')
    _require(type(receipt['format_version']) is int and receipt['format_version'] == 1,
             'Unsupported receipt format_version.')
    _require(receipt['run_id'] == run_id and receipt['snapshot_id'] == snapshot_id,
             'Receipt belongs to a different run or snapshot.')
    _require(snapshot_id == _digest({k: v for k, v in start.items() if k != 'observed_at'}),
             'Snapshot ID does not identify the starting snapshot.')
    _require(type(receipt['record_fields']) is list and type(receipt['comments']) is list,
             'Receipt collections must be lists.')
    _require(bool(receipt['record_fields'] or receipt['comments']), 'Receipt contains no writes.')
    derived = copy.deepcopy(start)
    seen_fields, seen_comments = set(), set()
    for entry in receipt['record_fields']:
        _object(entry, _FIELD_KEYS, 'Field entry')
        _require(all(_nonempty(entry[key]) for key in
                     ('record_key', 'field_id', 'readback_evidence')),
                 'Field identity and readback evidence are required.')
        key, field = entry['record_key'], entry['field_id']
        _require((key, field) not in seen_fields, 'Duplicate field entry.')
        seen_fields.add((key, field))
        _require(key in start['records'], 'Field entry refers to an unknown record.')
        row = start['records'][key]
        _object(row, {'role', 'fields'}, 'Record')
        _require(_nonempty(row['role']) and type(row['fields']) is dict,
                 'Record role and fields are required.')
        _require(field in row['fields'], 'Field entry refers to an unknown field.')
        _require(_same(row['fields'][field], entry['before']), 'Field before value does not match.')
        _require(not _same(entry['before'], entry['after']), 'Field entry contains no change.')
        derived['records'][key]['fields'][field] = copy.deepcopy(entry['after'])
    for entry in receipt['comments']:
        _object(entry, _COMMENT_KEYS, 'Comment entry')
        key = entry['event_key']
        _require(_nonempty(key) and _nonempty(entry['readback_evidence']),
                 'Comment identity and readback evidence are required.')
        _require(key not in seen_comments, 'Duplicate comment entry.')
        seen_comments.add(key)
        _comment(key, entry['after'])
        if key in start['comments']:
            previous = start['comments'][key]
            _comment(key, previous)
            _require(_same(previous, entry['before']), 'Comment before value does not match.')
            _require(not _same(previous, entry['after']), 'Comment entry contains no change.')
            _require(all(_same(previous['content'][name], entry['after']['content'][name])
                         for name in _IDENTITY_KEYS), 'Comment edit changes immutable identity.')
            _require(all(_same(previous[name], entry['after'][name])
                         for name in ('location', 'thread', 'context')),
                     'Comment edit changes location or thread state.')
        else:
            _require(entry['before'] is None, 'New comment before must be null.')
            _require(key not in start.get('notices', {}),
                     'An existing unresolved comment cannot be treated as a new write.')
        derived['comments'][key] = copy.deepcopy(entry['after'])
    return derived
