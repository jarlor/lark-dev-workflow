import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('watch', SCRIPTS / 'watch.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def config():
    value = {'format_version': 2, 'project_key': 'example', 'project_number': 'PRJ-0001',
             'base': {'token': 'BaseFixture', 'url': 'https://example.feishu.cn/base/BaseFixture'},
             'scope': {'mode': 'project_record', 'project_table_id': 'tblProjects', 'project_record_id': 'recA'},
             'tables': {'projects': {'id': 'tblProjects', 'fields': {
                 'title': {'id': 'fldTitle', 'type': 'text'},
                 'number': {'id': 'fldNumber', 'type': 'auto_number'}}}},
             'documents': {'requirements_and_bugs': {'token': 'DocFixture', 'anchor': 'sectionA',
                 'url': 'https://example.feishu.cn/docx/DocFixture#sectionA', 'project_record_id': 'recA'}}}
    for role in ('feedback', 'requirements', 'bugs', 'work', 'verification', 'delivery', 'versions'):
        value['tables'][role] = {'id': 'tbl' + role.title(), 'fields': {
            'title': {'id': 'fldTitle', 'type': 'text'},
            'project': {'id': 'fldProject', 'type': 'link', 'target_table_id': 'tblProjects'}}}
    value['tables']['feedback']['fields'].update(
        project_input={'id': 'fldInput', 'type': 'select', 'multiple': False,
                       'options': [{'name': 'obsolete binding option'}]},
        status={'id': 'fldStatus', 'type': 'select', 'multiple': False})
    return value


def reply(identifier='r1', text='请补充说明', *, table='tblWork', record='recWork', update=10):
    return {'reply_id': identifier, 'user_id': 'ouFixture', 'create_time': 1, 'update_time': update,
            'content': {'elements': [{'type': 'text_run', 'text_run': {'text': text}}]},
            'extra': {'notify_extra': {'table': table, 'record': record}}}


class FakeCli:
    profile = 'fixture-profile'

    def __init__(self):
        self.config = config()
        self.predicates = []
        self.data = {'tblProjects': [{'record_id': 'recA', 'values': {
            'fldTitle': 'Example', 'fldNumber': 'PRJ-0001'}}]}
        for role, table in self.config['tables'].items():
            if role == 'projects':
                continue
            values = {'fldTitle': role, 'fldProject': [{'id': 'recA'}]}
            if role == 'feedback':
                values.update(fldInput=['PRJ-0001 · Old name'], fldStatus=['待受理'])
            self.data[table['id']] = [{'record_id': 'rec' + role.title(), 'values': values}]
        self.intake = [{'record_id': 'recNew', 'values': {
            'fldTitle': '新反馈', 'fldProject': [], 'fldInput': ['PRJ-0001 · Current name'], 'fldStatus': ['待补充']}}]
        self.comments = {'bitable': [{'comment_id': 'c1', 'is_solved': False, 'update_time': 11,
                                      'reply_list': {'replies': [reply()]}}], 'docx': []}
        self.replies = {'c1': [reply()]}
        self.extra_record_links = {'recMoved': [{'id': 'recB'}]}
        self.fail_comments = False

    def fields(self, token, table_id):
        table = next(t for t in self.config['tables'].values() if t['id'] == table_id)
        fields = copy.deepcopy(list(table['fields'].values()))
        for field in fields:
            if field['type'] == 'link':
                field['link_table'] = {'id': field['target_table_id']}
            if field['id'] == 'fldInput':
                field['options'] = [{'name': name} for name in (
                    '待确认项目', 'PRJ-0001 · Old name', 'PRJ-0001 · Current name', 'PRJ-00010 · Other')]
        return fields

    def records(self, token, table, fields, predicate):
        self.predicates.append((table, predicate))
        intake = any(c[1] == 'empty' for c in predicate['conditions'])
        return copy.deepcopy(self.intake if intake else self.data[table])

    def read_record(self, token, table, record, fields):
        if record in self.extra_record_links:
            values = {'fldProject': self.extra_record_links[record]}
        else:
            row = next(r for r in self.data[table] if r['record_id'] == record)
            values = row['values']
        return {'record_id': record, 'values': {f['id']: copy.deepcopy(values[f['id']]) for f in fields}}

    def read(self, domain, command, *args):
        if self.fail_comments:
            raise m.OnboardingError('fixture permission failure')
        url = args[args.index('--url') + 1]
        kind, token = ('docx', 'DocFixture') if '/docx/' in url else ('bitable', 'BaseFixture')
        if command == '+list-comments':
            assert args[args.index('--solved-status') + 1] == 'all'
            items = self.comments[kind]
            extra = {}
        else:
            cid = args[args.index('--comment-id') + 1]
            items, extra = self.replies[cid], {'comment_id': cid}
        return {'file_token': token, 'file_type': kind, 'items': copy.deepcopy(items),
                'count': len(items), 'has_more': False, **extra}


class RecordRunner:
    """Produce the actual manifest/artifact protocol without running the CLI."""
    def __init__(self, pages=None, mutate=None):
        self.pages = pages or {0: ([('rec1', 'first')], False, None)}
        self.calls = []
        self.mutate = mutate

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        offset = int(argv[argv.index('--offset') + 1])
        values, more, following = self.pages[offset]
        output = Path(kwargs['cwd']) / argv[argv.index('--output') + 1]
        rows = [{'record_id': rid, 'Renamed title': title} for rid, title in values]
        output.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        manifest = {'manifest_version': 'v1', 'format': 'ndjson', 'base_token': 'BaseFixture',
                    'table_id': 'tblWork', 'rev': 12, 'records_count': len(rows),
                    'page_count': 1, 'has_more': more, 'next_offset': following,
                    'record_file': str(output), 'record_file_size_bytes': output.stat().st_size,
                    'columns': {'Renamed title': {'field_id': 'fldTitle', 'field_type': 'text'}},
                    'query_context': {'record_scope': 'filtered_records', 'filter': 'fixed-predicate'}}
        if self.mutate:
            self.mutate(manifest, output, offset)
        return subprocess.CompletedProcess(argv, 0, json.dumps(manifest), '')


class ManifestTests(unittest.TestCase):
    def read(self, runner):
        return m.Cli('fixture-profile', runner).records('BaseFixture', 'tblWork',
            [{'id': 'fldTitle', 'type': 'text'}], {'logic': 'and', 'conditions': [['fldProject', 'non_empty']]})

    def test_follows_returned_offset_and_projects_by_stable_field_id(self):
        runner = RecordRunner({0: ([('rec1', 'first')], True, 37), 37: ([('rec2', 'second')], False, None)})
        rows = self.read(runner)
        self.assertEqual([r['record_id'] for r in rows], ['rec1', 'rec2'])
        self.assertEqual(rows[1]['values'], {'fldTitle': 'second'})
        for argv in runner.calls:
            self.assertEqual(argv[argv.index('--as') + 1], 'user')
            self.assertEqual(argv[argv.index('--profile') + 1], 'fixture-profile')
            self.assertNotIn('--view-id', argv)
        self.assertEqual(runner.calls[1][runner.calls[1].index('--offset') + 1], '37')

    def test_exact_lark_executable_is_used_for_records_and_shared_readers(self):
        executable = '/private/example node/lib/lark/cli.js'
        runner = RecordRunner()
        cli = m.Cli('fixture-profile', runner, executable=executable)
        cli.records('BaseFixture', 'tblWork', [{'id': 'fldTitle', 'type': 'text'}],
                    {'logic': 'and', 'conditions': [['fldProject', 'non_empty']]})
        self.assertEqual(runner.calls[0][0], executable)
        shared_calls = []
        def shared_runner(argv, **kwargs):
            shared_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'data': {}}), '')
        cli = m.Cli('fixture-profile', shared_runner, executable=executable)
        cli.read('drive', '+list-comments', '--url', 'https://example.invalid/base/Fixture')
        cli.base.read('+field-list', '--base-token', 'BaseFixture', '--table-id', 'tblWork')
        self.assertEqual([argv[0] for argv in shared_calls], [executable, executable])
        for value in ('', 'relative/cli.js', None):
            with self.subTest(value=value), self.assertRaises(m.WatchError):
                m.Cli('fixture-profile', executable=value)

    def test_rejects_partial_or_foreign_manifests(self):
        mutations = {
            'source': lambda d, p, o: d.update(table_id='tblOther'),
            'scope': lambda d, p, o: d['query_context'].update(record_scope='all_records'),
            'identity': lambda d, p, o: d.update(identity='bot'),
            'count': lambda d, p, o: d.update(records_count=2),
            'bool count': lambda d, p, o: d.update(records_count=True),
            'more': lambda d, p, o: d.update(has_more='false'),
            'revision': lambda d, p, o: d.pop('rev'),
            'path': lambda d, p, o: d.update(record_file=str(p.parent / 'unrequested.ndjson')),
            'size': lambda d, p, o: d.update(record_file_size_bytes=999),
            'columns': lambda d, p, o: d.update(columns={}),
            'duplicate column': lambda d, p, o: d['columns'].update(another={'field_id': 'fldTitle'}),
            'field type': lambda d, p, o: d['columns']['Renamed title'].update(field_type='number'),
            'bad next offset': lambda d, p, o: d.update(has_more=True, next_offset=0),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), self.assertRaises(m.OnboardingError):
                self.read(RecordRunner(mutate=mutate))

    def test_rejects_changed_revision_query_and_duplicate_id_between_pages(self):
        pages = {0: ([('rec1', 'first')], True, 4), 4: ([('rec2', 'second')], False, None)}
        for key in ('rev', 'query'):
            def mutate(manifest, output, offset):
                if offset:
                    if key == 'rev':
                        manifest['rev'] += 1
                    else:
                        manifest['query_context']['filter'] = 'different'
            with self.subTest(key=key), self.assertRaisesRegex(m.WatchError, 'changed between pages'):
                self.read(RecordRunner(pages, mutate))
        with self.assertRaisesRegex(m.WatchError, 'duplicate record'):
            self.read(RecordRunner({0: ([('rec1', 'first')], True, 4), 4: ([('rec1', 'again')], False, None)}))

    def test_rejects_mid_scan_failure_and_empty_nonfinal_page(self):
        runner = RecordRunner({0: ([('rec1', 'first')], True, 4)})
        def fail_second(argv, **kwargs):
            if argv[argv.index('--offset') + 1] == '4':
                return subprocess.CompletedProcess(argv, 1, '', json.dumps({
                    'ok': False, 'error': {'message': 'access denied'}}))
            return runner(argv, **kwargs)
        with self.assertRaisesRegex(m.OnboardingError, 'access denied'):
            self.read(fail_second)
        with self.assertRaises(m.WatchError):
            self.read(RecordRunner({0: ([], True, 4)}))

    def test_empty_final_page_is_a_complete_snapshot(self):
        self.assertEqual(self.read(RecordRunner({0: ([], False, None)})), [])

    def test_same_table_changing_between_different_queries_fails(self):
        runner = RecordRunner()
        cli = m.Cli('fixture-profile', runner)
        args = ('BaseFixture', 'tblWork', [{'id': 'fldTitle', 'type': 'text'}], {'conditions': []})
        cli.records(*args)
        runner.mutate = lambda d, p, o: d.update(rev=13)
        with self.assertRaisesRegex(m.WatchError, 'changed between queries'):
            cli.records(*args)

    def test_deadline_bounds_all_shared_cli_subprocesses(self):
        called = []
        def runner(argv, **kwargs):
            called.append(kwargs['timeout'])
            return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'data': {}}), '')
        with mock.patch.object(m.time, 'monotonic', side_effect=[100, 105, 119, 121]):
            cli = m.Cli('fixture-profile', runner, scan_timeout=20)
            cli.base.read('+field-list')
            cli.read('drive', '+list-comments')
            with self.assertRaisesRegex(m.WatchError, 'time limit'):
                cli.base.read('+field-list')
        self.assertEqual(called, [15, 1])


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / 'comments.json'
        self.cli = FakeCli()
        self.config = config()

    def collect(self):
        with mock.patch.object(m, 'Cli', return_value=self.cli):
            return m.collect(self.config, 'fixture-profile', comments_state_path=self.state)

    def linked_only_without_project_input(self):
        self.config['documents'] = {}
        self.config['feedback_intake'] = {'mode': 'linked_only'}
        self.config['tables']['feedback']['fields'].pop('project_input')
        self.cli.config['tables']['feedback']['fields'].pop('project_input')
        for row in self.cli.data['tblFeedback']:
            row['values'].pop('fldInput')

    def test_linked_only_reads_existing_records_and_comments_without_project_input(self):
        self.linked_only_without_project_input()
        original = copy.deepcopy(self.config)
        first, second = self.collect(), self.collect()
        self.assertEqual(first, second)
        self.assertEqual(self.config, original)
        self.assertEqual(len(first['records']), 8)
        self.assertIn('tblFeedback/recFeedback', first['records'])
        self.assertNotIn('tblFeedback/recNew', first['records'])
        self.assertEqual(len(first['comments']), 1)
        self.assertEqual(next(iter(first['comments'].values()))['content']['text'], '请补充说明')
        coverage = first['coverage']['feedback']
        self.assertEqual(coverage['mode'], 'linked_only')
        self.assertEqual(coverage['unassigned_feedback'], 'excluded')
        self.assertTrue(coverage['message'])
        self.assertTrue(self.cli.predicates)
        self.assertTrue(all(predicate['conditions'] == [
            ['fldProject', 'intersects', [{'id': 'recA'}]]]
            for _, predicate in self.cli.predicates))
        self.assertEqual(json.loads(self.state.read_text())['acknowledgements'], {})

    def test_linked_only_skips_unassigned_intake_even_when_project_input_exists(self):
        self.config['feedback_intake'] = {'mode': 'linked_only'}
        snapshot = self.collect()
        self.assertIn('tblFeedback/recFeedback', snapshot['records'])
        self.assertNotIn('tblFeedback/recNew', snapshot['records'])
        self.assertFalse(any(condition[1] == 'empty'
            for _, predicate in self.cli.predicates for condition in predicate['conditions']))
        self.assertEqual(snapshot['coverage']['feedback']['unassigned_feedback'], 'excluded')

    def test_linked_only_can_use_verified_record_id_without_project_number(self):
        self.linked_only_without_project_input()
        self.assertEqual(self.config['documents'], {})
        del self.config['project_number']
        del self.config['tables']['projects']['fields']['number']
        snapshot = self.collect()
        self.assertIn('tblProjects/recA', snapshot['records'])
        self.assertIn('tblFeedback/recFeedback', snapshot['records'])
        self.assertEqual(len(snapshot['comments']), 1)
        self.assertEqual(snapshot['coverage']['feedback']['mode'], 'linked_only')

    def test_linked_only_still_rejects_changed_known_project_number(self):
        self.linked_only_without_project_input()
        self.collect()
        before = self.state.read_bytes()
        self.cli.data['tblProjects'][0]['values']['fldNumber'] = 'PRJ-0002'
        with self.assertRaisesRegex(m.WatchError, 'project number'):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_linked_only_with_docx_requires_project_number_and_preserves_observation(self):
        self.config['feedback_intake'] = {'mode': 'linked_only'}
        self.assertTrue(self.config['documents'])
        self.collect()
        before = self.state.read_bytes()
        del self.config['project_number']
        for number in ('missing', None, '', '   '):
            with self.subTest(number=number):
                if number != 'missing':
                    self.config['project_number'] = number
                with mock.patch.object(m, 'Cli') as constructor:
                    with self.assertRaisesRegex(m.WatchError, 'Docx.*project_number'):
                        m.collect(self.config, 'fixture-profile', comments_state_path=self.state)
                constructor.assert_not_called()
                self.assertEqual(self.state.read_bytes(), before)

    def test_docx_project_number_is_verified_without_a_feedback_table(self):
        self.config['feedback_intake'] = {'mode': 'linked_only'}
        del self.config['tables']['feedback']
        self.assertTrue(self.config['documents'])
        self.collect()
        before = self.state.read_bytes()
        self.cli.data['tblProjects'][0]['values']['fldNumber'] = 'PRJ-0002'
        with self.assertRaisesRegex(m.WatchError, 'project number'):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_declared_project_number_is_verified_without_feedback_or_documents(self):
        self.config['feedback_intake'] = {'mode': 'linked_only'}
        self.config['documents'] = {}
        del self.config['tables']['feedback']
        self.assertEqual(self.config['project_number'], 'PRJ-0001')
        self.collect()
        before = self.state.read_bytes()
        self.cli.data['tblProjects'][0]['values']['fldNumber'] = 'PRJ-0002'
        with self.assertRaisesRegex(m.WatchError, 'project number'):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_missing_project_input_never_silently_downgrades_default_mode(self):
        self.collect()
        before = self.state.read_bytes()
        self.config['tables']['feedback']['fields'].pop('project_input')
        self.cli.config['tables']['feedback']['fields'].pop('project_input')
        for row in self.cli.data['tblFeedback']:
            row['values'].pop('fldInput')
        for setting in (None, {'mode': 'project_input'}):
            with self.subTest(setting=setting):
                if setting is not None:
                    self.config['feedback_intake'] = setting
                with mock.patch.object(self.cli, 'read', wraps=self.cli.read) as comments_reader:
                    with self.assertRaisesRegex(m.WatchError, 'project_input'):
                        self.collect()
                comments_reader.assert_not_called()
                self.assertEqual(self.state.read_bytes(), before)

    def test_invalid_feedback_intake_mode_fails_before_any_remote_read(self):
        for value in (None, {}, [], 'linked_only', {'mode': 'all'}, {'mode': None}):
            with self.subTest(value=value):
                self.config['feedback_intake'] = value
                with mock.patch.object(m, 'Cli') as constructor:
                    with self.assertRaisesRegex(m.WatchError, 'feedback_intake.mode'):
                        m.collect(self.config, 'fixture-profile', comments_state_path=self.state)
                constructor.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_linked_only_rejects_foreign_unassigned_or_ambiguous_records(self):
        self.linked_only_without_project_input()
        self.collect()
        before = self.state.read_bytes()
        for table in ('tblFeedback', 'tblWork'):
            original = copy.deepcopy(self.cli.data[table][0]['values']['fldProject'])
            for links in ([{'id': 'recB'}], [], [{'id': 'recA'}, {'id': 'recB'}]):
                with self.subTest(table=table, links=links):
                    self.cli.data[table][0]['values']['fldProject'] = links
                    with self.assertRaisesRegex(m.WatchError, 'outside this project|ambiguous ownership'):
                        self.collect()
                    self.assertEqual(self.state.read_bytes(), before)
            self.cli.data[table][0]['values']['fldProject'] = original

    def test_linked_only_rejects_project_link_drift_and_keeps_previous_observation(self):
        self.linked_only_without_project_input()
        self.collect()
        before = self.state.read_bytes()
        self.cli.config['tables']['feedback']['fields']['project']['target_table_id'] = 'tblOtherProjects'
        with self.assertRaisesRegex(m.WatchError, 'project link'):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_linked_only_comment_failure_keeps_previous_observation(self):
        self.linked_only_without_project_input()
        self.collect()
        before = self.state.read_bytes()
        self.cli.replies['c1'][0] = reply(text='新的答复尚未采集完整', update=30)
        self.cli.fail_comments = True
        with self.assertRaisesRegex(m.WatchError, 'Comment scan failed'):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_linked_only_does_not_include_foreign_or_unassigned_comment_content(self):
        self.linked_only_without_project_input()
        for record, links in (('recMoved', [{'id': 'recB'}]), ('recUnassigned', [])):
            with self.subTest(record=record):
                self.cli.extra_record_links[record] = links
                comment = reply(record=record, text='unverified private content')
                self.cli.comments['bitable'][0]['reply_list']['replies'] = [comment]
                self.cli.replies['c1'] = [comment]
                snapshot = self.collect()
                self.assertEqual(snapshot['comments'], {})
                self.assertNotIn('unverified private content', json.dumps(snapshot))

    def test_collect_passes_the_configured_lark_executable(self):
        with mock.patch.object(m, 'Cli', return_value=self.cli) as constructor:
            m.collect(self.config, 'fixture-profile', comments_state_path=self.state,
                      lark_bin='/private/example node/lib/lark/cli.js')
        constructor.assert_called_once_with('fixture-profile', executable='/private/example node/lib/lark/cli.js')

    def test_base_only_collect_returns_project_records_and_comments(self):
        self.config['documents'] = {}
        with mock.patch.object(self.cli, 'read', wraps=self.cli.read) as reader:
            first, second = self.collect(), self.collect()
        self.assertEqual(first, second)
        self.assertEqual(len(first['records']), 9)
        self.assertEqual(first['records']['tblWork/recWork']['fields']['fldProject'], [{'id': 'recA'}])
        self.assertEqual(first['binding']['documents'], [])
        self.assertEqual(len(first['comments']), 1)
        observed = next(iter(first['comments'].values()))
        self.assertEqual(observed['content']['text'], '请补充说明')
        self.assertEqual(observed['location']['project_record_id'], 'recA')
        self.assertEqual(first['notices'], {})
        self.assertTrue(reader.call_args_list)
        self.assertTrue(all('/base/BaseFixture' in call.args[call.args.index('--url') + 1]
                            for call in reader.call_args_list))
        journal = json.loads(self.state.read_text())
        self.assertEqual(journal['last_complete_scan'], journal['last_full_reconciliation'])
        before = self.state.read_bytes()
        self.cli.config['tables']['work']['fields']['project']['target_table_id'] = 'tblOtherProjects'
        with self.assertRaisesRegex(m.WatchError, 'project link'):
            self.collect()
        self.assertEqual(self.state.read_bytes(), before)

    def test_complete_stable_snapshot_and_narrow_feedback_intake(self):
        first, second = self.collect(), self.collect()
        self.assertEqual(first, second)
        self.assertEqual(len(first['records']), 9)
        self.assertEqual(first['records']['tblFeedback/recNew']['fields']['fldTitle'], '新反馈')
        self.assertEqual(first['binding'], m.comments.binding_identity(self.config))
        self.assertEqual(len(first['comments']), 1)
        observed = next(iter(first['comments'].values()))
        self.assertEqual(observed['content']['text'], '请补充说明')
        self.assertEqual(observed['content']['author'], 'ouFixture')
        self.assertEqual(observed['content']['body'], self.cli.replies['c1'][0]['content'])
        self.assertEqual(observed['location']['record_id'], 'recWork')
        self.assertEqual(first['notices'], {})
        predicate = next(p for t, p in self.cli.predicates if any(c[1] == 'empty' for c in p['conditions']))
        self.assertEqual(predicate['conditions'], [['fldProject', 'empty'],
            ['fldInput', 'intersects', ['PRJ-0001 · Current name', 'PRJ-0001 · Old name']],
            ['fldStatus', 'intersects', ['待受理', '待补充']]])
        self.assertEqual(json.loads(self.state.read_text())['acknowledgements'], {})
        self.assertEqual(first['coverage']['feedback']['mode'], 'project_input')
        self.assertEqual(first['coverage']['feedback']['unassigned_feedback'], 'matching_project_input_only')

    def test_content_edits_resolve_reopen_and_record_deletions_are_changes(self):
        first = self.collect()
        self.cli.replies['c1'][0] = reply(text='这是补充', update=12)
        edited = self.collect()
        self.assertNotEqual(first['comments'], edited['comments'])
        self.cli.comments['bitable'][0]['is_solved'] = True
        solved = self.collect()
        self.assertNotEqual(edited['comments'], solved['comments'])
        self.cli.comments['bitable'][0]['is_solved'] = False
        reopened = self.collect()
        self.assertEqual(edited['comments'], reopened['comments'])
        self.cli.data['tblBugs'] = []
        self.assertNotEqual(reopened['records'], self.collect()['records'])

    def test_thread_timestamp_alone_is_not_a_change(self):
        before = self.collect()
        self.cli.comments['bitable'][0]['update_time'] = 999
        self.assertEqual(before, self.collect())

    def test_out_of_scope_is_excluded_unresolved_is_only_notice(self):
        self.cli.comments['bitable'][0]['reply_list']['replies'] = [reply(record='recMoved')]
        self.cli.replies['c1'] = [reply(record='recMoved', text='private other project text')]
        foreign = self.collect()
        self.assertFalse(foreign['comments'])
        self.assertFalse(foreign['notices'])
        self.assertNotIn('private other', json.dumps(foreign))
        self.cli.replies['c1'][0]['extra'] = {}
        self.cli.comments['bitable'][0]['reply_list']['replies'] = self.cli.replies['c1']
        uncertain = self.collect()
        self.assertFalse(uncertain['comments'])
        self.assertEqual(next(iter(uncertain['notices'].values()))['status'], 'unresolved')
        self.assertNotIn('private other', json.dumps(uncertain))

    def test_moved_or_missing_comments_leave_stable_notice_without_foreign_content(self):
        self.collect()
        self.cli.data['tblWork'] = []
        self.cli.extra_record_links['recWork'] = [{'id': 'recB'}]
        moved = self.collect()
        self.assertFalse(moved['comments'])
        self.assertEqual(next(iter(moved['notices'].values()))['status'], 'moved_out_of_scope')
        self.cli.replies['c1'][0] = reply(text='private project content', update=44)
        self.assertEqual(moved, self.collect())
        self.cli.comments['bitable'] = []
        missing = self.collect()
        self.assertEqual(next(iter(missing['notices'].values()))['status'], 'not_returned')
        self.assertEqual(missing, self.collect())

    def test_failure_leaves_previous_comment_journal_byte_for_byte(self):
        self.collect()
        before = self.state.read_bytes()
        self.cli.replies['c1'][0] = reply(text='未提交的观察', update=30)
        self.cli.fail_comments = True
        with self.assertRaisesRegex(m.WatchError, 'Comment scan failed'):
            self.collect()
        self.assertEqual(before, self.state.read_bytes())
        self.cli.fail_comments = False
        self.cli.data['tblWork'][0]['values']['fldProject'] = [{'id': 'recB'}]
        with self.assertRaisesRegex(m.WatchError, 'outside this project'):
            self.collect()
        self.assertEqual(before, self.state.read_bytes())

    def test_rejects_bad_scope_project_number_and_live_link_target(self):
        for mode in ('single_project', 'base', None):
            self.config['scope']['mode'] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(m.WatchError, 'project_record'):
                self.collect()
        self.config = config()
        self.cli.data['tblProjects'][0]['values']['fldNumber'] = 'PRJ-0002'
        with self.assertRaisesRegex(m.WatchError, 'project number'):
            self.collect()
        self.assertFalse(self.state.exists())
        self.cli.data['tblProjects'][0]['values']['fldNumber'] = 'PRJ-0001'
        self.cli.config['tables']['work']['fields']['project']['target_table_id'] = 'tblOtherProjects'
        with self.assertRaisesRegex(m.WatchError, 'project link'):
            self.collect()

    def test_rejects_intake_misrouting_and_ambiguous_ownership(self):
        bad_values = [dict(fldProject=[{'id': 'recB'}]), dict(fldInput=['PRJ-00010 · Other']),
                      dict(fldStatus=['已答复']), dict(fldInput=['PRJ-0001 · Old name', 'PRJ-00010 · Other'])]
        for replacement in bad_values:
            self.cli = FakeCli()
            self.cli.intake[0]['values'].update(replacement)
            with self.subTest(replacement=replacement), self.assertRaisesRegex(m.WatchError, 'narrow scope'):
                self.collect()
            self.assertFalse(self.state.exists())
        self.cli = FakeCli()
        self.cli.data['tblWork'][0]['values']['fldProject'].append({'id': 'recB'})
        with self.assertRaisesRegex(m.WatchError, 'ambiguous ownership'):
            self.collect()

    def test_existing_acknowledgements_are_preserved(self):
        self.collect()
        state = json.loads(self.state.read_text())
        state['acknowledgements'] = {'existing-event': [{'revision': 'existing'}]}
        self.state.write_text(json.dumps(state))
        self.collect()
        self.assertEqual(json.loads(self.state.read_text())['acknowledgements'], state['acknowledgements'])

    def test_malformed_input_and_local_io_fail_as_onboarding_errors(self):
        self.state.write_text('not json')
        with self.assertRaises(m.OnboardingError):
            self.collect()
        self.assertEqual(self.state.read_text(), 'not json')
        self.state.unlink()
        with mock.patch.object(m, 'atomic_json', side_effect=OSError('fixture disk full')):
            with self.assertRaisesRegex(m.WatchError, 'OSError'):
                self.collect()
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
