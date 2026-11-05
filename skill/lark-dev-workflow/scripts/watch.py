#!/usr/bin/env python3
"""Read a complete, project-scoped change snapshot; never write to Feishu.

The snapshot is an observation, not a business acknowledgement. A caller must
retain its previous checkpoint whenever collection fails.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import comments
from onboard import OnboardingError, atomic_json, link_target


class WatchError(OnboardingError):
    pass


class Cli(comments.Cli):
    """Official CLI reads, always under the explicitly selected user profile."""

    def __init__(self, profile, runner=subprocess.run, *, scan_timeout=300, executable='lark-cli'):
        if scan_timeout <= 0:
            raise WatchError('The scan timeout must be positive.')
        if (not isinstance(executable, str) or not executable.strip()
                or (executable != 'lark-cli' and not Path(executable).is_absolute())):
            raise WatchError('The configured Lark executable must be an absolute path.')
        deadline = time.monotonic() + scan_timeout

        def bounded(argv, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WatchError('Project scan exceeded its time limit; keep the previous checkpoint.')
            kwargs['timeout'] = min(kwargs.get('timeout', 60), 60, remaining)
            # This wrapper also serves the inherited Base/document/comment readers.
            # A resolved npm entrypoint may be named cli.js, so PATH alone cannot pin it.
            return runner([executable, *argv[1:]], **kwargs)

        super().__init__(profile, bounded)

    def fields(self, token, table):
        return self.base.pages('+field-list', 'fields', '--base-token', token,
                               '--table-id', table, limit=200)

    def records(self, token, table, fields, predicate):
        records, seen, offset, first_revision, first_query = [], set(), 0, None, None
        with tempfile.TemporaryDirectory(prefix='lark-watch-') as directory:
            for page_number in range(1000):
                output = Path(directory) / f'{page_number}.ndjson'
                argv = ['lark-cli', 'base', '+record-list', '--base-token', token,
                        '--table-id', table, '--filter-json', json.dumps(predicate, ensure_ascii=False),
                        '--offset', str(offset), '--limit', '2000']
                for field in fields:
                    argv += ['--field-id', field['id']]
                argv += ['--format', 'ndjson', '--output', './' + output.name,
                         '--as', 'user', '--profile', self.profile]
                try:
                    result = self.runner(argv, cwd=directory, capture_output=True, text=True, timeout=60,
                                         env=dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                                                  LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'))
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise WatchError('+record-list: CLI read failed; keep the previous checkpoint.') from exc
                if result.returncode:
                    # Use the shared error protocol without printing raw data or credentials.
                    self.base._envelope(result, '+record-list')
                try:
                    manifest = json.loads(result.stdout)
                except (ValueError, TypeError) as exc:
                    raise WatchError('+record-list: expected an NDJSON manifest.') from exc
                if (not isinstance(manifest, dict) or manifest.get('manifest_version') != 'v1'
                        or manifest.get('format') != 'ndjson'):
                    raise WatchError('+record-list: expected a v1 NDJSON manifest.')
                if (manifest.get('base_token') != token or manifest.get('table_id') != table
                        or manifest.get('identity') not in (None, 'user')):
                    raise WatchError('+record-list: source or operator identity changed.')
                query = manifest.get('query_context')
                revision = manifest.get('rev')
                if (not isinstance(query, dict) or query.get('record_scope') != 'filtered_records'
                        or revision is None):
                    raise WatchError('+record-list: missing filtered scope or revision metadata.')
                if page_number and (revision != first_revision or query != first_query):
                    raise WatchError('+record-list: data or query changed between pages; retry a fresh scan.')
                revisions = self.__dict__.setdefault('_table_revisions', {})
                if table in revisions and revisions[table] != revision:
                    raise WatchError('+record-list: table changed between queries; retry a fresh scan.')
                revisions[table] = revision
                first_revision, first_query = revision, query
                reported_path = manifest.get('record_file')
                if not isinstance(reported_path, str):
                    raise WatchError('+record-list: missing artifact path.')
                artifact = Path(reported_path)
                if not artifact.is_absolute():
                    artifact = Path(directory) / artifact
                if artifact.resolve() != output.resolve() or output.is_symlink():
                    raise WatchError('+record-list: artifact path does not match the requested output.')
                size = manifest.get('record_file_size_bytes')
                if (not output.is_file() or type(size) is not int or size != output.stat().st_size):
                    raise WatchError('+record-list: artifact is missing or its size does not match.')
                try:
                    rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()
                            if line.strip()]
                except (ValueError, UnicodeError) as exc:
                    raise WatchError('+record-list: invalid NDJSON artifact.') from exc
                count, more = manifest.get('records_count'), manifest.get('has_more')
                if (type(count) is not int or count != len(rows) or count > 2000
                        or type(more) is not bool or (more and not rows)):
                    raise WatchError('+record-list: incomplete record count or pagination metadata.')
                columns = manifest.get('columns')
                if not isinstance(columns, dict):
                    raise WatchError('+record-list: missing projected column metadata.')
                projection = {}
                for field in fields:
                    matches = [name for name, column in columns.items()
                               if isinstance(column, dict) and column.get('field_id') == field['id']]
                    if len(matches) != 1:
                        raise WatchError('+record-list: projected field is missing or ambiguous.')
                    if columns[matches[0]].get('field_type') != field['type']:
                        raise WatchError('+record-list: projected field type differs from the binding.')
                    projection[field['id']] = matches[0]
                for row in rows:
                    identifier = row.get('record_id') if isinstance(row, dict) else None
                    if not comments.nonempty(identifier) or identifier in seen:
                        raise WatchError('+record-list: missing or duplicate record ID across pages.')
                    if any(name not in row for name in projection.values()):
                        raise WatchError('+record-list: a projected field is absent from a record.')
                    seen.add(identifier)
                    records.append({'record_id': identifier,
                                    'values': {fid: row[name] for fid, name in projection.items()}})
                if not more:
                    return records
                following = manifest.get('next_offset')
                if type(following) is not int or following <= offset:
                    raise WatchError('+record-list: invalid next_offset; scan is incomplete.')
                offset = following
        raise WatchError('+record-list: exceeded 1000 pages; scan is incomplete.')


def _binding(config):
    if not isinstance(config, dict) or config.get('format_version') != 2:
        raise WatchError('A format-2 project binding is required.')
    scope = config.get('scope', {})
    if scope.get('mode') != 'project_record':
        raise WatchError('Unattended reads currently require scope.mode=project_record.')
    if any(not comments.nonempty(scope.get(key)) for key in ('project_record_id', 'project_table_id')):
        raise WatchError('Project record and table IDs are required.')
    tables = config.get('tables')
    if (not isinstance(tables, dict) or not tables or not isinstance(tables.get('projects'), dict)
            or tables['projects'].get('id') != scope['project_table_id']):
        raise WatchError('The projects table does not match the bound project scope.')
    table_ids = [t.get('id') for t in tables.values() if isinstance(t, dict)]
    if (len(table_ids) != len(tables) or any(not comments.nonempty(t) for t in table_ids)
            or len(set(table_ids)) != len(table_ids)):
        raise WatchError('Mapped table IDs are missing or duplicated.')
    comments.resources(config)
    return scope, tables


def _fields(cli, config, role, table):
    mapped = table.get('fields')
    if not isinstance(mapped, dict) or not mapped:
        raise WatchError(f'{role}: no mapped fields.')
    ids = [f.get('id') for f in mapped.values() if isinstance(f, dict)]
    if (len(ids) != len(mapped) or any(not comments.nonempty(i) for i in ids)
            or len(ids) != len(set(ids))):
        raise WatchError(f'{role}: field IDs are missing or duplicated.')
    current = {f['id']: f for f in cli.fields(config['base']['token'], table['id'])}
    for key, expected in mapped.items():
        actual = current.get(expected['id'])
        if not actual or actual.get('type') != expected.get('type'):
            raise WatchError(f'{role}.{key}: field is missing or its type changed; recheck the project binding.')
    if role != 'projects':
        project = mapped.get('project', {})
        actual = current.get(project.get('id'), {})
        target = config['scope']['project_table_id']
        if (project.get('type') != 'link' or project.get('target_table_id') != target
                or link_target(actual) != target):
            raise WatchError(f'{role}: project link does not target the bound project table.')
    return current


def _one_project(values, field_id, project_id):
    links = values.get(field_id)
    return (isinstance(links, list) and len(links) == 1
            and isinstance(links[0], dict) and links[0].get('id') == project_id)


def _feedback_options(field, number):
    if field.get('type') != 'select' or field.get('multiple', False) or not comments.nonempty(number):
        raise WatchError('Feedback intake requires a single-select project_input and a verified project_number.')
    options = field.get('options')
    if not isinstance(options, list):
        raise WatchError('Feedback project options were not returned completely.')
    names = [o.get('name') if isinstance(o, dict) else o for o in options]
    if any(not comments.nonempty(name) for name in names) or len(names) != len(set(names)):
        raise WatchError('Feedback project options are missing names or duplicated.')
    # A renamed project can leave old options. Both still identify the same
    # real project number; a substring such as PRJ-00010 must never match.
    pattern = re.compile(re.escape(number) + r'(?:\s*·\s*.+)?\Z')
    return sorted(name for name in names if pattern.fullmatch(name))


def _read_records(config, cli, scope, tables):
    snapshot = {}
    token, project_id = config['base']['token'], scope['project_record_id']
    # Verify the immutable project number before allowing the unassigned intake.
    roles = ['projects', *sorted(role for role in tables if role != 'projects')]
    for role in roles:
        table = tables[role]
        current = _fields(cli, config, role, table)
        fields = list(table['fields'].values())
        if role == 'projects':
            rows = [cli.read_record(token, table['id'], project_id, fields)]
            number = table['fields'].get('number')
            if 'feedback' in tables and (not number or rows[0]['values'].get(number['id']) != config.get('project_number')):
                raise WatchError('The live project number does not match this binding; feedback intake was not read.')
        else:
            project_field = table['fields']['project']['id']
            rows = cli.records(token, table['id'], fields, {
                'logic': 'and', 'conditions': [[project_field, 'intersects', [{'id': project_id}]]]})
            if any(not _one_project(row['values'], project_field, project_id) for row in rows):
                raise WatchError(f'{role}: a returned record is outside this project or has ambiguous ownership.')
        if role == 'feedback':
            original, status = table['fields'].get('project_input'), table['fields'].get('status')
            if not original or not status or status.get('type') != 'select':
                raise WatchError('Feedback intake needs mapped project_input and status fields.')
            eligible = _feedback_options(current[original['id']], config.get('project_number'))
            if eligible:
                intake = cli.records(token, table['id'], fields, {'logic': 'and', 'conditions': [
                    [project_field, 'empty'], [original['id'], 'intersects', eligible],
                    [status['id'], 'intersects', ['待受理', '待补充']]]})
                for row in intake:
                    values = row['values']
                    choice, state = values.get(original['id']), values.get(status['id'])
                    if (values.get(project_field) not in (None, []) or not isinstance(choice, list)
                            or len(choice) != 1 or choice[0] not in eligible
                            or state not in (['待受理'], ['待补充'])):
                        raise WatchError('Feedback intake returned a record outside the verified narrow scope.')
                rows += intake
        for row in rows:
            identifier, values = row.get('record_id'), row.get('values')
            if (not comments.nonempty(identifier) or not isinstance(values, dict)
                    or set(values) != {f['id'] for f in fields}
                    or (role == 'projects' and identifier != project_id)):
                raise WatchError(f'{role}: incomplete record readback.')
            key = f"{table['id']}/{identifier}"
            if key in snapshot:
                raise WatchError(f'{role}: duplicate record across queries; retry a fresh scan.')
            snapshot[key] = {'role': role, 'fields': copy.deepcopy(values)}
    return snapshot


def _comment_snapshot(state):
    included, notices = {}, {}
    for key, event in state['events'].items():
        context = event['context_history'][-1]
        location = context['snapshot']['location']
        current = event.get('last_seen_scan') == state['last_complete_scan']
        status = location['status'] if current else 'not_returned'
        value = {'revision': event['latest_revision'], 'context': context['fingerprint']}
        if status == 'in_scope':
            observed = event['revisions'][event['latest_revision']]
            value['content'] = {k: copy.deepcopy(observed.get(k)) for k in (
                'resource_type', 'resource_token', 'comment_id', 'reply_id', 'root_reply',
                'author', 'body', 'text', 'create_time', 'update_time', 'raw_reply_extra')}
            value['location'] = {k: copy.deepcopy(v) for k, v in location.items()
                                 if k not in ('document_revision', 'mapping_evidence')}
            value['thread'] = {k: context['snapshot']['thread'].get(k)
                               for k in ('is_solved', 'solver_user_id')}
            included[key] = value
        elif status == 'unresolved':
            notices[key] = {'status': 'unresolved', **value,
                            'reason': location.get('reason', 'Comment ownership is not verified.')}
        elif any(c['snapshot']['location']['status'] == 'in_scope' for c in event['context_history']):
            # Retain a stable notice for previously owned comments; omit other
            # projects' new content and their subsequent edits.
            notices[key] = {'status': 'moved_out_of_scope' if current else 'not_returned',
                            'reason': 'A previously observed project comment is no longer in this project scope.'}
    return included, notices


def collect(config, profile, *, comments_state_path, locations=None, lark_bin='lark-cli'):
    """Return a stable snapshot, committing only complete comment observations.

    ``locations`` is the existing comments.py format-1 location-evidence object.
    Failures raise OnboardingError (or its subclass); no checkpoint or local
    acknowledgement is advanced. The caller owns its own processing checkpoint.
    """
    try:
        scope, tables = _binding(config)
        cli = Cli(profile, executable=lark_bin)
        known = comments.load_locations(locations)
        with comments.locked(comments_state_path):
            previous = comments.load_state(comments_state_path, config)
            records = _read_records(config, cli, scope, tables)
            updated, report = comments.scan(config, previous, cli, include_resolved=True, known=known)
            if report['status'] != 'complete':
                raise WatchError('Comment scan failed; previous observations were kept: ' + report['error'])
            observed, notices = _comment_snapshot(updated)
            snapshot = {'format_version': 1, 'binding': comments.binding_identity(config),
                        'records': records, 'comments': observed, 'notices': notices}
            atomic_json(Path(comments_state_path), updated)
            return snapshot
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise WatchError(f'Project snapshot failed ({type(exc).__name__}); keep the previous checkpoint.') from exc
