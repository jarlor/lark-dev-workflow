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
spec = importlib.util.spec_from_file_location('comments', SCRIPTS / 'comments.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def config(project='A'):
    return {'format_version': 2, 'project_key': 'demo-' + project.lower(),
            'project_number': 'PRJ-0001' if project == 'A' else 'PRJ-0002',
            'base': {'token': 'BaseFixture', 'url': 'https://example.feishu.cn/base/BaseFixture'},
            'scope': {'mode': 'project_record', 'project_table_id': 'tblProjects', 'project_record_id': 'rec' + project},
            'tables': {'work': {'id': 'tblWork', 'fields': {
                'project': {'id': 'fldProject', 'type': 'link', 'target_table_id': 'tblProjects'}}},
                       'projects': {'id': 'tblProjects', 'fields': {'title': {'id': 'fldTitle', 'type': 'text'}}}},
            'documents': {'requirements_and_bugs': {'token': 'DocFixture', 'anchor': 'section' + project,
                'url': 'https://example.feishu.cn/docx/DocFixture#section' + project,
                'project_record_id': 'rec' + project}}}


def reply(identifier='replyRoot', text='请补充说明', *, update=10, table=None, record=None, author='ouAuthor'):
    value = {'reply_id': identifier, 'user_id': author, 'create_time': 1, 'update_time': update,
             'content': {'elements': [{'type': 'text_run', 'text_run': {'text': text}, 'person': None, 'docs_link': None}]},
             'extra': {'image_list': None}}
    if table or record:
        value['extra']['notify_extra'] = {'table': table, 'record': record, 'view': 'vewFixture'}
    return value


def comment(identifier='commentA', block='childA', *, solved=False):
    value = {'comment_id': identifier, 'is_solved': solved, 'is_whole': block is None, 'update_time': 11,
             'reply_list': {'replies': [reply()]}}
    if block:
        value.update(relation={'content_deleted': False, 'relation': json.dumps({
            '22-DocFixture': {'objType': 22, 'positionInfo': {'blockID': block}}})},
                     extra={'content_anchor_id': block})
    return value


def page(kind, token, items, more=False, cursor='final-cursor-is-not-pagination', comment_id=None):
    result = {'file_type': kind, 'file_token': token, 'items': copy.deepcopy(items),
              'count': len(items), 'has_more': more, 'page_token': cursor}
    if comment_id:
        result['comment_id'] = comment_id
    return result


class FakeCli:
    profile = 'fixture-profile'
    def __init__(self, comments=None, replies=None):
        self.comments = comments if comments is not None else {'bitable': [], 'docx': [comment()]}
        self.replies = replies or {'commentA': [reply()]}
        self.calls, self.record_calls = [], []
        self.overrides = {}
        self.links = {'recWorkA': [{'id': 'recA'}], 'recWorkB': [{'id': 'recB'}]}
        self.content = ('<title>共享文档</title><h1 id="sectionA">PRJ-0001 · 同名项目</h1>'
                        '<p id="childA">A正文</p><h2 id="subA">修订历史</h2><p id="deepA">旧版</p>'
                        '<h1 id="sectionB">PRJ-0002 · 同名项目</h1><p id="childB">B正文</p>')

    def read(self, domain, command, *args):
        self.calls.append((domain, command, args))
        if domain == 'docs':
            return {'document': {'document_id': 'DocFixture', 'revision_id': 7, 'content': self.content}}
        url = args[args.index('--url') + 1]
        kind, token = ('docx', 'DocFixture') if '/docx/' in url else ('bitable', 'BaseFixture')
        cursor = args[args.index('--page-token') + 1] if '--page-token' in args else None
        cid = args[args.index('--comment-id') + 1] if '--comment-id' in args else None
        key = (command, kind, cid, cursor)
        if key in self.overrides:
            result = self.overrides[key]
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        if command == '+list-comments':
            include = args[args.index('--solved-status') + 1] == 'all'
            values = [c for c in self.comments[kind] if include or not c['is_solved']]
            return page(kind, token, values)
        return page(kind, token, self.replies[cid], comment_id=cid)

    def read_record(self, token, table, record, fields):
        self.record_calls.append((token, table, record, fields))
        return {'record_id': record, 'values': {fields[0]['id']: copy.deepcopy(self.links[record])}}


def scanned(cli=None, project='A', include=True):
    c = config(project)
    return m.scan(c, m.empty_state(c), cli or FakeCli(), include_resolved=include)


def ack_payload(observation):
    return {'event_key': observation['event_key'], 'revision': observation['revision'],
            'observation_id': observation['observation_id'],
            'disposition': '已更新说明并读回', 'target': 'https://example.feishu.cn/docx/DocFixture#childA',
            'evidence': [{'reference': 'local-evidence/readback.json', 'readback': '已读回目标说明和版本',
                          'observed_at': '2026-10-26T10:00:00Z'}], 'handler': 'ouAgentUser'}


class PaginationTests(unittest.TestCase):
    def test_outer_and_nested_reply_pagination_keeps_only_first_root(self):
        cli = FakeCli()
        first, second = comment('commentA'), comment('commentSecond', 'deepA')
        first['has_more'] = True
        cli.overrides[('+list-comments', 'docx', None, None)] = page('docx', 'DocFixture', [first], True, 'outer2')
        cli.overrides[('+list-comments', 'docx', None, 'outer2')] = page('docx', 'DocFixture', [second])
        cli.overrides[('+list-replies', 'docx', 'commentA', None)] = page('docx', 'DocFixture', [reply()], True, 'reply2', 'commentA')
        cli.overrides[('+list-replies', 'docx', 'commentA', 'reply2')] = page('docx', 'DocFixture', [reply('replyLater')], comment_id='commentA')
        cli.replies['commentSecond'] = [reply('secondRoot')]
        state, report = scanned(cli)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(state['events']), 3)
        roots = [p for p in m.pending(state) if p['root_reply']]
        self.assertEqual({r['reply_id'] for r in roots}, {'replyRoot', 'secondRoot'})

    def test_missing_nested_has_more_still_fetches_replies(self):
        cli = FakeCli(replies={'commentA': [reply(), reply('hiddenReply')]})
        state, _ = scanned(cli)
        self.assertEqual(len(state['events']), 2)
        self.assertEqual(sum(call[1] == '+list-replies' for call in cli.calls), 1)

    def test_final_nonempty_page_token_is_ignored(self):
        cli = FakeCli()
        state, report = scanned(cli)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(state['events']), 1)
        self.assertFalse(any('--page-token' in call[2] for call in cli.calls))

    def test_incomplete_duplicate_and_cyclic_pagination_fail_closed(self):
        for broken in ['no_cursor', 'duplicate', 'cycle', 'wrong_resource', 'missing_has_more']:
            with self.subTest(broken=broken):
                cli = FakeCli()
                initial = page('docx', 'DocFixture', [comment()], True, 'next')
                if broken == 'no_cursor':
                    initial['page_token'] = ''
                elif broken == 'wrong_resource':
                    initial['file_token'] = 'DifferentDoc'
                elif broken == 'missing_has_more':
                    initial.pop('has_more')
                cli.overrides[('+list-comments', 'docx', None, None)] = initial
                cli.overrides[('+list-comments', 'docx', None, 'next')] = page(
                    'docx', 'DocFixture', [comment('comment2') if broken == 'cycle' else comment()], True, 'next')
                state, report = scanned(cli)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(state['events'], {})
                self.assertIsNone(state['last_complete_scan'])

    def test_failure_preserves_all_prior_observations_and_acknowledgements(self):
        initial, _ = scanned()
        initial, _ = m.acknowledgement(initial, ack_payload(m.pending(initial)[0]))
        cli = FakeCli()
        cli.overrides[('+list-replies', 'docx', 'commentA', None)] = m.CommentError('second page denied')
        failed, report = m.scan(config(), initial, cli, include_resolved=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(failed['events'], initial['events'])
        self.assertEqual(failed['acknowledgements'], initial['acknowledgements'])
        with self.assertRaises(m.CommentError):
            m.acknowledgement(failed, {'event_key': 'unused'})


class LocationTests(unittest.TestCase):
    def test_shared_doc_same_name_uses_real_project_section(self):
        comments = {'bitable': [], 'docx': [comment('a', 'deepA'), comment('b', 'childB'), comment('unknown', None)]}
        replies = {cid: [reply('reply-' + cid)] for cid in ('a', 'b', 'unknown')}
        a, _ = scanned(FakeCli(comments, replies), 'A')
        b, _ = scanned(FakeCli(comments, replies), 'B')
        self.assertEqual({x['comment_id'] for x in m.pending(a)}, {'a', 'unknown'})
        self.assertEqual({x['comment_id'] for x in m.pending(b)}, {'b', 'unknown'})
        self.assertEqual(next(x for x in m.pending(a) if x['comment_id'] == 'unknown')['location']['status'], 'unresolved')

    def test_base_anchor_is_verified_against_actual_project_relation(self):
        comments = {'bitable': [comment('baseA', None)], 'docx': []}
        cli = FakeCli(comments, {'baseA': [reply(table='tblWork', record='recWorkA')]})
        a, _ = scanned(cli, 'A')
        self.assertEqual(m.pending(a)[0]['location']['status'], 'in_scope')
        self.assertEqual(cli.record_calls[0][:3], ('BaseFixture', 'tblWork', 'recWorkA'))
        b, _ = scanned(FakeCli(comments, {'baseA': [reply(table='tblWork', record='recWorkA')]}), 'B')
        self.assertEqual(m.pending(b), [])

    def test_conflicting_base_reply_anchors_and_quote_only_remain_unresolved(self):
        for replies in ([reply(table='tblWork', record='recWorkA'), reply('other', table='tblWork', record='recWorkB')], [reply()]):
            cli = FakeCli({'bitable': [dict(comment('baseA', None), quote='第 1 行')], 'docx': []}, {'baseA': replies})
            state, _ = scanned(cli)
            self.assertTrue(all(x['location']['status'] == 'unresolved' for x in m.pending(state)))
            self.assertEqual(cli.record_calls, [])

    def test_empty_or_multiple_project_links_remain_unresolved(self):
        for links in ([], [{'id': 'recA'}, {'id': 'recB'}]):
            cli = FakeCli({'bitable': [comment('baseA', None)], 'docx': []}, {'baseA': [reply(table='tblWork', record='recWorkA')]})
            cli.links['recWorkA'] = links
            state, _ = scanned(cli)
            self.assertEqual(m.pending(state)[0]['location']['status'], 'unresolved')

    def test_docx_deleted_missing_and_conflicting_anchors_are_unresolved(self):
        for mode in ('deleted', 'missing', 'conflict'):
            c = comment()
            if mode == 'deleted':
                c['relation']['content_deleted'] = True
            elif mode == 'missing':
                c = comment(block='notInDocument')
            else:
                c['extra']['content_anchor_id'] = 'other'
            state, _ = scanned(FakeCli({'bitable': [], 'docx': [c]}))
            self.assertEqual(m.pending(state)[0]['location']['status'], 'unresolved')

    def test_known_mapping_needs_evidence_and_live_section_verification(self):
        mapping = {'resource_type': 'docx', 'resource_token': 'DocFixture', 'comment_id': 'commentA', 'block_id': 'childB',
                   'evidence': {'reference': 'review.json', 'verified_by': 'ouUser', 'verified_at': '2026-10-26'}}
        known = m.load_locations({'format_version': 1, 'bindings': [mapping]})
        cli = FakeCli({'bitable': [], 'docx': [comment(block=None)]})
        state, _ = m.scan(config(), m.empty_state(config()), cli, known=known)
        self.assertEqual(m.pending(state), [])
        mapping['evidence'] = {}
        with self.assertRaises(m.CommentError):
            m.load_locations({'format_version': 1, 'bindings': [mapping]})

    def test_known_mapping_cannot_override_relation_conflicts(self):
        for mode in ('extra', 'extra_only', 'multiple', 'malformed'):
            c = comment()
            if mode == 'extra':
                c['extra']['content_anchor_id'] = 'childB'
            elif mode == 'extra_only':
                c.pop('relation')
                c['extra']['content_anchor_id'] = 'childB'
            elif mode == 'multiple':
                c['relation']['relation'] = json.dumps({'a': {'positionInfo': {'blockID': 'childA'}},
                                                       'b': {'positionInfo': {'blockID': 'childB'}}})
            else:
                c['relation']['relation'] = 'not-json'
            mapping = {'resource_type': 'docx', 'resource_token': 'DocFixture', 'comment_id': 'commentA', 'block_id': 'childA',
                       'evidence': {'reference': 'review.json', 'verified_by': 'ouUser', 'verified_at': '2026-10-26'}}
            known = m.load_locations({'format_version': 1, 'bindings': [mapping]})
            state, _ = m.scan(config(), m.empty_state(config()), FakeCli({'bitable': [], 'docx': [c]}), known=known)
            self.assertEqual(m.pending(state)[0]['location']['status'], 'unresolved')

    def test_whole_base_legacy_binding_does_not_guess_project(self):
        c = config()
        c['scope'] = {'mode': 'whole_base'}
        state, _ = m.scan(c, m.empty_state(c), FakeCli())
        self.assertEqual(m.pending(state)[0]['location']['status'], 'unresolved')


class JournalTests(unittest.TestCase):
    def test_thread_timestamp_only_and_new_reply_do_not_reopen_acked_old_reply(self):
        state, _ = scanned()
        old = m.pending(state)[0]
        state, _ = m.acknowledgement(state, ack_payload(old))
        updated_comment = dict(comment(), update_time=99)
        cli = FakeCli({'bitable': [], 'docx': [updated_comment]})
        state, report = m.scan(config(), state, cli, include_resolved=True)
        self.assertEqual(m.pending(state), [])
        self.assertEqual(report['context_changes'], [])
        context = state['events'][old['event_key']]['context_history'][-1]
        self.assertEqual(len(context['readbacks']), 2)
        self.assertEqual(context['current_readback']['thread']['update_time'], 99)
        cli.replies['commentA'].append(reply('newReply'))
        cli.comments['docx'][0]['update_time'] = 100
        state, report = m.scan(config(), state, cli, include_resolved=True)
        self.assertEqual([x['reply_id'] for x in m.pending(state)], ['newReply'])
        self.assertEqual(report['context_changes'], [])

    def test_ownership_move_is_reported_even_when_it_leaves_pending_scope(self):
        state, _ = scanned()
        old = m.pending(state)[0]
        state, _ = m.acknowledgement(state, ack_payload(old))
        moved = FakeCli({'bitable': [], 'docx': [comment(block='childB')]})
        state, report = m.scan(config(), state, moved, include_resolved=True)
        self.assertEqual(m.pending(state), [])
        self.assertEqual(len(report['context_changes']), 1)
        change = report['context_changes'][0]
        self.assertEqual(change['previous']['location_status'], 'in_scope')
        self.assertEqual(change['current']['location_status'], 'out_of_scope')
        self.assertTrue(change['previously_acknowledged'])
        self.assertEqual(change['acknowledged_targets'], [ack_payload(old)['target']])
        self.assertEqual(len(state['events'][old['event_key']]['revisions']), 1)

    def test_solve_then_reopen_requires_new_review_without_new_content_revision(self):
        state, _ = scanned()
        old = m.pending(state)[0]
        state, _ = m.acknowledgement(state, ack_payload(old))
        state, _ = m.scan(config(), state, FakeCli({'bitable': [], 'docx': [comment(solved=True)]}), include_resolved=True)
        solved = m.pending(state)[0]
        self.assertEqual(solved['revision'], old['revision'])
        self.assertNotEqual(solved['observation_id'], old['observation_id'])
        state, _ = m.acknowledgement(state, ack_payload(solved))
        state, _ = m.scan(config(), state, FakeCli(), include_resolved=True)
        reopened = m.pending(state)[0]
        self.assertNotEqual(reopened['observation_id'], old['observation_id'])
        self.assertEqual(len(state['events'][old['event_key']]['revisions']), 1)
        self.assertEqual(len(state['events'][old['event_key']]['context_history']), 3)
        with self.assertRaises(m.CommentError):
            m.acknowledgement(state, ack_payload(old))

    def test_unseen_event_cannot_be_acknowledged_and_is_not_deleted(self):
        state, _ = scanned()
        old = m.pending(state)[0]
        state, _ = m.scan(config(), state, FakeCli({'bitable': [], 'docx': []}), include_resolved=True)
        self.assertEqual(len(m.pending(state)), 1)
        with self.assertRaises(m.CommentError):
            m.acknowledgement(state, ack_payload(old))

    def test_early_format_one_state_is_upgraded_without_losing_history(self):
        state, _ = scanned()
        original = copy.deepcopy(state)
        for event in state['events'].values():
            event.pop('context_history')
            event.pop('latest_observation_id')
        upgraded = m.upgrade_state(state)
        self.assertEqual(upgraded['acknowledgements'], original['acknowledgements'])
        self.assertEqual(upgraded['events'].keys(), original['events'].keys())
        self.assertTrue(m.pending(upgraded)[0]['observation_id'])

    def test_same_revision_is_idempotent_and_ack_does_not_claim_remote_proof(self):
        state, _ = scanned()
        payload = ack_payload(m.pending(state)[0])
        acked, result = m.acknowledgement(state, payload)
        self.assertFalse(result['remote_proof_verified'])
        repeated, result = m.acknowledgement(acked, payload)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(repeated, acked)
        rescanned, report = m.scan(config(), acked, FakeCli(), include_resolved=True)
        self.assertEqual(report['new_events'], 0)
        self.assertEqual(report['new_revisions'], 0)
        self.assertEqual(m.pending(rescanned), [])
        repeated, result = m.acknowledgement(rescanned, payload)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(repeated, rescanned)

    def test_edit_retains_history_and_new_reply_becomes_pending(self):
        state, _ = scanned()
        old = m.pending(state)[0]
        acked, _ = m.acknowledgement(state, ack_payload(old))
        cli = FakeCli(replies={'commentA': [reply(text='修改后的正文', update=20), reply('newReply')]})
        changed, report = m.scan(config(), acked, cli, include_resolved=True)
        self.assertEqual(report['new_events'], 1)
        self.assertEqual(report['new_revisions'], 2)
        self.assertEqual(len(m.pending(changed)), 2)
        self.assertEqual(len(changed['events'][old['event_key']]['revisions']), 2)
        self.assertIn(old['revision'], changed['acknowledgements'][old['event_key']])
        with self.assertRaises(m.CommentError):
            m.acknowledgement(changed, ack_payload(old))

    def test_fingerprint_tracks_author_body_and_update_not_handler(self):
        c, r = comment(), {'type': 'docx', 'token': 'DocFixture'}
        loc = {'status': 'in_scope'}
        baseline = m.normalize_reply(r, c, reply(), 'replyRoot', loc)
        variants = [reply(author='ouOther'), reply(text='不同正文'), reply(update=11)]
        for value in variants:
            other = m.normalize_reply(r, c, value, 'replyRoot', loc)
            self.assertEqual(other['event_key'], baseline['event_key'])
            self.assertNotEqual(other['revision'], baseline['revision'])

    def test_initial_default_scan_does_not_claim_full_reconciliation(self):
        state, report = scanned(include=False)
        self.assertIsNone(state['last_full_reconciliation'])
        self.assertTrue(report['warnings'])
        state, report = m.scan(config(), state, FakeCli(), include_resolved=True)
        self.assertTrue(state['last_full_reconciliation'])
        self.assertEqual(report['warnings'], [])

    def test_ack_requires_readback_evidence_and_in_scope_location(self):
        state, _ = scanned()
        payload = ack_payload(m.pending(state)[0])
        payload['evidence'] = [{'reference': 'only-a-link'}]
        with self.assertRaises(m.CommentError):
            m.acknowledgement(state, payload)
        unknown, _ = scanned(FakeCli({'bitable': [], 'docx': [comment(block=None)]}))
        with self.assertRaises(m.CommentError):
            m.acknowledgement(unknown, ack_payload(m.pending(unknown)[0]))

    def test_receipt_must_be_read_back_in_same_thread_and_resolved_requires_full_scan(self):
        cli = FakeCli({'bitable': [], 'docx': [comment(solved=True)]}, {'commentA': [reply(), reply('receipt')]})
        state, _ = scanned(cli)
        payload = ack_payload(next(x for x in m.pending(state) if x['root_reply']))
        payload.update(receipt={'reply_id': 'receipt'}, resolved=True)
        acked, _ = m.acknowledgement(state, payload)
        stored = acked['acknowledgements'][payload['event_key']][payload['revision']][0]
        self.assertFalse(stored['remote_write_performed'])
        self.assertFalse(stored['remote_proof_verified'])
        payload['receipt']['reply_id'] = 'other-thread-reply'
        with self.assertRaises(m.CommentError):
            m.acknowledgement(state, payload)
        state, _ = m.scan(config(), state, cli, include_resolved=False)
        payload['receipt']['reply_id'] = 'receipt'
        with self.assertRaises(m.CommentError):
            m.acknowledgement(state, payload)

    def test_binding_mismatch_preserves_existing_state(self):
        state, _ = scanned()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            m.atomic_json(path, state)
            original = path.read_bytes()
            with self.assertRaises(m.CommentError):
                m.load_state(path, config('B'))
            self.assertEqual(path.read_bytes(), original)


class CliTests(unittest.TestCase):
    def test_windows_standard_library_lock_fallback(self):
        calls = []
        win = type('WinLock', (), {'LK_NBLCK': 2, 'LK_UNLCK': 0,
                                   'locking': staticmethod(lambda fd, flag, count: calls.append((flag, count)))})
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(m, 'fcntl', None), mock.patch.object(m, 'msvcrt', win):
            with m.locked(Path(temp) / 'state.json'):
                self.assertEqual(calls, [(2, 1)])
        self.assertEqual(calls, [(2, 1), (0, 1)])

    def test_only_read_commands_with_explicit_user_and_profile(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'identity': 'user', 'data': {}}), '')
        cli = m.Cli('selected-profile', runner)
        cli.read('drive', '+list-comments', '--token', 'DocFixture', '--type', 'docx')
        self.assertEqual(calls[0][-4:], ['--as', 'user', '--profile', 'selected-profile'])
        with self.assertRaises(m.CommentError):
            cli.read('drive', '+add-reply', '--text', 'must not send')
        self.assertEqual(len(calls), 1)

    def test_current_profile_resolves_effective_without_switching(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps([
                {'name': 'active-but-overridden', 'active': True}, {'name': 'effective-profile', 'effective': True}]), '')
        self.assertEqual(m.current_profile({}, runner=runner), 'effective-profile')
        self.assertEqual(calls, [['lark-cli', 'profile', 'list']])
        self.assertEqual(m.current_profile({'lark_profile': 'configured'}, runner=runner), 'configured')
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
