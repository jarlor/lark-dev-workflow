"""Optional, validated context shortcuts. Missing evidence keeps the old execution path."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def snapshot_digest(snapshot):
    stable = {k: v for k, v in snapshot.items() if k != 'observed_at'}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def checkpoint_snapshot(directory, state):
    """Use only a snapshot matching the committed checkpoint, including pre-upgrade runs."""
    candidates = [Path(directory) / 'checkpoint-snapshot.json']
    run_id = (state.get('last_result') or {}).get('run_id')
    if isinstance(run_id, str) and run_id and Path(run_id).name == run_id:
        candidates.append(Path(directory) / 'runs' / run_id / 'snapshot.json')
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(value, dict) and snapshot_digest(value) == state.get('checkpoint'):
                return value
        except (OSError, ValueError, TypeError):
            pass
    return None


def session_runs(state, maximum):
    count = state.get('session_runs')
    if type(count) is int and count >= 0:
        return count
    # Old sessions first produce a handoff in their existing context, never reset blindly.
    return maximum if state.get('session_id') else 0


def validate_handoff(value, result):
    required = {'format_version', 'run_id', 'snapshot_id', 'can_start_fresh',
                'summary', 'open_items', 'constraints', 'active_operations'}
    if (not isinstance(value, dict) or set(value) != required
            or type(value['format_version']) is not int or value['format_version'] != 1):
        raise ValueError('handoff structure is invalid')
    if any(value[k] != result.get(k) for k in ('run_id', 'snapshot_id')):
        raise ValueError('handoff belongs to another run')
    if type(value['can_start_fresh']) is not bool:
        raise ValueError('can_start_fresh must be a boolean')
    if not isinstance(value['summary'], str) or not value['summary'].strip():
        raise ValueError('handoff summary is empty')
    for name, fields in [('open_items', {'source', 'next_step'}),
                         ('constraints', {'source', 'rule'})]:
        if not isinstance(value[name], list):
            raise ValueError(f'{name} must be a list')
        for item in value[name]:
            if (not isinstance(item, dict) or set(item) != fields
                    or any(not isinstance(v, str) or not v.strip() for v in item.values())):
                raise ValueError(f'{name} needs concrete sources and actions')
    if not isinstance(value['active_operations'], list):
        raise ValueError('active_operations must list unfinished external operations')
    return value


def prepare_session(state, directory, maximum):
    """Rotate only at a successful boundary with an explicit worker handoff.

    The caller persists this plan together with inflight before starting a child.
    A failed fresh child then remains the recovery session; the previous ID is retained.
    """
    if (not state.get('session_id') or state.get('recovery_note') or state.get('inflight')
            or session_runs(state, maximum) < maximum):
        return {'mode': 'resume' if state.get('session_id') else 'fresh'}
    result = state.get('last_result') or {}
    run_id = result.get('run_id')
    if (result.get('outcome') not in ('continue', 'waiting', 'idle')
            or not isinstance(run_id, str) or Path(run_id).name != run_id):
        return {'mode': 'resume', 'note': 'No successful run suitable for a handoff.'}
    path = Path(directory) / 'runs' / run_id / 'handoff.json'
    try:
        value = validate_handoff(json.loads(path.read_text(encoding='utf-8')), result)
    except (OSError, ValueError, TypeError, KeyError):
        return {'mode': 'resume', 'note': 'No valid handoff; keep the existing session.'}
    if not value['can_start_fresh'] or value['active_operations']:
        return {'mode': 'resume', 'note': 'The worker still needs its existing session.'}
    prior = state['session_id']
    handoff = {'previous_session_id': prior, 'source': str(path), 'content': copy.deepcopy(value)}
    state.setdefault('session_history', []).append({
        'session_id': prior, 'last_run_id': run_id, 'handoff_file': str(path)})
    state['session_id'] = None
    state['session_runs'] = 0
    state['session_handoff'] = handoff
    return {'mode': 'fresh_from_handoff', 'handoff': handoff}
