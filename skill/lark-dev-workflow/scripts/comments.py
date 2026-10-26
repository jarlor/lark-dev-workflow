#!/usr/bin/env python3
"""Read project comments and keep a local observation/acknowledgement journal.

No command writes to Feishu. A local acknowledgement is an Agent's recorded
disposition, never independent proof that a remote update or approval happened.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

try:
    import fcntl
except ImportError:  # Windows uses its standard-library byte-range lock.
    fcntl = None
    import msvcrt
else:
    msvcrt = None

from onboard import Cli as BaseCli, OnboardingError, atomic_json, read_json


class CommentError(OnboardingError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def current_profile(config, requested=None, runner=subprocess.run):
    selected = requested or config.get('lark_profile') or config.get('profile')
    if selected is not None:
        if not nonempty(selected):
            raise CommentError('Profile must be a nonempty name.')
        return selected
    result = runner(['lark-cli', 'profile', 'list'], capture_output=True, text=True, timeout=30,
                    env=dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                             LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'))
    try:
        profiles = json.loads(result.stdout)
    except (ValueError, TypeError):
        profiles = None
    if result.returncode or not isinstance(profiles, list):
        raise CommentError('Cannot identify the current CLI profile; pass --profile explicitly.')
    effective = [p for p in profiles if isinstance(p, dict) and p.get('effective') is True]
    if len(effective) != 1 or not nonempty(effective[0].get('name')):
        raise CommentError('Current CLI profile is ambiguous; pass --profile explicitly.')
    return effective[0]['name']


class Cli:
    """An allowlisted reader with explicit user identity and profile on every call."""
    def __init__(self, profile, runner=subprocess.run):
        if not nonempty(profile):
            raise CommentError('An explicit resolved profile is required.')
        self.profile, self.runner = profile, runner
        self.calls = []
        self.base = BaseCli(profile, runner)

    def read(self, domain, command, *args):
        if (domain, command) not in {('drive', '+list-comments'), ('drive', '+list-replies'),
                                     ('docs', '+fetch')}:
            raise CommentError('Only comment/document reads are supported.')
        argv = ['lark-cli', domain, command, *map(str, args), '--as', 'user', '--profile', self.profile]
        self.calls.append({'domain': domain, 'command': command})
        try:
            result = self.runner(argv, capture_output=True, text=True, timeout=60,
                                 env=dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                                          LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommentError(f'{command}: read failed ({type(exc).__name__}).') from exc
        return BaseCli._envelope(result, command)

    def read_record(self, token, table_id, record_id, fields):
        self.calls.append({'domain': 'base', 'command': '+record-get'})
        return self.base.read_record(token, table_id, record_id, fields)


def resources(config):
    if config.get('format_version') != 2 or not nonempty(config.get('base', {}).get('token')):
        raise CommentError('A format-2 project binding is required.')
    base = config['base']
    result = [{'type': 'bitable', 'token': base['token'], 'url': base.get('url')}]
    seen = {('bitable', base['token'])}
    for purpose, doc in config.get('documents', {}).items():
        if not nonempty(doc.get('token')):
            raise CommentError(f'Document binding {purpose} has no token.')
        identity = ('docx', doc['token'])
        if identity not in seen:
            result.append({'type': 'docx', 'token': doc['token'], 'url': doc.get('url')})
            seen.add(identity)
    if len(result) < 2:
        raise CommentError('Project binding has no document resources.')
    return result


def resource_args(resource):
    if nonempty(resource.get('url')):
        return ['--url', resource['url']]
    return ['--token', resource['token'], '--type', resource['type']]


def pages(cli, command, resource, *, comment_id=None, include_resolved=False, max_pages=1000):
    token = None
    tokens, identities, items = set(), set(), []
    for _ in range(max_pages):
        args = resource_args(resource) + ['--page-size', '100']
        if comment_id:
            args += ['--comment-id', comment_id]
        elif command == '+list-comments':
            args += ['--solved-status', 'all' if include_resolved else 'false']
            if resource['type'] == 'docx':
                args.append('--need-relation')
        if token:
            args += ['--page-token', token]
        data = cli.read('drive', command, *args)
        if data.get('file_token') != resource['token'] or data.get('file_type') != resource['type']:
            raise CommentError(f'{command}: response identifies another resource.')
        if comment_id and data.get('comment_id') != comment_id:
            raise CommentError('Reply response identifies another comment thread.')
        page = data.get('items')
        if (not isinstance(page, list) or not isinstance(data.get('has_more'), bool)
                or ('count' in data and data['count'] != len(page))):
            raise CommentError(f'{command}: incomplete pagination metadata.')
        for item in page:
            key = 'reply_id' if comment_id else 'comment_id'
            identifier = item.get(key) if isinstance(item, dict) else None
            if not nonempty(identifier) or identifier in identities:
                raise CommentError(f'{command}: missing or repeated {key}; retry a complete scan.')
            identities.add(identifier)
            items.append(item)
        # The API may return a nonempty page_token on its final page.
        if data['has_more'] is False:
            return items
        following = data.get('page_token')
        if not page or not nonempty(following) or following in tokens:
            raise CommentError(f'{command}: incomplete or cyclic pagination.')
        tokens.add(following)
        token = following
    raise CommentError(f'{command}: pagination bound exceeded; nothing was acknowledged.')


def thread_key(resource, comment_id):
    return f"{resource['type']}:{resource['token']}/comment:{comment_id}"


def event_key(resource, comment_id, reply_id):
    return thread_key(resource, comment_id) + '/reply:' + reply_id


def normalize_reply(resource, comment, reply, root_reply_id, location):
    required = ('reply_id', 'user_id')
    if any(not nonempty(reply.get(key)) for key in required) or not isinstance(reply.get('content'), dict):
        raise CommentError('Reply is missing its stable ID, author, or content.')
    if not isinstance(reply['content'].get('elements'), list):
        raise CommentError('Reply content.elements was not returned completely.')
    body = copy.deepcopy(reply['content'])
    author = reply['user_id']
    updated = reply.get('update_time')
    if updated is None:
        raise CommentError('Reply update_time is required for revision tracking.')
    revision = digest({'author': author, 'body': body, 'update_time': updated})
    text = ''.join(e.get('text_run', {}).get('text', '') for e in body['elements']
                   if isinstance(e, dict) and isinstance(e.get('text_run'), dict))
    return {'event_key': event_key(resource, comment['comment_id'], reply['reply_id']),
            'resource_type': resource['type'], 'resource_token': resource['token'],
            'comment_id': comment['comment_id'], 'reply_id': reply['reply_id'],
            'root_reply': reply['reply_id'] == root_reply_id, 'revision': revision,
            'author': author, 'body': body, 'text': text, 'create_time': reply.get('create_time'),
            'update_time': updated, 'location': copy.deepcopy(location),
            'thread': {'is_solved': comment.get('is_solved'),
                       'solver_user_id': comment.get('solver_user_id'),
                       'update_time': comment.get('update_time')},
            'raw_reply_extra': copy.deepcopy(reply.get('extra'))}


def load_locations(value):
    if value is None:
        return {}
    if value.get('format_version') != 1 or not isinstance(value.get('bindings'), list):
        raise CommentError('Location evidence must contain format_version:1 and bindings:[...].')
    result = {}
    for item in value['bindings']:
        if not isinstance(item, dict):
            raise CommentError('Invalid location evidence entry.')
        evidence = item.get('evidence', {})
        if any(not nonempty(evidence.get(k)) for k in ('reference', 'verified_by', 'verified_at')):
            raise CommentError('Known location mappings require a reference, verifier, and verification time.')
        if any(not nonempty(item.get(k)) for k in ('resource_type', 'resource_token', 'comment_id')):
            raise CommentError('Known location mapping has no resource/comment identity.')
        key = (item['resource_type'], item['resource_token'], item['comment_id'])
        if key in result:
            raise CommentError('Duplicate known location mapping for a comment.')
        result[key] = copy.deepcopy(item)
    return result


def unresolved(reason, **evidence):
    return {'status': 'unresolved', 'reason': reason, **evidence}


def docx_anchor(comment):
    relation = comment.get('relation') or {}
    if relation.get('content_deleted') is True:
        return None, 'The commented content was deleted.', False
    raw = relation.get('relation')
    if not raw:
        return None, 'Comment has no exact Docx relation.blockID.', True
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return None, 'Comment relation is not valid JSON.', False
    if not isinstance(decoded, dict):
        return None, 'Comment relation has an unsupported shape.', False
    anchors = {v['positionInfo']['blockID'] for v in decoded.values()
               if isinstance(v, dict) and isinstance(v.get('positionInfo'), dict)
               and nonempty(v['positionInfo'].get('blockID'))}
    if not anchors:
        return None, 'Comment has no exact Docx block anchor.', True
    if len(anchors) != 1:
        return None, 'Comment has multiple conflicting Docx block anchors.', False
    anchor = next(iter(anchors))
    extra = comment.get('extra') or {}
    if extra.get('content_anchor_id') not in (None, anchor):
        return None, 'Docx relation and content_anchor_id conflict.', False
    return anchor, None, False


def base_anchor(replies):
    anchors = set()
    for reply in replies:
        extra = (reply.get('extra') or {}).get('notify_extra')
        if extra is None:
            continue
        if not isinstance(extra, dict) or any(not nonempty(extra.get(k)) for k in ('table', 'record')):
            return None, 'Base notify_extra has no complete table/record identity.'
        anchors.add((extra['table'], extra['record']))
    if len(anchors) != 1:
        return None, 'Base replies have conflicting anchors.' if anchors else 'Base comment has no exact record ID; quote/row order was not used.'
    return next(iter(anchors)), None


def xml_index(content):
    if not isinstance(content, str):
        raise CommentError('Document read has no XML content.')
    try:
        root = ET.fromstring('<document-read>' + content + '</document-read>')
    except ET.ParseError as exc:
        raise CommentError('Document read is not valid XML.') from exc
    # Full reads may wrap their blocks in a single document element.
    if len(root) == 1 and root[0].tag in ('document', 'doc', 'body'):
        root = root[0]
    ids = {}
    for element in root.iter():
        identifier = element.get('id') or element.get('block-id')
        if identifier:
            if identifier in ids:
                raise CommentError('Document contains duplicate block IDs.')
            ids[identifier] = element
    return root, ids


def project_sections(root, ids):
    """Map real blocks to their enclosing top-level PRJ heading, never quote text."""
    owner_by_id, section_by_anchor = {}, {}
    stack = []
    for top in root:
        match = re.fullmatch(r'h([1-6])', top.tag)
        if match:
            level = int(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            title = ''.join(top.itertext()).strip()
            numbers = set(re.findall(r'(?<![A-Za-z0-9])PRJ-[A-Za-z0-9]+(?![A-Za-z0-9])', title))
            anchor = top.get('id') or top.get('block-id')
            if len(numbers) == 1 and anchor:
                stack.append((level, anchor, next(iter(numbers))))
                section_by_anchor[anchor] = {'number': next(iter(numbers)), 'block_ids': set()}
        owner = stack[-1] if stack else None
        for item in top.iter():
            identifier = item.get('id') or item.get('block-id')
            if identifier and owner:
                owner_by_id[identifier] = {'anchor': owner[1], 'number': owner[2]}
                section_by_anchor[owner[1]]['block_ids'].add(identifier)
    return owner_by_id, section_by_anchor


class Locator:
    def __init__(self, config, cli, known=None):
        self.config, self.cli, self.known = config, cli, known or {}
        self.documents, self.records = {}, {}

    def locate(self, resource, comment, replies):
        scope = self.config.get('scope', {})
        project_id = scope.get('project_record_id')
        if scope.get('mode') != 'project_record' or not nonempty(project_id):
            return unresolved('Binding has no verified project record scope; explicit project binding is needed.')
        mapping = self.known.get((resource['type'], resource['token'], comment['comment_id']))
        if resource['type'] == 'docx':
            return self.docx(resource, comment, mapping)
        return self.base(resource, replies, mapping)

    def docx(self, resource, comment, mapping):
        anchor, problem, may_supply_missing = docx_anchor(comment)
        if mapping:
            mapped = mapping.get('block_id')
            extra_anchor = (comment.get('extra') or {}).get('content_anchor_id')
            if (not nonempty(mapped) or (anchor and mapped != anchor)
                    or (extra_anchor is not None and mapped != extra_anchor)):
                return unresolved('Known Docx mapping conflicts with the exact comment anchor.')
            if anchor is None and not may_supply_missing:
                return unresolved(problem)
            if anchor is None:
                anchor = mapped
        if anchor is None:
            return unresolved(problem)
        token = resource['token']
        if token not in self.documents:
            # Full structure is necessary to distinguish sibling project sections.
            data = self.cli.read('docs', '+fetch', '--doc', token, '--detail', 'with-ids')
            doc = data.get('document', {})
            if doc.get('document_id') != token:
                raise CommentError('Document location read returned another token.')
            root, ids = xml_index(doc.get('content'))
            owners, sections = project_sections(root, ids)
            self.documents[token] = (ids, owners, sections, doc.get('revision_id'))
        ids, owners, sections, revision = self.documents[token]
        evidence = {'block_id': anchor, 'document_revision': revision,
                    'method': 'known_mapping_and_live_document' if mapping else 'relation_and_live_document'}
        if mapping:
            evidence['mapping_evidence'] = mapping['evidence']
        if anchor not in ids:
            return unresolved('Exact comment block was not present in the current document.', **evidence)
        bound = [d for d in self.config['documents'].values() if d['token'] == token]
        eligible = {d.get('anchor') for d in bound if d.get('project_record_id') == self.config['scope']['project_record_id']}
        number = self.config.get('project_number')
        verified_sections = {a for a in eligible if a in sections and sections[a]['number'] == number}
        owner = owners.get(anchor)
        if owner and owner['anchor'] in verified_sections:
            return {'status': 'in_scope', 'project_record_id': self.config['scope']['project_record_id'],
                    'project_section': owner['anchor'], **evidence}
        if owner and owner['number'] != number:
            return {'status': 'out_of_scope', 'reason': 'Exact block belongs to a different project heading.',
                    'other_project_number': owner['number'], **evidence}
        return unresolved('Exact block exists, but its enclosing section is not verified as this project.', **evidence)

    def base(self, resource, replies, mapping):
        anchor, problem = base_anchor(replies)
        if mapping:
            mapped = (mapping.get('table_id'), mapping.get('record_id'))
            if not all(nonempty(v) for v in mapped) or (anchor and mapped != anchor):
                return unresolved('Known Base mapping conflicts with the exact record anchor.')
            if problem and ('conflicting' in problem or 'complete' in problem):
                return unresolved(problem)
            if anchor is None:
                anchor = mapped
        if anchor is None:
            return unresolved(problem)
        table_id, record_id = anchor
        matches = [(r, t) for r, t in self.config['tables'].items() if t['id'] == table_id]
        if len(matches) != 1:
            return unresolved('Comment table is not uniquely mapped in this project binding.', table_id=table_id, record_id=record_id)
        role, table = matches[0]
        scope = self.config['scope']
        field = table['fields'].get('title' if role == 'projects' else 'project')
        if not field or (role != 'projects' and (field.get('type') != 'link' or
                                                field.get('target_table_id') != scope.get('project_table_id'))):
            return unresolved('Mapped record has no verifiable project relation.', table_id=table_id, record_id=record_id)
        key = (table_id, record_id, field['id'])
        if key not in self.records:
            self.records[key] = self.cli.read_record(resource['token'], table_id, record_id, [field])
        read = self.records[key]
        if read.get('record_id') != record_id:
            raise CommentError('Base location read returned another record.')
        linked = [{'id': record_id}] if role == 'projects' else read['values'].get(field['id'])
        if (not isinstance(linked, list) or len(linked) != 1 or not isinstance(linked[0], dict)
                or not nonempty(linked[0].get('id'))):
            return unresolved('Record project relation is empty or ambiguous.', table_id=table_id, record_id=record_id)
        actual_id = linked[0]['id']
        evidence = {'table_id': table_id, 'record_id': record_id, 'project_record_id': actual_id,
                    'project_field_id': field['id'], 'method': 'known_mapping_and_live_record' if mapping else 'notify_extra_and_live_record'}
        if mapping:
            evidence['mapping_evidence'] = mapping['evidence']
        return {'status': 'in_scope' if actual_id == scope['project_record_id'] else 'out_of_scope', **evidence}


def binding_identity(config):
    return {'base_token': config['base']['token'], 'scope': copy.deepcopy(config.get('scope')),
            'documents': sorted([{'token': d['token'], 'anchor': d.get('anchor')}
                                 for d in config.get('documents', {}).values()], key=lambda d: (d['token'], d['anchor'] or ''))}


def empty_state(config):
    resources(config)
    return {'format_version': 1, 'binding': binding_identity(config), 'events': {},
            'acknowledgements': {}, 'scans': [], 'last_complete_scan': None,
            'last_full_reconciliation': None}


def context_fingerprint(snapshot):
    # A new document revision elsewhere is readback evidence, not a change in
    # this comment's ownership. Preserve it without reopening unrelated work.
    location = {k: v for k, v in snapshot['location'].items()
                if k not in ('document_revision', 'mapping_evidence')}
    thread = {k: snapshot['thread'].get(k) for k in ('is_solved', 'solver_user_id')}
    return digest({'thread': thread, 'location': location})


def observe_context(event, observed, scan_id, observed_at):
    snapshot = {'thread': copy.deepcopy(observed['thread']), 'location': copy.deepcopy(observed['location'])}
    fingerprint = context_fingerprint(snapshot)
    history = event.setdefault('context_history', [])
    if not history or history[-1]['fingerprint'] != fingerprint:
        history.append({'observation_id': f'{len(history) + 1}:{fingerprint}',
                        'fingerprint': fingerprint, 'first_observed_at': observed_at,
                        'last_observed_at': observed_at, 'scan_id': scan_id,
                        'snapshot': snapshot, 'readbacks': []})
    context = history[-1]
    context.update(last_observed_at=observed_at, scan_id=scan_id, current_readback=snapshot)
    evidence_hash = digest(snapshot)
    if not any(item['fingerprint'] == evidence_hash for item in context['readbacks']):
        context['readbacks'].append({'fingerprint': evidence_hash, 'snapshot': snapshot,
                                    'observed_at': observed_at, 'scan_id': scan_id})
    event['latest_observation_id'] = context['observation_id']


def upgrade_state(state):
    """Safely enrich early format-1 journals without discarding observations."""
    state = copy.deepcopy(state)
    for event in state['events'].values():
        if not event.get('context_history'):
            observed = event['revisions'][event['latest_revision']]
            observe_context(event, observed, event.get('last_seen_scan'),
                            observed.get('last_observed_at') or observed.get('first_observed_at'))
        else:
            # Early format-1 prototypes included readback timestamps in the
            # context hash. Recompute comparison metadata without changing the
            # opaque observation IDs referenced by existing acknowledgements.
            for context in event['context_history']:
                context['fingerprint'] = context_fingerprint(context['snapshot'])
    return state


def load_state(path, config):
    state = read_json(path) if Path(path).exists() else empty_state(config)
    if state.get('format_version') != 1 or state.get('binding') != binding_identity(config):
        raise CommentError('Existing comment state belongs to another binding; preserve it and choose a separate path.')
    if any(not isinstance(state.get(k), dict) for k in ('events', 'acknowledgements')) or not isinstance(state.get('scans'), list):
        raise CommentError('Comment state is malformed; it was not modified.')
    return upgrade_state(state)


def scan(config, state, cli, *, include_resolved=False, known=None):
    state = upgrade_state(state)
    scan_id = str(uuid.uuid4())
    started = now()
    locator = Locator(config, cli, known)
    observations, coverage = [], []
    try:
        for resource in resources(config):
            comments = pages(cli, '+list-comments', resource, include_resolved=include_resolved)
            reply_count = 0
            for comment in comments:
                # Always read replies from page one: nested has_more may be absent.
                replies = pages(cli, '+list-replies', resource, comment_id=comment['comment_id'])
                if not replies:
                    raise CommentError('Comment thread has no readable root reply; retry the scan.')
                location = locator.locate(resource, comment, replies)
                observations.extend(normalize_reply(resource, comment, reply, replies[0]['reply_id'], location)
                                    for reply in replies)
                reply_count += len(replies)
            coverage.append({'resource_type': resource['type'], 'resource_token': resource['token'],
                             'comments': len(comments), 'replies': reply_count, 'pagination_complete': True})
    except (OnboardingError, OSError, ValueError, TypeError, KeyError) as exc:
        failed = copy.deepcopy(state)
        attempt = {'id': scan_id, 'started_at': started, 'finished_at': now(), 'status': 'failed',
                   'include_resolved': include_resolved, 'error': str(exc), 'resources': coverage,
                   'profile': cli.profile, 'remote_writes': 0}
        failed['scans'].append(attempt)
        return failed, {'status': 'failed', 'error': str(exc), 'observations_committed': False,
                        'acknowledgements_changed': False, 'scan': attempt}
    updated = copy.deepcopy(state)
    fresh, revisions, context_changes = 0, 0, []
    observed_at = now()
    for observation in observations:
        key, revision = observation['event_key'], observation['revision']
        if key not in updated['events']:
            fresh += 1
            updated['events'][key] = {'event_key': key, 'revisions': {}, 'latest_revision': revision}
        event = updated['events'][key]
        previous_context = copy.deepcopy(event.get('context_history', [])[-1]) if event.get('context_history') else None
        if revision not in event['revisions']:
            revisions += 1
            event['revisions'][revision] = {**observation, 'first_observed_at': observed_at}
        # Content revisions stay distinct from thread-state/ownership observations.
        # A return to an earlier thread state is a new observation generation.
        stored = event['revisions'][revision]
        stored.update(last_observed_at=observed_at, scan_id=scan_id)
        observe_context(event, observation, scan_id, observed_at)
        if previous_context and previous_context['observation_id'] != event['latest_observation_id']:
            def summary(snapshot):
                location = snapshot['location']
                return {'location_status': location['status'],
                        'project_record_id': location.get('project_record_id'),
                        'other_project_number': location.get('other_project_number'),
                        'block_id': location.get('block_id'), 'record_id': location.get('record_id'),
                        'is_solved': snapshot['thread'].get('is_solved'),
                        'solver_user_id': snapshot['thread'].get('solver_user_id'),
                        'thread_update_time': snapshot['thread'].get('update_time')}
            acknowledged = [ack for entries in updated['acknowledgements'].get(key, {}).values() for ack in entries]
            context_changes.append({'event_key': key, 'observation_id': event['latest_observation_id'],
                                    'previous': summary(previous_context['snapshot']),
                                    'current': summary(event['context_history'][-1]['snapshot']),
                                    'previously_acknowledged': bool(acknowledged),
                                    'acknowledged_targets': sorted({a['target'] for a in acknowledged}),
                                    'action': 'Reconcile existing linked work; this scan does not change its ownership or disposition.'})
        event['latest_revision'] = revision
        event['last_seen_scan'] = scan_id
    attempt = {'id': scan_id, 'started_at': started, 'finished_at': observed_at, 'status': 'complete',
               'include_resolved': include_resolved, 'resources': coverage, 'profile': cli.profile,
               'remote_writes': 0}
    updated['scans'].append(attempt)
    updated['last_complete_scan'] = scan_id
    if include_resolved:
        updated['last_full_reconciliation'] = scan_id
    warnings = [] if updated['last_full_reconciliation'] else [
        'No full historical reconciliation yet; explicitly use --include-resolved to include resolved threads.']
    report = {'status': 'complete', 'scan': attempt, 'new_events': fresh, 'new_revisions': revisions,
              'context_changes': context_changes,
              'observations_committed': True, 'acknowledgements_changed': False, 'warnings': warnings,
              'pending_count': len(pending(updated)), 'remote_writes': 0}
    return updated, report


def pending(state):
    state = upgrade_state(state)
    result = []
    for key, event in state['events'].items():
        revision = event['latest_revision']
        context = event['context_history'][-1]
        observed = {**event['revisions'][revision], **context.get('current_readback', context['snapshot']),
                    'observation_id': event['latest_observation_id']}
        if observed['location']['status'] == 'out_of_scope':
            continue
        acknowledgements = state['acknowledgements'].get(key, {}).get(revision, [])
        if not any(ack.get('observation_id') == event['latest_observation_id'] for ack in acknowledgements):
            result.append(copy.deepcopy(observed))
    return sorted(result, key=lambda item: (str(item.get('create_time')), item['event_key']))


def acknowledgement(state, payload):
    state = upgrade_state(state)
    if not state['scans'] or state['scans'][-1]['status'] != 'complete':
        raise CommentError('A complete successful scan is required before acknowledgement.')
    if any(not nonempty(payload.get(k)) for k in ('event_key', 'revision', 'observation_id', 'disposition', 'target')):
        raise CommentError('Acknowledgement requires event_key, revision, observation_id, disposition, and target.')
    key, revision = payload['event_key'], payload['revision']
    event = state['events'].get(key)
    if not event or event['latest_revision'] != revision:
        raise CommentError('Acknowledgement must name the latest observed revision; scan and review again.')
    if event.get('last_seen_scan') != state['last_complete_scan']:
        raise CommentError('Event was not seen in the latest complete scan; absence is not deletion or processing proof.')
    if payload['observation_id'] != event['latest_observation_id']:
        raise CommentError('Thread status or ownership changed; review the latest observation before acknowledgement.')
    context = event['context_history'][-1]
    observed = {**event['revisions'][revision], **context.get('current_readback', context['snapshot'])}
    if observed['location']['status'] != 'in_scope':
        raise CommentError('Comment project ownership is unresolved or outside this project; do not acknowledge it here.')
    evidence = payload.get('evidence')
    if not isinstance(evidence, list) or not evidence or any(
            not isinstance(item, dict) or any(not nonempty(item.get(k)) for k in ('reference', 'readback', 'observed_at'))
            for item in evidence):
        raise CommentError('Provide evidence entries with reference, readback summary, and observed_at.')
    receipt = payload.get('receipt')
    if receipt is not None:
        if not isinstance(receipt, dict) or not nonempty(receipt.get('reply_id')):
            raise CommentError('Receipt requires a reply_id in this same comment thread.')
        expected = key.rsplit('/reply:', 1)[0] + '/reply:' + receipt['reply_id']
        found = state['events'].get(expected)
        if not found or found.get('last_seen_scan') != state['last_complete_scan']:
            raise CommentError('Receipt reply was not read back in this thread by the latest complete scan.')
    resolved = payload.get('resolved')
    if resolved is not None:
        if not isinstance(resolved, bool) or observed['thread'].get('is_solved') is not resolved:
            raise CommentError('Requested resolved state was not observed on this thread.')
        latest_scan = state['scans'][-1]
        if resolved and (not latest_scan['include_resolved'] or event['last_seen_scan'] != latest_scan['id']):
            raise CommentError('Resolved proof requires a fresh explicit --include-resolved scan.')
    recorded = {k: copy.deepcopy(payload[k]) for k in
                ('event_key', 'revision', 'observation_id', 'disposition', 'target', 'evidence', 'handler', 'receipt', 'resolved') if k in payload}
    recorded.update(kind='local_agent_acknowledgement', remote_write_performed=False,
                    remote_proof_verified=False, evidence_basis='agent_reported_readback',
                    source_scan_id=state['last_complete_scan'])
    updated = copy.deepcopy(state)
    history = updated['acknowledgements'].setdefault(key, {}).setdefault(revision, [])
    same = lambda a: {k: v for k, v in a.items() if k not in ('acknowledged_at', 'source_scan_id')}
    if any(same(item) == same(recorded) for item in history):
        return state, {'status': 'unchanged', 'event_key': key, 'revision': revision, 'remote_writes': 0}
    history.append({**recorded, 'acknowledged_at': now()})
    return updated, {'status': 'acknowledged_locally', 'event_key': key, 'revision': revision,
                     'remote_writes': 0, 'remote_proof_verified': False}


@contextmanager
def locked(path):
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+b') as stream:
        try:
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                if lock.stat().st_size == 0:
                    stream.write(b'\0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise CommentError('Another local operation is using this comment state; retry after it finishes.') from exc
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    for name in ('scan', 'pending', 'ack'):
        sub = commands.add_parser(name)
        sub.add_argument('--config', required=True, type=Path)
        sub.add_argument('--state', type=Path, help='Default: comments-state.json beside the project configuration.')
        sub.add_argument('--output', type=Path, help='Optional local JSON result file.')
        if name == 'scan':
            sub.add_argument('--profile', help='Otherwise resolve the configured/current CLI profile read-only.')
            sub.add_argument('--include-resolved', action='store_true', help='Explicitly include solved threads for full reconciliation.')
            sub.add_argument('--locations', type=Path, help='Explicit known location evidence; every target is verified again read-only.')
        elif name == 'ack':
            sub.add_argument('--ack-file', required=True, type=Path,
                             help='JSON with event_key, revision, observation_id, disposition, target, evidence; optional receipt/resolved.')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    state_path = args.state or args.config.with_name('comments-state.json')
    try:
        inputs = [args.config, getattr(args, 'locations', None), getattr(args, 'ack_file', None)]
        outputs = [state_path, args.output]
        paths = [p.resolve() for p in outputs if p]
        if len(paths) != len(set(paths)) or any(p and p.resolve() in paths for p in inputs):
            raise CommentError('Inputs, state, and output paths must be distinct.')
        config = read_json(args.config)
        resources(config)
        with locked(state_path):
            state = load_state(state_path, config)
            if args.command == 'scan':
                profile = current_profile(config, args.profile)
                known = load_locations(read_json(args.locations)) if args.locations else {}
                updated, result = scan(config, state, Cli(profile), include_resolved=args.include_resolved, known=known)
                atomic_json(state_path, updated)
            elif args.command == 'ack':
                updated, result = acknowledgement(state, read_json(args.ack_file))
                if updated != state:
                    atomic_json(state_path, updated)
            else:
                result = {'status': 'local_observations', 'pending': pending(state),
                          'last_full_reconciliation': state['last_full_reconciliation'],
                          'last_scan': state['scans'][-1] if state['scans'] else None, 'remote_writes': 0}
        result['state'] = str(state_path)
        if args.output:
            atomic_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('status') == 'failed' else 0
    except (OnboardingError, OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc), 'remote_writes': 0}, ensure_ascii=False, indent=2))
        return 2


if __name__ == '__main__':
    sys.exit(main())
