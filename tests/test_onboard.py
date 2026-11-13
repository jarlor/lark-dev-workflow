"""Behavioral tests for safe copy binding and partial discovery."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts/onboard.py'
spec = importlib.util.spec_from_file_location('onboard', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    contract = {
        'template_id': 'test-workflow', 'template_version': '1.0.0', 'mapping_format_version': 2,
        'tables': {
            'projects': {'name': 'Projects', 'fields': {'title': {'name': 'Title', 'type': 'text'}}},
            'work': {'name': 'Work', 'fields': {
                'title': {'name': 'Title', 'type': 'text'},
                'status': {'name': 'Status', 'type': 'select', 'options': [{'name': 'Open'}, {'name': 'Done'}]},
                'project': {'name': 'Project', 'type': 'link', 'target_role': 'projects', 'bidirectional': False},
            }, 'quick_view': ['title', 'status']},
        },
        'documents': {'requirements_and_bugs': {'name': 'Scope'}},
        'capabilities': {'read_project': ['projects.title'], 'track_work': ['work.title', 'work.status', 'work.project']},
    }
    snapshot = {
        'snapshot_format': 1, 'observed_at': '2026-10-26T05:00:00Z',
        'base': {'base_token': 'CopyFixture', 'url': 'https://example.feishu.cn/base/CopyFixture'},
        'blocks': [{'id': 'blockdoc', 'type': 'docx', 'name': 'Scope', 'docx_token': 'CopyDocFixture'}],
        'tables': [
            {'id': 'tblProjectFixture', 'name': 'Projects', 'fields': [{'id': 'fldProjectTitle', 'name': 'Title', 'type': 'text'}]},
            {'id': 'tblWorkFixture', 'name': 'Work', 'fields': [
                {'id': 'fldWorkTitle', 'name': 'Title', 'type': 'text'},
                {'id': 'fldWorkStatus', 'name': 'Status', 'type': 'select', 'options': [{'name': 'Open'}, {'name': 'Done'}]},
                {'id': 'fldWorkProject', 'name': 'Project', 'type': 'link', 'link_table': 'tblProjectFixture', 'bidirectional': False},
            ], 'views': [{'id': 'viewFixture', 'name': '快速总览', 'type': 'grid',
                          'visible_fields_config': {'visible_fields': ['Title', 'Status']},
                          'filter_config': {'filter': {'conditions': []}}}], 'forms': []},
        ],
    }
    return contract, snapshot


def check(contract, snapshot, **kwargs):
    return m.validate(snapshot, contract, 'CopyFixture', 'https://example.feishu.cn/base/CopyFixture',
                      'demo', kwargs.pop('project_name', 'Demo'), kwargs.pop('scope', 'whole-base'),
                      kwargs.pop('governance', {'rules_reference': 'PROJECT-RULES.md'}), **kwargs)


def select_project(contract, snapshot, record_id='recProjectFixture', title='Demo', number='PRJ-0001', anchor='blockA'):
    project = contract['tables']['projects']
    actual = snapshot['tables'][0]
    for key, name, kind, identifier in [
        ('number', 'Number', 'auto_number', 'fldProjectNumber'),
        ('requirements_doc', 'Requirements document', 'text', 'fldProjectDocument'),
    ]:
        if key not in project['fields']:
            project['fields'][key] = {'name': name, 'type': kind}
            actual['fields'].append({'id': identifier, 'name': name, 'type': kind})
    contract['documents']['requirements_and_bugs']['project_field'] = 'requirements_doc'
    url = 'https://example.feishu.cn/docx/SharedDocFixture#' + anchor
    snapshot['project_record'] = {'id': record_id, 'table_id': actual['id'], 'verified': True,
                                  'values': {'title': title, 'number': number, 'requirements_doc': url}}
    snapshot['project_documents'] = {'requirements_and_bugs': {
        'url': url, 'token': 'SharedDocFixture', 'anchor': anchor, 'verified': True,
        'project_record_id': record_id, 'anchor_title': number + ' · ' + title, 'revision_id': 1,
    }}
    return snapshot


class BindingTests(unittest.TestCase):
    def test_stage_contract_preserves_config_and_rejects_wrong_terminal_kind(self):
        contract, snapshot = fixture()
        spec = contract['tables']['work']['fields']['status']
        spec.update(type='stage', options=[{'name': 'Open', 'type': 'Default'},
                                          {'name': 'Done', 'type': 'EndDone'}])
        field = snapshot['tables'][1]['fields'][1]
        field.update(type='stage', options=copy.deepcopy(spec['options']), stage_config={
            'property': {'convertMode': 'NextStage', 'options': copy.deepcopy(spec['options'])}})
        config, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'ready')
        mapped = config['tables']['work']['fields']['status']
        self.assertEqual(mapped['stage_config'], field['stage_config'])
        self.assertNotIn('write_options', mapped)
        field['options'][1]['type'] = 'Default'
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('node kind differs' in e for e in report['errors']))
        field['type'] = 'select'
        field.pop('stage_config')
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('ordinary select' in e for e in report['errors']))

    def test_valid_copy_records_actual_field_and_relation_ids(self):
        contract, snapshot = fixture()
        config, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(config['tables']['work']['fields']['project']['target_table_id'], 'tblProjectFixture')
        self.assertTrue(report['capabilities']['track_work']['schema_ready'])
        self.assertEqual(report['remote_writes'], 0)

    def test_copy_must_not_reuse_source_base_or_doc(self):
        contract, snapshot = fixture()
        _, report = check(contract, snapshot, source_snapshot=copy.deepcopy(snapshot))
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('source template' in e for e in report['errors']))
        self.assertTrue(any('source document' in e for e in report['errors']))
        source = copy.deepcopy(snapshot)
        source['base']['base_token'] = 'SourceFixture'
        source['blocks'][0]['docx_token'] = 'SourceDocFixture'
        _, valid = check(contract, snapshot, source_snapshot=source)
        self.assertEqual(valid['status'], 'ready')  # IDs may be scoped to different Bases.

    def test_bad_link_target_disables_only_affected_capability(self):
        contract, snapshot = fixture()
        snapshot['tables'][1]['fields'][2]['link_table'] = 'ForeignTableFixture'
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['capabilities']['track_work']['schema_ready'])
        self.assertTrue(report['capabilities']['read_project']['schema_ready'])

    def test_select_options_type_and_direction_cannot_be_guessed(self):
        contract, snapshot = fixture()
        snapshot['tables'][1]['fields'][1]['options'] = [{'name': 'Open'}]
        snapshot['tables'][1]['fields'][2]['bidirectional'] = True
        snapshot['tables'][0]['fields'][0]['type'] = 'number'
        _, report = check(contract, snapshot)
        self.assertEqual(len(report['errors']), 3)
        self.assertTrue(any('missing options: Done' in e for e in report['errors']))

    def test_duplicate_names_not_selected_arbitrarily(self):
        contract, snapshot = fixture()
        snapshot['tables'].append(dict(snapshot['tables'][0], id='tblOtherFixture'))
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('found 2' in e for e in report['errors']))

    def test_unexpected_copied_filter_is_reported(self):
        contract, snapshot = fixture()
        snapshot['tables'][1]['views'][0]['filter_config'] = {'filter': {'conditions': [['Project', 'intersects', [{'id': 'OldProjectFixture'}]]]}}
        _, report = check(contract, snapshot)
        self.assertTrue(any('copied project filters' in e for e in report['errors']))

    def test_declared_quick_filter_accepts_name_or_id_and_rejects_extra_scope(self):
        contract, snapshot = fixture()
        contract['tables']['work']['fields']['nature'] = {'name': '记录性质', 'type': 'select',
                                                           'options': [{'name': '当前工作'}]}
        contract['tables']['work']['quick_filter'] = {'logic': 'and', 'conditions': [['nature', 'intersects', ['当前工作']]]}
        snapshot['tables'][1]['fields'].append({'id': 'fldNatureFixture', 'name': '记录性质', 'type': 'select',
                                               'options': [{'name': '当前工作'}]})
        view = snapshot['tables'][1]['views'][0]
        for field in ('记录性质', 'fldNatureFixture'):
            view['filter_config'] = {'filter': {'logic': 'and', 'conditions': [[field, 'intersects', ['当前工作']]]}}
            _, report = check(contract, snapshot)
            self.assertEqual(report['status'], 'ready')
        view['filter_config']['filter']['conditions'].append(['Project', 'intersects', [{'id': 'recOldProject'}]])
        _, report = check(contract, snapshot)
        self.assertTrue(any('differs from the template' in e for e in report['errors']))
        view['filter_config']['filter']['conditions'] = [['ForeignProject', 'intersects', [{'id': 'recOldProject'}]]]
        _, report = check(contract, snapshot)
        self.assertTrue(any('unknown field' in e for e in report['errors']))

    def test_feedback_form_questions_and_required_flags_are_checked(self):
        contract, snapshot = fixture()
        contract['feedback_form'] = {'name': 'Submit', 'table_role': 'work',
                                     'fields': ['title', 'status'], 'required': ['title']}
        snapshot['tables'][1]['forms'] = [{'id': 'formFixture', 'name': 'Submit', 'questions_config': {
            'items': [{'id': 'fldWorkTitle', 'required': True}, {'id': 'fldWorkStatus', 'required': False}]
        }}]
        _, valid = check(contract, snapshot)
        self.assertEqual(valid['status'], 'ready')
        questions = snapshot['tables'][1]['forms'][0]['questions_config']['items']
        questions.append({'id': 'fldInternalFixture', 'title': 'Record key', 'required': False})
        _, extra = check(contract, snapshot)
        self.assertTrue(any('unexpected maintenance' in e for e in extra['errors']))
        questions.pop()
        snapshot['tables'][1]['forms'][0]['questions_config']['items'][0]['required'] = False
        _, invalid = check(contract, snapshot)
        self.assertTrue(any('required question is optional' in e for e in invalid['errors']))
        snapshot['tables'][1]['forms'][0]['questions_config']['items'].pop()
        _, invalid = check(contract, snapshot)
        self.assertTrue(any('visible question is missing' in e for e in invalid['errors']))

    def test_disabled_form_is_structurally_bindable_but_not_submission_ready(self):
        contract, snapshot = fixture()
        contract['feedback_form'] = {'name': 'Submit', 'table_role': 'work', 'fields': ['title'], 'required': ['title']}
        snapshot['tables'][1]['forms'] = [{'id': 'formFixture', 'name': 'Submit',
            'questions_config': {'questions': [{'id': 'fldWorkTitle', 'required': True}]},
            'share_config': {'enabled': False, 'access_scope': 'anyone',
                             'settings': {'require_login': False, 'allow_anonymous': False}, 'share_url': ''}}]
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['validation_level'], 'structure_binding')
        self.assertIs(report['write_access_verified'], False)
        self.assertEqual(report['feedback_entry']['enabled'], False)
        self.assertEqual(report['feedback_entry']['access_scope'], 'anyone')
        self.assertIs(report['feedback_entry']['submission_verified'], False)
        self.assertTrue(any('not submission-ready' in text for text in report['warnings']))

    def test_whole_base_cannot_hide_multiple_projects(self):
        contract, snapshot = fixture()
        snapshot['tables'][0]['records_count'] = 2
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('multiple project records' in e for e in report['errors']))

    def test_project_record_scope_needs_verified_record_but_no_dedicated_view(self):
        contract, snapshot = fixture()
        _, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report['status'], 'partial')
        select_project(contract, snapshot)
        config, unscoped = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(unscoped['status'], 'ready')
        self.assertEqual(config['tables']['work']['project_view_ids'], [])
        self.assertTrue(any('queries must still filter' in warning for warning in unscoped['warnings']))
        snapshot['tables'][1]['views'][0]['filter_config'] = {'filter': {
            'logic': 'and', 'conditions': [['Project', 'intersects', [{'id': 'recProjectFixture'}]]]}}
        config, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(config['scope']['project_record_id'], 'recProjectFixture')
        self.assertEqual(config['tables']['work']['project_view_ids'], ['viewFixture'])

    def test_required_default_loss_blocks_binding(self):
        contract, snapshot = fixture()
        contract['tables']['work']['fields']['status']['default_value'] = ['Open']
        snapshot['tables'][1]['fields'][1]['default_value'] = ['Open']
        config, good = check(contract, snapshot)
        self.assertEqual(good['status'], 'ready')
        self.assertEqual(config['tables']['work']['fields']['status']['default_value'], ['Open'])
        del snapshot['tables'][1]['fields'][1]['default_value']
        _, missing = check(contract, snapshot)
        self.assertEqual(missing['status'], 'partial')
        self.assertFalse(missing['capabilities']['track_work']['schema_ready'])
        self.assertTrue(any('default value' in e for e in missing['errors']))

    def test_project_a_and_b_views_coexist_without_mixing_view_ids(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        for identifier, project_id in [('projectAView', 'recProjectFixture'), ('projectBView', 'recOtherFixture')]:
            snapshot['tables'][1]['views'].append({'id': identifier, 'name': identifier, 'type': 'grid',
                'filter_config': {'filter': {'conditions': [['fldWorkProject', 'intersects', [{'id': project_id}]]]}}})
        config, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(config['tables']['work']['project_view_ids'], ['projectAView'])
        _, whole = check(contract, snapshot)
        self.assertEqual(whole['status'], 'ready')

    def test_only_mandatory_single_project_filters_enter_scoped_view_ids(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        view = {'id': 'candidateScopeView', 'name': 'Scope candidate', 'type': 'grid', 'filter_config': {
            'filter': {'logic': 'or', 'conditions': [
                ['Project', 'intersects', [{'id': 'recProjectFixture'}]], ['Status', 'intersects', ['Done']]]}}}
        snapshot['tables'][1]['views'].append(view)
        config, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(config['tables']['work']['project_view_ids'], [])
        view['filter_config']['filter'] = {'logic': 'and', 'conditions': [
            ['Project', 'intersects', [{'id': 'recProjectFixture'}, {'id': 'recOtherFixture'}]]]}
        config, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(config['tables']['work']['project_view_ids'], [])

    def test_related_project_relation_cannot_prove_project_ownership(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        table = snapshot['tables'][1]
        table['fields'].append({'id': 'fldRelatedProject', 'name': 'Related project', 'type': 'link',
                                'link_table': 'tblProjectFixture'})
        table['views'].append({'id': 'relatedProjectView', 'name': 'Related-project view', 'type': 'grid',
                              'filter_config': {'filter': {'conditions': [
                                  ['Related project', 'intersects', [{'id': 'recProjectFixture'}]]]}}})
        config, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(config['tables']['work']['project_view_ids'], [])
        for ownership_alias in ('Project', 'fldWorkProject'):
            table['views'][-1]['filter_config']['filter']['conditions'] = [
                [ownership_alias, 'intersects', [{'id': 'recProjectFixture'}]]]
            config, accepted = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
            self.assertEqual(accepted['status'], 'ready')
            self.assertEqual(config['tables']['work']['project_view_ids'], ['relatedProjectView'])
        table['views'][-1]['filter_config']['filter']['conditions'].append(
            ['Related project', 'intersects', [{'id': 'recForeignFixture'}]])
        config, still_scoped = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(still_scoped['status'], 'ready')
        self.assertEqual(config['tables']['work']['project_view_ids'], ['relatedProjectView'])

    def test_other_project_view_in_extra_table_is_allowed(self):
        contract, snapshot = fixture()
        snapshot['tables'].append({'id': 'tblExtraFixture', 'name': 'Extra', 'fields': [
            {'id': 'fldExtraProject', 'name': 'Team project', 'type': 'link', 'link_table': 'tblProjectFixture'}],
            'views': [{'id': 'extraViewFixture', 'name': 'Extra view', 'type': 'grid',
                       'filter_config': {'filter': {'conditions': [['Team project', 'intersects', [{'id': 'recForeignFixture'}]]]}}}]})
        _, report = check(contract, snapshot)
        self.assertEqual(report['status'], 'ready')

    def test_projects_bind_distinct_sections_of_shared_documents(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        a, report_a = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(report_a['status'], 'ready')
        select_project(contract, snapshot, 'recOtherFixture', 'Other project', 'PRJ-0002', 'blockB')
        b, report_b = check(contract, snapshot, scope='project-record', project_record_id='recOtherFixture', project_name='Other project')
        self.assertEqual(report_b['status'], 'ready')
        self.assertEqual(a['documents']['requirements_and_bugs']['token'], b['documents']['requirements_and_bugs']['token'])
        self.assertNotEqual(a['documents']['requirements_and_bugs']['anchor'], b['documents']['requirements_and_bugs']['anchor'])
        self.assertEqual(a['documents']['requirements_and_bugs']['project_record_id'], 'recProjectFixture')
        self.assertTrue(a['documents']['requirements_and_bugs']['project_verified'])

    def test_project_row_documents_do_not_fall_back_to_base_named_documents(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        snapshot['project_record']['values']['requirements_doc'] = ''
        _, missing = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(missing['status'], 'partial')
        self.assertTrue(any('link is empty' in error for error in missing['errors']))
        select_project(contract, snapshot)
        snapshot['project_documents']['requirements_and_bugs']['project_record_id'] = 'recOtherFixture'
        _, wrong = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(wrong['status'], 'partial')
        self.assertTrue(any('not verified' in error for error in wrong['errors']))

    def test_wrong_project_record_name_or_document_heading_is_rejected(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        snapshot['project_record']['values']['title'] = 'Wrong project'
        _, wrong = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertTrue(any('does not match --project-name' in error for error in wrong['errors']))
        select_project(contract, snapshot)
        snapshot['project_documents']['requirements_and_bugs']['anchor_title'] = 'PRJ-0002 · Demo'
        _, wrong = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(wrong['status'], 'partial')
        self.assertTrue(any('different or ambiguous project number' in error for error in wrong['errors']))
        snapshot['project_documents']['requirements_and_bugs']['anchor_title'] = 'Custom scope section'
        config, custom = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        self.assertEqual(custom['status'], 'ready')
        self.assertFalse(config['documents']['requirements_and_bugs']['project_verified'])
        self.assertTrue(any('ownership is not independently confirmed' in warning for warning in custom['warnings']))

    def test_native_bidirectional_pair_uses_actual_reverse_field_id(self):
        contract, snapshot = fixture()
        contract['tables']['work']['fields']['project'].update(bidirectional=True, reverse_field='projects.work')
        contract['tables']['projects']['fields']['work'] = {'name': 'Project work', 'type': 'link',
            'target_role': 'work', 'bidirectional': True, 'reverse_field': 'work.project'}
        snapshot['tables'][1]['fields'][2].update(bidirectional=True, bidirectional_link_field_id='fldProjectWork')
        snapshot['tables'][0]['fields'].append({'id': 'fldProjectWork', 'name': 'Project work', 'type': 'link',
            'link_table': 'tblWorkFixture', 'bidirectional': True, 'bidirectional_link_field_id': 'fldWorkProject'})
        _, valid = check(contract, snapshot)
        self.assertEqual(valid['status'], 'ready')
        snapshot['tables'][0]['fields'][-1]['bidirectional_link_field_id'] = 'fldUnrelatedReverse'
        _, invalid = check(contract, snapshot)
        self.assertEqual(invalid['status'], 'partial')
        self.assertTrue(any('native reverse field' in error for error in invalid['errors']))

    def test_project_options_can_expand_but_cannot_gain_a_default(self):
        contract, snapshot = fixture()
        contract['tables']['work']['fields']['status'].update(default_value=None)
        snapshot['tables'][1]['fields'][1]['options'].append({'name': 'PRJ-0001 · Demo'})
        _, valid = check(contract, snapshot)
        self.assertEqual(valid['status'], 'ready')
        snapshot['tables'][1]['fields'][1]['default_value'] = ['PRJ-0001 · Demo']
        _, invalid = check(contract, snapshot)
        self.assertEqual(invalid['status'], 'partial')

    def test_doc_anchor_is_refresh_identity_and_legacy_binding_is_preserved(self):
        contract, snapshot = fixture()
        select_project(contract, snapshot)
        current, _ = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
        for change in ('anchor', 'legacy'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'project.json'
                old = copy.deepcopy(current)
                if change == 'legacy': old['format_version'] = 1
                m.atomic_json(path, old)
                before = path.read_bytes()
                candidate = copy.deepcopy(current)
                if change == 'anchor': candidate['documents']['requirements_and_bugs']['anchor'] = 'blockB'
                _, report = check(contract, snapshot, scope='project-record', project_record_id='recProjectFixture')
                m.persist(path, candidate, report, refresh=True)
                self.assertEqual(report['status'], 'blocked')
                self.assertEqual(path.read_bytes(), before)

    def test_refresh_preserves_manual_extensions_at_all_levels(self):
        contract, snapshot = fixture()
        current, report = check(contract, snapshot)
        current['local_note'] = {'owner': 'local maintainer'}
        current['scope']['local_scope_note'] = 'dedicated project Base'
        current['tables']['work']['role_filter'] = {'conditions': [['kind', 'intersects', ['development']]]}
        current['tables']['work']['fields']['title']['local_annotation'] = 'keep'
        current['tables']['work']['views'][0]['manual_purpose'] = 'daily view'
        current['documents']['requirements_and_bugs']['anchor_convention'] = 'stable numbered headings'
        snapshot['tables'][1]['fields'][1]['options'].append({'name': 'Paused'})
        discovered, fresh_report = check(contract, snapshot)
        discovered['observed_at'] = '2026-10-27T00:00:00Z'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            m.atomic_json(path, current)
            m.persist(path, discovered, fresh_report, refresh=True)
            updated = m.read_json(path)
        self.assertTrue(fresh_report['config_saved'])
        self.assertEqual(updated['local_note'], current['local_note'])
        self.assertEqual(updated['scope']['local_scope_note'], 'dedicated project Base')
        self.assertEqual(updated['tables']['work']['role_filter'], current['tables']['work']['role_filter'])
        self.assertEqual(updated['tables']['work']['fields']['title']['local_annotation'], 'keep')
        self.assertEqual(updated['tables']['work']['views'][0]['manual_purpose'], 'daily view')
        self.assertEqual(updated['documents']['requirements_and_bugs']['anchor_convention'], 'stable numbered headings')
        self.assertEqual(updated['observed_at'], '2026-10-27T00:00:00Z')

    def test_repeated_binding_with_manual_extensions_is_byte_preserving_noop(self):
        contract, snapshot = fixture()
        current, _ = check(contract, snapshot)
        current['manual_extension'] = {'notes': 'keep original bytes'}
        current['tables']['work']['role_filter'] = {'conditions': [['kind', 'intersects', ['dev']]]}
        current['documents']['requirements_and_bugs']['anchor_convention'] = 'stable headings'
        for refresh in (False, True):
            with self.subTest(refresh=refresh), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'project.json'
                m.atomic_json(path, current)
                before = path.read_bytes()
                before_stat = path.stat().st_mtime_ns
                discovered, report = check(contract, snapshot)
                discovered['observed_at'] = '2026-10-28T00:00:00Z'
                m.persist(path, discovered, report, refresh=refresh)
                self.assertEqual(report['status'], 'ready')
                self.assertEqual(report['config_action'], 'unchanged')
                self.assertTrue(report['config_saved'])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(path.stat().st_mtime_ns, before_stat)

    def test_real_discovered_change_still_requires_explicit_refresh(self):
        contract, snapshot = fixture()
        current, _ = check(contract, snapshot)
        current['manual_extension'] = 'preserve'
        snapshot['tables'][1]['fields'][1]['options'].append({'name': 'Paused'})
        discovered, report = check(contract, snapshot)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            m.atomic_json(path, current)
            before = path.read_bytes()
            m.persist(path, discovered, report)
            self.assertEqual(report['status'], 'blocked')
            self.assertFalse(report['config_saved'])
            self.assertEqual(path.read_bytes(), before)
            _, report = check(contract, snapshot)
            m.persist(path, discovered, report, refresh=True)
            self.assertEqual(report['config_action'], 'refreshed')
            updated = m.read_json(path)
            self.assertEqual(updated['manual_extension'], 'preserve')
            self.assertIn('Paused', updated['tables']['work']['fields']['status']['write_options'])

    def test_refresh_refuses_identity_or_governance_conflicts(self):
        contract, snapshot = fixture()
        current, _ = check(contract, snapshot)
        for change in ('field_id', 'document_token', 'governance'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'project.json'
                m.atomic_json(path, current)
                before = path.read_bytes()
                discovered, report = check(contract, snapshot)
                if change == 'field_id':
                    discovered['tables']['work']['fields']['title']['id'] = 'fldRecreated'
                elif change == 'document_token':
                    discovered['documents']['requirements_and_bugs']['token'] = 'ReboundDoc'
                else:
                    discovered['governance'] = {'rules_reference': 'OTHER-RULES.md'}
                m.persist(path, discovered, report, refresh=True)
                self.assertEqual(report['status'], 'blocked')
                self.assertFalse(report['config_saved'])
                self.assertEqual(path.read_bytes(), before)

    def test_governance_missing_is_inspection_not_active_binding(self):
        contract, snapshot = fixture()
        config, report = check(contract, snapshot, governance=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            m.persist(path, config, report)
            self.assertEqual(report['status'], 'inspection')
            self.assertFalse(path.exists())

    def test_failed_rerun_preserves_valid_configuration(self):
        contract, snapshot = fixture()
        config, report = check(contract, snapshot)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            m.persist(path, config, report)
            original = path.read_bytes()
            snapshot['tables'][1]['fields'].pop()
            invalid_config, invalid_report = check(contract, snapshot)
            m.persist(path, invalid_config, invalid_report, refresh=True)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(invalid_report['config_saved'])

    def test_repeat_is_noop_and_scope_change_never_overwrites(self):
        contract, snapshot = fixture()
        config, report = check(contract, snapshot)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            m.persist(path, config, report)
            original = path.read_bytes()
            second = copy.deepcopy(config)
            second['observed_at'] = '2026-10-27T05:00:00Z'
            _, second_report = check(contract, snapshot)
            m.persist(path, second, second_report)
            self.assertEqual(second_report['config_action'], 'unchanged')
            self.assertEqual(path.read_bytes(), original)
            second['scope'] = {'mode': 'project_record', 'project_record_id': 'recOther'}
            m.persist(path, second, second_report, refresh=True)
            self.assertEqual(second_report['status'], 'blocked')
            self.assertEqual(path.read_bytes(), original)


class CliTests(unittest.TestCase):
    def test_form_views_use_questions_api_not_view_configuration(self):
        class FakeCli:
            def __init__(self):
                self.calls = []
            def read(self, command, *args, **kwargs):
                self.calls.append(command)
                if command == '+base-get':
                    return {'base': {'base_token': 'CopyFixture', 'revision': 1}}
                if command == '+base-block-list':
                    return {'blocks': [], 'total': 0}
                if command == '+form-list':
                    return {'forms': [{'id': 'formFixture', 'name': 'Submit'}], 'total': 1}
                if command == '+form-questions-list':
                    return {'questions': [], 'total': 0}
                if command == '+form-share-get':
                    return {'enabled': False, 'access_scope': 'tenant', 'settings': {}, 'share_url': ''}
                raise AssertionError('Unexpected read: ' + command)
            def pages(self, command, key, *args, **kwargs):
                if command == '+table-list':
                    return [{'id': 'tblFixture', 'name': 'Work'}]
                if command == '+field-list':
                    return []
                if command == '+view-list':
                    return [{'id': 'formFixture', 'name': 'Submit', 'type': 'form'}]
                raise AssertionError('Unexpected pagination: ' + command)
        cli = FakeCli()
        result = m.collect(cli, 'CopyFixture', {})
        self.assertEqual(result['tables'][0]['forms'][0]['questions_config']['total'], 0)
        self.assertNotIn('+view-get-visible-fields', cli.calls)
        self.assertIn('+form-questions-list', cli.calls)

    def test_ndjson_record_manifest_succeeds_without_ok_envelope(self):
        def runner(argv, **kwargs):
            output = Path(kwargs['cwd']) / 'project.ndjson'
            output.write_text(json.dumps({'record_id': 'recProjectFixture', 'Title': 'Demo'}) + '\n')
            manifest = {'manifest_version': 'v1', 'format': 'ndjson', 'base_token': 'CopyFixture',
                'table_id': 'tblProjectFixture', 'rev': 1, 'timezone': 'Asia/Shanghai',
                'query_context': {'record_scope': 'selected_record_ids', 'requested_record_count': 1},
                'records_count': 1, 'page_count': 1, 'has_more': False, 'record_file': str(output),
                'record_file_size_bytes': output.stat().st_size,
                'columns': {'record_id': {'physical_type': 'string'}, 'Title': {'field_id': 'fldProjectTitle', 'field_type': 'text'}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(manifest), '')
        result = m.Cli(runner=runner).read_record('CopyFixture', 'tblProjectFixture', 'recProjectFixture',
                                                [{'id': 'fldProjectTitle', 'name': 'Title'}])
        self.assertEqual(result['record_id'], 'recProjectFixture')
        self.assertEqual(result['values']['fldProjectTitle'], 'Demo')
        self.assertIs(result['manifest']['has_more'], False)

    def test_ndjson_manifest_checks_identity_completeness_file_and_record(self):
        mutations = ['base', 'table', 'count', 'has_more', 'scope', 'path', 'size', 'record_id', 'missing_file', 'projection', 'returncode', 'envelope']
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                def runner(argv, **kwargs):
                    output = Path(kwargs['cwd']) / 'project.ndjson'
                    record = {'record_id': 'recWrong' if mutation == 'record_id' else 'recProjectFixture', 'Title': 'Demo'}
                    output.write_text(json.dumps(record) + '\n')
                    manifest = {'manifest_version': 'v1', 'format': 'ndjson', 'base_token': 'CopyFixture',
                        'table_id': 'tblProjectFixture', 'query_context': {'record_scope': 'selected_record_ids', 'requested_record_count': 1},
                        'records_count': 1, 'page_count': 1, 'has_more': False, 'record_file': str(output),
                        'record_file_size_bytes': output.stat().st_size, 'columns': {'Title': {'field_id': 'fldProjectTitle'}}}
                    if mutation == 'base': manifest['base_token'] = 'OtherBase'
                    if mutation == 'table': manifest['table_id'] = 'OtherTable'
                    if mutation == 'count': manifest['records_count'] = 2
                    if mutation == 'has_more': manifest['has_more'] = True
                    if mutation == 'scope': manifest['query_context']['record_scope'] = 'all_records'
                    if mutation == 'path': manifest['record_file'] = str(output.parent / 'other.ndjson')
                    if mutation == 'size': manifest['record_file_size_bytes'] += 1
                    if mutation == 'missing_file': output.unlink()
                    if mutation == 'projection': manifest['columns']['Title']['field_id'] = 'OtherField'
                    if mutation == 'envelope': manifest = {'ok': True, 'data': manifest}
                    return subprocess.CompletedProcess(argv, 1 if mutation == 'returncode' else 0, json.dumps(manifest), '')
                with self.assertRaises(m.OnboardingError):
                    m.Cli(runner=runner).read_record('CopyFixture', 'tblProjectFixture', 'recProjectFixture',
                                                   [{'id': 'fldProjectTitle', 'name': 'Title'}])

    def test_document_fetch_verifies_token_anchor_and_project_number_separately(self):
        reference = m.parse_docx_reference('[Scope](https://example.feishu.cn/docx/SharedDocFixture#blockA)')
        cases = [('PRJ-0001 · Demo', 'blockA', 'SharedDocFixture', True),
                 ('Custom heading', 'blockA', 'SharedDocFixture', False),
                 ('PRJ-0002 · Demo', 'blockA', 'SharedDocFixture', None),
                 ('PRJ-0001 · Demo', 'blockB', 'SharedDocFixture', None),
                 ('PRJ-0001 · Demo', 'blockA', 'OtherDoc', None)]
        for heading, anchor, token, expected in cases:
            with self.subTest(heading=heading, anchor=anchor, token=token):
                def runner(argv, **kwargs):
                    self.assertEqual(argv[1:3], ['docs', '+fetch'])
                    self.assertEqual(argv[argv.index('--start-block-id') + 1], 'blockA')
                    data = {'document': {'document_id': token, 'revision_id': 7,
                            'content': '<fragment mode="section" requested-start="blockA"><h1 id="' + anchor + '">' + heading + '</h1></fragment>'}}
                    return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'identity': 'user', 'data': data}), '')
                if expected is None:
                    with self.assertRaises(m.OnboardingError):
                        m.Cli(runner=runner).read_document(reference, 'PRJ-0001')
                else:
                    result = m.Cli(runner=runner).read_document(reference, 'PRJ-0001')
                    self.assertTrue(result['verified'])
                    self.assertIs(result['project_verified'], expected)
                    self.assertEqual(result['url'], 'https://example.feishu.cn/docx/SharedDocFixture#blockA')

    def test_mutation_not_in_allowlist(self):
        cli = m.Cli(runner=lambda *args, **kwargs: self.fail('runner must not execute'))
        with self.assertRaises(m.OnboardingError):
            cli.read('+record-batch-create', '--json', '{}')

    def test_stderr_error_is_not_a_success(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 1, '', json.dumps({'ok': False, 'error': {'message': 'scope denied'}}))
        with self.assertRaisesRegex(m.OnboardingError, 'scope denied'):
            m.Cli(runner=runner).read('+base-get', '--base-token', 'Fixture')

    def test_legacy_code_zero_is_not_confirmed_success(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, '{"code":0,"data":{}}', '')
        with self.assertRaises(m.OnboardingError):
            m.Cli(runner=runner).read('+base-get', '--base-token', 'Fixture')

    def test_pagination_incomplete_and_duplicate_pages_fail(self):
        for pages in [
            [{'tables': [{'id': 'A'}], 'total': 2}, {'tables': [], 'total': 2}],
            [{'tables': [{'id': 'A'}], 'total': 2}, {'tables': [{'id': 'A'}], 'total': 2}],
            [{'tables': [{'id': 'A'}], 'total': 2}, {'tables': [{'id': 'B'}], 'total': 3}],
        ]:
            with self.subTest(pages=pages):
                cli = m.Cli()
                stream = iter(pages)
                cli.read = lambda *args: next(stream)
                with self.assertRaises(m.OnboardingError):
                    cli.pages('+table-list', 'tables')

    def test_successful_pages_use_offset_and_explicit_user(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            offset = argv[argv.index('--offset') + 1]
            item = {'id': 'A' if offset == '0' else 'B'}
            return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'identity': 'user', 'data': {'tables': [item], 'total': 2}}), '')
        result = m.Cli(profile='chosen-profile', runner=runner).pages('+table-list', 'tables', '--base-token', 'Fixture')
        self.assertEqual([x['id'] for x in result], ['A', 'B'])
        for argv in calls:
            self.assertIn('--as', argv)
            self.assertEqual(argv[argv.index('--as') + 1], 'user')
            self.assertEqual(argv[argv.index('--profile') + 1], 'chosen-profile')


if __name__ == '__main__':
    unittest.main()
