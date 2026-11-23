"""Compute a worker's next check from its individual outstanding actions."""
from __future__ import annotations


STATES = ('ready', 'dependency', 'human', 'timer', 'closed')
FIELDS = ('key', 'state', 'next_step', 'evidence', 'check_at')
ITEMS_SCHEMA = {'type': 'array', 'items': {
    'type': 'object', 'additionalProperties': False, 'required': list(FIELDS),
    'properties': {'key': {'type': 'string'}, 'state': {'type': 'string', 'enum': list(STATES)},
                   'next_step': {'type': 'string'}, 'evidence': {'type': 'string'},
                   'check_at': {'type': ['integer', 'null']}},
}}


def checked_items(items):
    if not isinstance(items, list):
        raise ValueError('续跑事项必须是列表。')
    result = {}
    for item in items:
        if (not isinstance(item, dict) or set(item) != set(FIELDS)
                or any(not isinstance(item[key], str) or not item[key].strip()
                       for key in ('key', 'next_step', 'evidence'))
                or item['key'] != item['key'].strip() or item['key'] in result
                or item['state'] not in STATES):
            raise ValueError('续跑事项缺少稳定唯一的动作编号、状态、下一步或依据。')
        due = item['check_at']
        valid_time = type(due) is int and 0 < due <= 253402300799
        if (item['state'] == 'timer' and not valid_time) or (item['state'] != 'timer' and due is not None):
            raise ValueError('只有定时复查填写 UTC Unix 秒，其他事项的 check_at 必须为 null。')
        result[item['key']] = item
    return result


def schedule(result, previous, timestamp, interval, limit):
    updates = checked_items(result['continuation_items'])
    items = {**{key: entry['item'] for key, entry in previous.items()}, **updates}
    queue = {}
    for key, item in items.items():
        kind = item['state']
        if kind == 'closed':
            continue
        prior = previous.get(key, {})
        old = prior.get('item', {})
        due, attempts = None, 0
        if kind in ('ready', 'timer'):
            same = all(item[field] == old.get(field) for field in ('state', 'evidence'))
            target = item['check_at']
            if same and kind == 'timer':
                target = min(target, old['check_at'])
                item = dict(item, check_at=target)
            if same and prior['due'] > timestamp:
                due, attempts = prior['due'], prior['attempts']
                if kind == 'timer' and target < old['check_at']:
                    due = min(due, max(timestamp + interval, target))
            elif kind == 'timer' and target > timestamp:
                due = max(timestamp + interval, target)
            else:
                attempts = prior['attempts'] + 1 if same else 0
                delay = min(max(interval, 3600), interval * 2 ** min(12, max(0, attempts - limit + 1)))
                due = timestamp + delay
        queue[key] = {'item': item, 'due': due, 'attempts': attempts}
    if result['outcome'] == 'paused':
        return queue, 'paused', None
    if not queue and result['outcome'] in ('waiting', 'continue') and not updates:
        raise ValueError('仍有下一步却没有登记任何续跑事项。')
    due = min((entry['due'] for entry in queue.values() if entry['due'] is not None), default=None)
    outcome = ('continue' if any(entry['item']['state'] == 'ready' for entry in queue.values())
               else 'waiting' if queue else 'idle')
    return queue, outcome, min(due, timestamp + 86400) if due is not None else None
