#!/usr/bin/env python3
"""Read a copied Base and write a project-local binding; never mutates Feishu."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

READ_COMMANDS = frozenset({
    '+base-get', '+table-list', '+field-list', '+base-block-list',
    '+view-list', '+view-get-visible-fields', '+view-get-filter',
    '+form-list', '+form-questions-list', '+form-share-get', '+record-get',
})


class OnboardingError(Exception):
    pass


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def parse_base_url(value):
    parsed = urlparse(value)
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or not (host.endswith('.feishu.cn') or host.endswith('.larksuite.com')):
        raise OnboardingError('Use the canonical HTTPS Feishu/Lark Base URL, not a share or app link.')
    match = re.fullmatch(r'/base/([A-Za-z0-9]+)/*', parsed.path)
    if not match:
        raise OnboardingError('Resolve the link to a canonical /base/<token> URL first.')
    return match.group(1), f'https://{host}/base/{match.group(1)}'


class Cli:
    def __init__(self, profile=None, runner=subprocess.run):
        self.profile = profile
        self.runner = runner
        self.calls = []

    def _invoke(self, domain, command, arguments, cwd=None):
        if not ((domain == 'base' and command in READ_COMMANDS) or (domain == 'docs' and command == '+fetch')):
            raise OnboardingError(f'Remote mutation is not supported: {domain} {command}')
        argv = ['lark-cli', domain, command, *map(str, arguments), '--as', 'user']
        if domain == 'base' and '--format' not in arguments:
            argv.extend(['--format', 'json'])
        if self.profile:
            argv.extend(['--profile', self.profile])
        self.calls.append(command if domain == 'base' else domain + '.' + command)
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
        try:
            return self.runner(argv, capture_output=True, text=True, timeout=60, env=env, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OnboardingError(f'{command}: read failed ({type(exc).__name__}); no write attempted.') from exc

    @staticmethod
    def _envelope(result, command):
        envelope = None
        for raw in (result.stdout, result.stderr):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and 'ok' in parsed:
                    envelope = parsed
                    if parsed.get('ok') is False:
                        break
            except (json.JSONDecodeError, TypeError):
                pass
        if result.returncode != 0 or not envelope or envelope.get('ok') is not True:
            error = (envelope or {}).get('error', {})
            detail = error.get('message') or 'CLI returned no confirmed success envelope'
            raise OnboardingError(f'{command}: {detail}')
        if envelope.get('identity') not in (None, 'user'):
            raise OnboardingError(f'{command}: unexpected operator identity')
        data = envelope.get('data')
        if not isinstance(data, dict):
            raise OnboardingError(f'{command}: missing object data')
        return data

    def read(self, command, *arguments, cwd=None):
        if command == '+record-get':
            raise OnboardingError('Use read_record for the record-get NDJSON manifest contract.')
        return self._envelope(self._invoke('base', command, arguments, cwd), command)

    def read_record(self, base_token, table_id, record_id, fields):
        """CLI 1.0.94 NDJSON reads return a manifest, not an ok envelope."""
        with tempfile.TemporaryDirectory(prefix='lark-onboard-record-') as temporary:
            output = Path(temporary) / 'project.ndjson'
            arguments = ['--base-token', base_token, '--table-id', table_id, '--record-id', record_id]
            for field in fields:
                arguments.extend(['--field-id', field['id']])
            arguments.extend(['--format', 'ndjson', '--output', './project.ndjson'])
            result = self._invoke('base', '+record-get', arguments, temporary)
            try:
                manifest = json.loads(result.stdout)
            except (json.JSONDecodeError, TypeError):
                manifest = None
            if result.returncode != 0:
                try:
                    error = json.loads(result.stderr).get('error', {}).get('message')
                except (json.JSONDecodeError, TypeError, AttributeError):
                    error = None
                raise OnboardingError('+record-get: ' + (error or 'NDJSON read failed'))
            if not isinstance(manifest, dict) or manifest.get('manifest_version') != 'v1' or manifest.get('format') != 'ndjson':
                raise OnboardingError('+record-get: expected a v1 NDJSON manifest, not an ordinary API envelope')
            if manifest.get('base_token') != base_token or manifest.get('table_id') != table_id:
                raise OnboardingError('+record-get: manifest identifies a different Base or table')
            query = manifest.get('query_context', {})
            if (query.get('record_scope') != 'selected_record_ids' or query.get('requested_record_count') != 1
                    or manifest.get('records_count') != 1 or manifest.get('page_count') != 1 or manifest.get('has_more') is not False):
                raise OnboardingError('+record-get: selected record read is incomplete or not unique')
            if not isinstance(manifest.get('record_file'), str) or Path(manifest['record_file']).resolve() != output.resolve():
                raise OnboardingError('+record-get: manifest record_file is not the requested local artifact')
            if not output.is_file() or manifest.get('record_file_size_bytes') != output.stat().st_size:
                raise OnboardingError('+record-get: record artifact is missing or has an unexpected size')
            records = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines() if line.strip()]
            if len(records) != 1 or not isinstance(records[0], dict) or records[0].get('record_id') != record_id:
                raise OnboardingError('+record-get: actual artifact does not contain the selected project record')
            columns = manifest.get('columns', {})
            values = {}
            for field in fields:
                matches = [name for name, column in columns.items()
                           if isinstance(column, dict) and column.get('field_id') == field['id']]
                if len(matches) != 1 or matches[0] not in records[0]:
                    raise OnboardingError('+record-get: projected project field was not returned unambiguously')
                values[field['id']] = records[0][matches[0]]
            return {'record_id': record_id, 'values': values,
                    'manifest': {key: copy.deepcopy(manifest[key]) for key in
                                 ('manifest_version', 'format', 'base_token', 'table_id', 'query_context',
                                  'records_count', 'page_count', 'has_more', 'columns')}}

    def read_document(self, reference, project_number=None):
        arguments = ['--doc', reference['token'], '--detail', 'with-ids']
        if reference.get('anchor'):
            arguments.extend(['--scope', 'section', '--start-block-id', reference['anchor'], '--max-depth', '0'])
        else:
            arguments.extend(['--scope', 'outline', '--max-depth', '1'])
        data = self._envelope(self._invoke('docs', '+fetch', arguments), 'docs +fetch')
        document = data.get('document', {})
        if document.get('document_id') != reference['token']:
            raise OnboardingError('Project document read returned a different Docx token.')
        anchor_title = None
        project_verified = False
        if reference.get('anchor'):
            try:
                root = ET.fromstring(document.get('content', ''))
            except ET.ParseError as exc:
                raise OnboardingError('Project document section response is not valid XML.') from exc
            matching = [element for element in root.iter()
                        if element.get('id') == reference['anchor'] or element.get('block-id') == reference['anchor']]
            if not matching:
                raise OnboardingError('Project document anchor was not found in the returned section.')
            anchor_title = ''.join(matching[0].itertext()).strip()
            project_verified = document_ownership(anchor_title, project_number)
        return {**reference, 'verified': True, 'revision_id': document.get('revision_id'),
                'anchor_title': anchor_title, 'project_number': project_number, 'project_verified': project_verified}

    def pages(self, command, key, *arguments, limit=100):
        items, seen, offset, expected_total = [], set(), 0, None
        for _ in range(1000):
            data = self.read(command, *arguments, '--limit', limit, '--offset', offset)
            page = data.get(key)
            total = data.get('total')
            if page is None and total == 0:
                page = []
            if not isinstance(page, list) or not isinstance(total, int) or total < 0:
                raise OnboardingError(f'{command}: incomplete pagination metadata')
            if expected_total is not None and total != expected_total:
                raise OnboardingError(f'{command}: inventory changed during discovery; retry a fresh read')
            expected_total = total
            for item in page:
                identifier = item.get('id') if isinstance(item, dict) else None
                if not identifier or identifier in seen:
                    raise OnboardingError(f'{command}: missing or duplicate ID during pagination')
                seen.add(identifier)
                items.append(item)
            if len(items) == total and not data.get('has_more', False):
                return items
            if not page or len(items) > total:
                raise OnboardingError(f'{command}: incomplete or inconsistent pagination')
            offset += len(page)
        raise OnboardingError(f'{command}: pagination bound exceeded')


def collect(cli, token, contract, project_record_id=None):
    base = cli.read('+base-get', '--base-token', token).get('base')
    if not isinstance(base, dict) or base.get('base_token') != token:
        raise OnboardingError('Base identity was not confirmed.')
    blocks_data = cli.read('+base-block-list', '--base-token', token)
    blocks = blocks_data.get('blocks')
    if not isinstance(blocks, list) or blocks_data.get('total') != len(blocks):
        raise OnboardingError('Base block inventory is incomplete.')
    tables = cli.pages('+table-list', 'tables', '--base-token', token)
    for table in tables:
        args = ('--base-token', token, '--table-id', table['id'])
        table['fields'] = cli.pages('+field-list', 'fields', *args, limit=200)
        table['views'] = cli.pages('+view-list', 'views', *args, limit=200)
        for view in table['views']:
            if view.get('type') == 'form':
                continue  # Form views expose questions through the separate form API.
            view['visible_fields_config'] = cli.read('+view-get-visible-fields', *args, '--view-id', view['id'])
            view['filter_config'] = cli.read('+view-get-filter', *args, '--view-id', view['id'])
        forms_data = cli.read('+form-list', *args)
        forms = forms_data.get('forms') or []
        if not isinstance(forms, list) or forms_data.get('total') != len(forms):
            raise OnboardingError('Form inventory is incomplete.')
        table['forms'] = forms
        for form in forms:
            form_id = form.get('id') or form.get('form_id')
            if not form_id:
                raise OnboardingError('Form has no ID.')
            form['questions_config'] = cli.read('+form-questions-list', *args, '--form-id', form_id)
            form['share_config'] = cli.read('+form-share-get', *args, '--form-id', form_id)
    snapshot = {
        'snapshot_format': 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'base': base, 'blocks': blocks, 'tables': tables,
    }
    if project_record_id:
        project_name = contract['tables']['projects']['name']
        candidates = [t for t in tables if t.get('name') == project_name]
        if len(candidates) != 1:
            raise OnboardingError('Cannot uniquely locate the project table for scope verification.')
        table = candidates[0]
        project_fields = contract['tables']['projects']['fields']
        semantic_keys = ['title', 'number', *project_document_fields(contract).values()]
        selected = {}
        for key in dict.fromkeys(semantic_keys):
            if key not in project_fields:
                raise OnboardingError(f'Project contract is missing the {key} field.')
            matches = names(table['fields'], project_fields[key]['name'])
            if len(matches) != 1:
                raise OnboardingError(f'Cannot uniquely locate project field {key}.')
            selected[key] = matches[0]
        record = cli.read_record(token, table['id'], project_record_id, list(selected.values()))
        values = {key: record['values'][field['id']] for key, field in selected.items()}
        snapshot['project_record'] = {'id': project_record_id, 'table_id': table['id'], 'verified': True,
                                      'values': values, 'manifest': record['manifest']}
        snapshot['project_documents'] = {}
        for purpose, field_key in project_document_fields(contract).items():
            try:
                reference = parse_docx_reference(values.get(field_key))
                check = cli.read_document(reference, values.get('number'))
                snapshot['project_documents'][purpose] = {**check, 'project_record_id': project_record_id}
            except OnboardingError as exc:
                snapshot['project_documents'][purpose] = {'verified': False, 'project_record_id': project_record_id,
                                                          'error': str(exc)}
    final_base = cli.read('+base-get', '--base-token', token).get('base', {})
    if final_base.get('revision') != base.get('revision'):
        raise OnboardingError('Base changed during discovery; retry after provisioning or editing finishes.')
    return snapshot


def project_document_fields(contract):
    defaults = {'requirements_and_bugs': 'requirements_doc', 'verification_and_delivery': 'evidence_doc'}
    result = {}
    for purpose, spec in contract.get('documents', {}).items():
        key = spec.get('project_field') or defaults.get(purpose)
        if not isinstance(key, str) or not key:
            raise OnboardingError(f'documents.{purpose}: no project-row field is declared.')
        result[purpose] = key
    return result


def document_ownership(anchor_title, project_number):
    """A section's existence is separate from evidence that it belongs to a project."""
    if not isinstance(anchor_title, str) or not project_number:
        return False
    number = str(project_number).strip()
    explicit_numbers = set(re.findall(r'(?<![A-Za-z0-9])PRJ-[A-Za-z0-9]+(?![A-Za-z0-9])', anchor_title))
    if explicit_numbers and explicit_numbers != {number}:
        raise OnboardingError('Project document section explicitly identifies a different or ambiguous project number.')
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(number) + r'(?![A-Za-z0-9])', anchor_title) is not None


def parse_docx_reference(value):
    if not isinstance(value, str) or not value.strip():
        raise OnboardingError('Project document link is empty; bind the selected project row to its document or section.')
    url = value.strip()
    markdown = re.fullmatch(r'\[[^\]]*\]\((https://[^\s]+)\)', url)
    if markdown:
        url = markdown.group(1)
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or not (host.endswith('.feishu.cn') or host.endswith('.larksuite.com'))
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise OnboardingError('Project document link must be a canonical HTTPS Feishu/Lark Docx URL.')
    match = re.fullmatch(r'/docx/([A-Za-z0-9]+)/*', parsed.path)
    if not match or (parsed.fragment and not re.fullmatch(r'[A-Za-z0-9_-]+', parsed.fragment)):
        raise OnboardingError('Project document link must identify a Docx token and optional real block ID.')
    return {'url': url, 'token': match.group(1), 'anchor': parsed.fragment or None}


def names(items, name):
    return [item for item in items if isinstance(item, dict) and item.get('name') == name]


def token_of(snapshot):
    return snapshot.get('base', {}).get('base_token') or snapshot.get('base', {}).get('token')


def document_tokens(snapshot):
    return {b.get('docx_token') for b in snapshot.get('blocks', []) if b.get('type') == 'docx' and b.get('docx_token')}


def option_names(field):
    return [v['name'] if isinstance(v, dict) else v for v in field.get('options', [])]


def link_target(field):
    value = field.get('link_table')
    if isinstance(value, dict):
        return value.get('id') or value.get('table_id')
    return value


def normalize_filter(value, fields, semantic=False):
    """Compare flat tuple filters without accepting extra or foreign field references."""
    if value is None:
        value = {'conditions': []}
    if not isinstance(value, dict) or set(value) - {'logic', 'conditions'}:
        raise OnboardingError('unsupported filter object')
    logic = value.get('logic', 'and')
    if logic not in ('and', 'or') or not isinstance(value.get('conditions', []), list):
        raise OnboardingError('unsupported filter logic or conditions')
    aliases = {key: key for key in fields} if semantic else {}
    if not semantic:
        for key, field in fields.items():
            for alias in (field.get('id'), field.get('name')):
                if alias:
                    if alias in aliases and aliases[alias] != key:
                        raise OnboardingError('ambiguous filter field')
                    aliases[alias] = key
    conditions = []
    for condition in value.get('conditions', []):
        if not isinstance(condition, list) or len(condition) not in (2, 3):
            raise OnboardingError('unsupported filter condition shape')
        field, operator = condition[:2]
        if field not in aliases or not isinstance(operator, str):
            raise OnboardingError('filter references an unknown field')
        normalized = [aliases[field], operator]
        if len(condition) == 3:
            operand = condition[2]
            if isinstance(operand, list) and operator in ('intersects', 'does_not_intersect', 'is', 'is_not'):
                operand = sorted(operand, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
            normalized.append(operand)
        conditions.append(normalized)
    conditions.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return {'logic': logic if conditions else 'and', 'conditions': conditions}


def project_view_coverage(table, project_table_id, scope, project_record_id, project_field_name=None):
    """List optional views that truly isolate this project; other project views coexist."""
    if scope != 'project-record' or not project_record_id or project_field_name is None:
        return [], []
    ownership_fields = names(table.get('fields', []), project_field_name)
    if len(ownership_fields) != 1:
        return [], []
    field = ownership_fields[0]
    if field.get('type') != 'link' or link_target(field) != project_table_id:
        return [], []
    aliases = {field['id'], field['name']}
    scoped = []
    for view in table.get('views', []):
        if view.get('type') == 'form':
            continue
        value = view.get('filter_config', {}).get('filter') or {'conditions': []}
        if (not isinstance(value, dict) or set(value) - {'logic', 'conditions'}
                or not isinstance(value.get('conditions', []), list) or value.get('logic', 'and') not in ('and', 'or')):
            continue
        conditions = value.get('conditions', [])
        if any(not isinstance(c, list) or len(c) not in (2, 3) or not isinstance(c[0], str) for c in conditions):
            continue
        ownership = [c for c in conditions if c[0] in aliases]
        if (ownership and all(len(c) == 3 and c[1] == 'intersects' and c[2] == [{'id': project_record_id}] for c in ownership)
                and (value.get('logic', 'and') == 'and' or len(conditions) == 1)):
            scoped.append(view['id'])
    return scoped, []


def valid_governance(governance):
    if not isinstance(governance, dict):
        return False
    if isinstance(governance.get('rules_reference'), str) and governance['rules_reference'].strip():
        return True
    required = ('ledger_maintainer', 'scope_confirmers', 'delivery_report_channel', 'independent_verification', 'feedback_entry')
    return all(bool(governance.get(key)) for key in required)


def validate(snapshot, contract, token, url, project_key, project_name, scope,
             governance=None, source_snapshot=None, project_record_id=None):
    errors, warnings, failed_fields = [], [], set()
    actual_tables = snapshot.get('tables', [])
    if snapshot.get('snapshot_format') != 1 or not isinstance(actual_tables, list):
        raise OnboardingError('Unsupported or malformed discovery snapshot.')
    if token_of(snapshot) != token:
        errors.append('Snapshot Base identity does not match --base-url.')
    if source_snapshot and token_of(source_snapshot) == token:
        errors.append('Target is the source template Base; bind a copy instead.')
    if source_snapshot and document_tokens(snapshot) & document_tokens(source_snapshot):
        errors.append('Target retains source document tokens; create independent copied documents before binding.')
    table_ids = [t.get('id') for t in actual_tables]
    if None in table_ids or len(set(table_ids)) != len(table_ids):
        errors.append('Table IDs are missing or duplicated.')
    mapped = {}
    for role, spec in contract.get('tables', {}).items():
        found = names(actual_tables, spec['name'])
        if len(found) != 1:
            errors.append(f'{role}: expected one table named {spec["name"]}, found {len(found)}.')
            failed_fields.update(f'{role}.{key}' for key in spec['fields'])
            continue
        table = found[0]
        mapped[role] = {'id': table['id'], 'name': table['name'], 'fields': {}, 'views': [], 'forms': []}
    for role, target in mapped.items():
        spec = contract['tables'][role]
        actual = next(t for t in actual_tables if t['id'] == target['id'])
        fields = actual.get('fields', [])
        field_ids = [f.get('id') for f in fields]
        if None in field_ids or len(set(field_ids)) != len(field_ids):
            errors.append(f'{role}: field IDs are missing or duplicated.')
            failed_fields.update(f'{role}.{key}' for key in spec['fields'])
        for key, expected in spec['fields'].items():
            path = f'{role}.{key}'
            candidates = names(fields, expected['name'])
            issues = []
            if len(candidates) != 1:
                issues.append(f'expected one field named {expected["name"]}, found {len(candidates)}')
            else:
                field = candidates[0]
                binding = {k: copy.deepcopy(field[k]) for k in ('id', 'name', 'type', 'style', 'options', 'multiple', 'default_value', 'bidirectional_link_field_id') if k in field}
                if field.get('type') != expected['type']:
                    issues.append(f'type {field.get("type")} does not match {expected["type"]}')
                if 'default_value' in expected and field.get('default_value') != expected['default_value']:
                    issues.append('default value is missing or differs from the template')
                if expected['type'] == 'select':
                    missing = set(option_names(expected)) - set(option_names(field))
                    if missing:
                        issues.append('missing options: ' + ', '.join(sorted(missing)))
                    if bool(field.get('multiple', False)) != bool(expected.get('multiple', False)):
                        issues.append('single/multiple select mismatch')
                    binding['write_options'] = option_names(field)
                if expected['type'] == 'link':
                    target_role = expected.get('target_role')
                    actual_target = link_target(field)
                    target_id = mapped.get(target_role, {}).get('id')
                    if not actual_target or actual_target != target_id:
                        issues.append(f'link target is not the discovered {target_role} table')
                    if bool(field.get('bidirectional', False)) != bool(expected.get('bidirectional', False)):
                        issues.append('relationship direction mismatch')
                    binding.update(target_table_id=actual_target, target_role=target_role,
                                   bidirectional=bool(field.get('bidirectional', False)))
                if expected.get('style', {}).get('type') and field.get('style', {}).get('type') != expected['style']['type']:
                    issues.append('field style mismatch')
                target['fields'][key] = binding
            if issues:
                failed_fields.add(path)
                errors.extend(path + ': ' + issue for issue in issues)
        views = actual.get('views', [])
        scoped_views, scope_errors = project_view_coverage(
            actual, mapped.get('projects', {}).get('id'), scope, project_record_id,
            spec['fields'].get('project', {}).get('name'))
        errors.extend(scope_errors)
        if scope == 'project-record' and role != 'projects':
            target['project_view_ids'] = scoped_views
            if not scoped_views:
                warnings.append(f'{role}: no dedicated project view; record queries must still filter by the bound project record ID.')
        target['views'] = [{k: v[k] for k in ('id', 'name', 'type') if k in v} for v in views]
        target['forms'] = [{k: f[k] for k in ('id', 'form_id', 'name') if k in f} for f in actual.get('forms', [])]
        if spec.get('quick_view'):
            candidates = names(views, spec.get('quick_view_name', '快速总览'))
            if len(candidates) != 1:
                errors.append(f'{role}: 快速总览 view is missing or ambiguous.')
            else:
                visible = candidates[0].get('visible_fields_config', {}).get('visible_fields')
                if visible is None:
                    errors.append(f'{role}: visible fields were not verified.')
                else:
                    if visible and isinstance(visible[0], dict):
                        visible = [f.get('id') or f.get('field_id') or f.get('name') for f in visible]
                    required = [target['fields'].get(key, {}) for key in spec['quick_view']]
                    if any(f.get('id') not in visible and f.get('name') not in visible for f in required):
                        errors.append(f'{role}: 快速总览 omits required visible fields.')
                if 'filter_config' not in candidates[0]:
                    errors.append(f'{role}: 快速总览 filter was not verified.')
                else:
                    filter_config = candidates[0]['filter_config'].get('filter')
                    expected_filter = spec.get('quick_filter', {'conditions': []})
                    try:
                        actual_normalized = normalize_filter(filter_config, target['fields'])
                        if scope == 'project-record' and candidates[0]['id'] in scoped_views:
                            actual_normalized['conditions'] = [c for c in actual_normalized['conditions'] if c[0] != 'project']
                            if not actual_normalized['conditions']:
                                actual_normalized['logic'] = 'and'
                        expected_normalized = normalize_filter(expected_filter, target['fields'], semantic=True)
                        if actual_normalized != expected_normalized:
                            errors.append(f'{role}: 快速总览 filter differs from the template; verify copied project filters.')
                    except OnboardingError as exc:
                        errors.append(f'{role}: 快速总览 filter cannot be verified: {exc}')
    for role, spec in contract.get('tables', {}).items():
        for key, field_spec in spec['fields'].items():
            reverse_path = field_spec.get('reverse_field')
            if not reverse_path:
                continue
            pair = reverse_path.split('.')
            source_field = mapped.get(role, {}).get('fields', {}).get(key, {})
            reverse = mapped.get(pair[0], {}).get('fields', {}).get(pair[1], {}) if len(pair) == 2 else {}
            if not source_field or source_field.get('bidirectional_link_field_id') != reverse.get('id') or not reverse.get('id'):
                errors.append(f'{role}.{key}: native reverse field does not match {reverse_path}.')
                failed_fields.add(f'{role}.{key}')
            elif source_field:
                source_field['reverse_field'] = reverse_path
    mapped_table_ids = {table['id'] for table in mapped.values()}
    for extra_table in actual_tables:
        if extra_table.get('id') not in mapped_table_ids:
            _, scope_errors = project_view_coverage(extra_table, mapped.get('projects', {}).get('id'), scope, project_record_id)
            errors.extend(scope_errors)
    documents = {}
    if scope == 'project-record':
        record = snapshot.get('project_record', {})
        values = record.get('values', {})
        for purpose, field_key in project_document_fields(contract).items():
            try:
                reference = parse_docx_reference(values.get(field_key))
            except OnboardingError as exc:
                errors.append(f'documents.{purpose}: {exc}')
                continue
            check = snapshot.get('project_documents', {}).get(purpose, {})
            if (check.get('verified') is not True or check.get('project_record_id') != project_record_id
                    or any(check.get(key) != reference.get(key) for key in ('url', 'token', 'anchor'))):
                errors.append(f'documents.{purpose}: selected project document or section was not verified: ' + check.get('error', 'missing or mismatched read evidence'))
                continue
            if source_snapshot and reference['token'] in document_tokens(source_snapshot):
                errors.append(f'documents.{purpose}: selected project still links to a source template document.')
                continue
            try:
                project_verified = document_ownership(check.get('anchor_title'), values.get('number')) if reference['anchor'] else False
            except OnboardingError as exc:
                errors.append(f'documents.{purpose}: {exc}')
                continue
            if not project_verified:
                warnings.append(f'documents.{purpose}: link and section existence were read, but document ownership is not independently confirmed by its heading; binding is based on the selected project row.')
            documents[purpose] = {**reference, 'name': contract['documents'][purpose]['name'], 'type': 'docx',
                                  'project_record_id': project_record_id, 'project_field': field_key,
                                  'project_verified': project_verified, 'binding_basis': 'selected_project_record_link',
                                  'revision_id': check.get('revision_id')}
        identities = {(d['token'], d['anchor']) for d in documents.values()}
        if len(identities) != len(documents):
            errors.append('Different project document purposes resolve to the same document section.')
    else:
        for purpose, spec in contract.get('documents', {}).items():
            found = [b for b in snapshot.get('blocks', []) if b.get('type') == 'docx' and b.get('name') == spec['name']]
            if len(found) != 1 or not found[0].get('docx_token'):
                errors.append(f'documents.{purpose}: document is missing or ambiguous.')
            else:
                documents[purpose] = {'token': found[0]['docx_token'], 'name': found[0]['name'], 'type': 'docx', 'block_id': found[0]['id']}
        if len({d['token'] for d in documents.values()}) != len(documents):
            errors.append('Different document purposes resolve to the same document.')
    feedback_entry = None
    form_spec = contract.get('feedback_form')
    if form_spec:
        role = form_spec['table_role']
        table_id = mapped.get(role, {}).get('id')
        table = next((t for t in actual_tables if t.get('id') == table_id), {})
        forms = names(table.get('forms', []), form_spec['name'])
        if len(forms) != 1:
            errors.append('feedback_form: submission form is missing or ambiguous.')
        else:
            share = forms[0].get('share_config', {})
            settings = share.get('settings', {})
            feedback_entry = {
                'enabled': share.get('enabled'),
                'access_scope': share.get('access_scope'),
                'require_login': settings.get('require_login'),
                'allow_anonymous': settings.get('allow_anonymous'),
                'share_url': share.get('share_url'),
                'submission_verified': False,
            }
            if share.get('enabled') is False:
                warnings.append('Feedback form is disabled: its structure can be bound, but it is not submission-ready. Activate only with separate authorization; this tool does not publish it.')
            elif share.get('enabled') is not True or not share.get('share_url'):
                warnings.append('Feedback submission entry is not confirmed usable; this inspection does not enable or publish the form.')
            question_data = forms[0].get('questions_config', {})
            questions = question_data.get('items', question_data.get('questions', []))
            allowed_question_ids = {mapped.get(role, {}).get('fields', {}).get(key, {}).get('id')
                                    for key in form_spec['fields']}
            extras = [q.get('title') or q.get('id') or q.get('field_id') for q in questions
                      if q.get('visible') is not False and (q.get('id') or q.get('field_id')) not in allowed_question_ids]
            if extras:
                errors.append('feedback_form: unexpected maintenance or foreign questions: ' + ', '.join(map(str, extras)))
            for key in form_spec['fields']:
                field = mapped.get(role, {}).get('fields', {}).get(key, {})
                matching = [q for q in questions if (q.get('id') or q.get('field_id')) == field.get('id')]
                if len(matching) != 1 or matching[0].get('visible') is False:
                    errors.append(f'feedback_form.{key}: visible question is missing or ambiguous.')
                elif key in form_spec.get('required', []) and matching[0].get('required') is not True:
                    errors.append(f'feedback_form.{key}: required question is optional.')
    binding_scope = {'mode': scope.replace('-', '_')}
    if scope == 'project-record':
        project_id = mapped.get('projects', {}).get('id')
        verified = snapshot.get('project_record', {})
        if (not project_record_id or verified.get('id') != project_record_id
                or verified.get('table_id') != project_id or verified.get('verified') is not True):
            errors.append('Project record scope has not been verified against the selected Base.')
        values = verified.get('values', {})
        if not isinstance(values.get('title'), str) or not values['title'].strip():
            errors.append('Selected project record has no verified project name.')
        elif values['title'] != project_name:
            errors.append('Selected project record name does not match --project-name; verify the selected project.')
        if values.get('number') in (None, ''):
            errors.append('Selected project record has no verified project number.')
        binding_scope.update(project_table_id=project_id, project_record_id=project_record_id)
        for role in mapped:
            if role != 'projects' and 'project' not in mapped[role]['fields']:
                errors.append(f'{role}: project-scoped operations require a project relation.')
    elif project_record_id:
        errors.append('--project-record-id is only valid with --scope project-record.')
    elif scope == 'whole-base':
        project_table_id = mapped.get('projects', {}).get('id')
        project_table = next((t for t in actual_tables if t.get('id') == project_table_id), {})
        if project_table.get('records_count', 0) > 1:
            errors.append('Whole-Base scope contains multiple project records; use a verified project-record scope.')
    capabilities = {}
    for name, required in contract.get('capabilities', {}).items():
        missing = [path for path in required if path in failed_fields or not _mapped_path(mapped, path)]
        capabilities[name] = {'schema_ready': not missing, 'missing_or_invalid_fields': missing}
        if missing and not any(path in failed_fields for path in missing):
            errors.append(f'capabilities.{name}: contract refers to missing fields: {missing}')
    governance_ready = valid_governance(governance)
    if not governance_ready:
        warnings.append('Maintenance responsibilities are not bound; inspection only. Provide --governance before saving an active binding.')
    report = {
        'status': 'partial' if errors else ('ready' if governance_ready else 'inspection'),
        'errors': errors, 'warnings': warnings, 'capabilities': capabilities,
        'governance_ready': governance_ready, 'remote_writes': 0,
        'source_copy_checked': source_snapshot is not None,
        'evidence_mode': 'discovery_snapshot', 'observed_at': snapshot.get('observed_at'),
        'config_saved': False,
        'validation_level': 'structure_binding', 'write_access_verified': False,
        'feedback_entry': feedback_entry,
    }
    config = {
        'format_version': 2, 'project_key': project_key, 'project_name': project_name,
        'template': {'id': contract['template_id'], 'version': contract['template_version']},
        'base': {'token': token, 'url': url}, 'scope': binding_scope,
        'tables': mapped, 'documents': documents, 'governance': governance,
        'observed_at': snapshot.get('observed_at'),
        'mapping_source': 'Read-only discovery; business authorization remains governed by project rules.',
    }
    if scope == 'project-record':
        config['project_number'] = snapshot.get('project_record', {}).get('values', {}).get('number')
    return config, report


def _mapped_path(mapped, path):
    pieces = path.split('.')
    return len(pieces) == 2 and pieces[1] in mapped.get(pieces[0], {}).get('fields', {})


def semantic_config(config):
    value = copy.deepcopy(config)
    value.pop('observed_at', None)
    return value


def merge_discovered(current, discovered):
    """Update observed properties while preserving local, unrecognized extensions."""
    if isinstance(current, dict) and isinstance(discovered, dict):
        merged = copy.deepcopy(current)
        for key, value in discovered.items():
            merged[key] = merge_discovered(current[key], value) if key in current else copy.deepcopy(value)
        return merged
    if isinstance(current, list) and isinstance(discovered, list):
        # Views/forms/options may carry local annotations. Preserve those on the
        # same observed entity, but do not retain entities absent from inventory.
        identity = next((key for key in ('id', 'form_id', 'name')
                         if discovered and all(isinstance(item, dict) and key in item for item in discovered)
                         and all(isinstance(item, dict) and key in item for item in current)), None)
        if identity:
            old = {item[identity]: item for item in current}
            if len(old) != len(current):
                raise OnboardingError('Cannot refresh an ambiguous list with duplicate identity keys.')
            return [merge_discovered(old[item[identity]], item) if item[identity] in old else copy.deepcopy(item)
                    for item in discovered]
    return copy.deepcopy(discovered)


def refresh_conflicts(current, discovered):
    """Changing resource identity or manual governance needs an explicit rebind."""
    conflicts = []
    for key in ('project_name', 'governance'):
        if key in current and current[key] != discovered.get(key):
            conflicts.append(key + ' is project-maintained and differs')
    if current.get('template', {}).get('id') != discovered.get('template', {}).get('id'):
        conflicts.append('template identity differs')
    for role, table in discovered['tables'].items():
        previous = current.get('tables', {}).get(role)
        if not isinstance(previous, dict):
            continue
        if previous.get('id') != table['id']:
            conflicts.append(f'tables.{role}.id changed')
        for key, field in table['fields'].items():
            old = previous.get('fields', {}).get(key)
            if not isinstance(old, dict):
                continue
            for attribute in ('id', 'type', 'target_table_id', 'bidirectional'):
                if attribute in old and old[attribute] != field.get(attribute):
                    conflicts.append(f'tables.{role}.fields.{key}.{attribute} changed')
    for purpose, document in discovered['documents'].items():
        old = current.get('documents', {}).get(purpose)
        if isinstance(old, dict):
            for identity_key in ('token', 'anchor'):
                if old.get(identity_key) != document.get(identity_key):
                    conflicts.append(f'documents.{purpose}.{identity_key} changed')
    return conflicts


def persist(output, config, report, refresh=False):
    output = Path(output)
    report['config_saved'] = False
    report.pop('config_action', None)
    if report['status'] != 'ready':
        return
    if output.exists():
        current = read_json(output)
        if current.get('format_version') != 2:
            report['status'] = 'blocked'
            report['errors'].append('Legacy binding preserved; this command does not automatically migrate its format.')
            return
        scope_identity = ('mode', 'project_table_id', 'project_record_id')
        identity_matches = (current.get('base', {}).get('token') == config['base']['token'] and
                            current.get('project_key') == config['project_key'] and
                            all(current.get('scope', {}).get(key) == config['scope'].get(key) for key in scope_identity))
        if not identity_matches:
            report['status'] = 'blocked'
            report['errors'].append('Existing binding preserved. Refresh requires the same Base, project key, and scope.')
            return
        conflicts = refresh_conflicts(current, config)
        if conflicts:
            report['status'] = 'blocked'
            report['errors'].append('Existing binding preserved; resolve refresh conflicts explicitly: ' + '; '.join(conflicts))
            return
        config = merge_discovered(current, config)
        if semantic_config(current) == semantic_config(config):
            report['config_saved'] = True
            report['config_action'] = 'unchanged'
            return
        if not refresh:
            report['status'] = 'blocked'
            report['errors'].append('Existing binding preserved. Changed discovered properties require --refresh.')
            return
    atomic_json(output, config)
    report['config_saved'] = True
    report['config_action'] = 'refreshed' if refresh else 'created'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--base-url', required=True)
    result.add_argument('--project-key', required=True)
    result.add_argument('--project-name', required=True)
    result.add_argument('--contract', required=True, type=Path, help='Separate template/schema.json; never inferred from a live project.')
    result.add_argument('--scope', default='project-record', choices=['whole-base', 'project-record'],
                        help='Default: project-record for a Base containing multiple projects; whole-base retains legacy dedicated-Base binding.')
    result.add_argument('--project-record-id')
    result.add_argument('--governance', type=Path)
    result.add_argument('--snapshot', type=Path, help='Validate a recorded discovery snapshot without remote calls.')
    result.add_argument('--save-snapshot', type=Path)
    result.add_argument('--source-snapshot', type=Path, help='Check isolation from the source template Base and Docx resources.')
    result.add_argument('--profile')
    result.add_argument('--output', required=True, type=Path)
    result.add_argument('--refresh', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    report_path = args.output.with_suffix('.report.json')
    report = {'status': 'blocked', 'errors': [], 'warnings': [], 'remote_writes': 0, 'config_saved': False,
              'validation_level': 'structure_binding', 'write_access_verified': False, 'feedback_entry': None}
    try:
        token, url = parse_base_url(args.base_url)
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', args.project_key):
            raise OnboardingError('Project key must be a stable lowercase identifier, up to 64 characters.')
        if not args.project_name.strip():
            raise OnboardingError('Project name cannot be empty.')
        if args.scope == 'project-record' and not args.project_record_id:
            raise OnboardingError('A multi-project Base requires --project-record-id for the selected project.')
        protected = [args.contract, args.snapshot, args.source_snapshot, args.governance]
        outputs = [args.output, report_path, args.save_snapshot]
        resolved = [p.resolve() for p in outputs if p]
        if len(resolved) != len(set(resolved)) or any(p and p.resolve() in resolved for p in protected):
            raise OnboardingError('Input/output paths must be distinct.')
        contract = read_json(args.contract)
        if not contract.get('tables') or not contract.get('capabilities') or not contract.get('documents'):
            raise OnboardingError('Template contract must declare tables, capabilities and documents.')
        if contract.get('mapping_format_version') != 2:
            raise OnboardingError('Unsupported template mapping format version.')
        governance = read_json(args.governance) if args.governance else None
        if governance is None and args.refresh and args.output.exists():
            governance = read_json(args.output).get('governance')
        cli = Cli(args.profile)
        snapshot = read_json(args.snapshot) if args.snapshot else collect(cli, token, contract, args.project_record_id)
        if args.save_snapshot:
            atomic_json(args.save_snapshot, snapshot)
        source = read_json(args.source_snapshot) if args.source_snapshot else None
        config, report = validate(snapshot, contract, token, url, args.project_key, args.project_name,
                                  args.scope, governance, source, args.project_record_id)
        report['evidence_mode'] = 'offline_snapshot' if args.snapshot else 'live_read_only'
        report['commands'] = cli.calls
        config['validation_mode'] = report['evidence_mode']
        if args.snapshot:
            report['warnings'].append('Offline validation describes the recorded snapshot, not current server state or write permission.')
        persist(args.output, config, report, args.refresh)
    except (OnboardingError, OSError, ValueError, KeyError, TypeError) as exc:
        report['status'] = 'blocked'
        report['errors'].append(str(exc))
    # Never overwrite an input even while reporting a preflight failure.
    protected_paths = [getattr(args, key, None) for key in ('contract', 'snapshot', 'source_snapshot', 'governance')]
    if report_path.resolve() != args.output.resolve() and all(p is None or p.resolve() != report_path.resolve() for p in protected_paths):
        atomic_json(report_path, report)
    print(json.dumps({'status': report['status'], 'validation_level': report['validation_level'],
                      'write_access_verified': report['write_access_verified'], 'feedback_entry': report['feedback_entry'],
                      'config_saved': report['config_saved'], 'report': str(report_path), 'errors': report['errors'], 'warnings': report['warnings']},
                     ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'ready' else 2


if __name__ == '__main__':
    sys.exit(main())
