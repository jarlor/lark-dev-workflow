"""Offline receipt validation; all accounts, content and evidence are fictitious."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import comments
import own_effects as m
import unattended


def comment(reply='replyFixture', author='shared-fixture-user', text='Worker update', update=2):
    content = {
        'resource_type': 'bitable', 'resource_token': 'FixtureBase',
        'comment_id': 'commentFixture', 'reply_id': reply, 'root_reply': False,
        'author': author, 'body': {'elements': [{'text_run': {'text': text}}]},
        'text': text, 'create_time': 1, 'update_time': update, 'raw_reply_extra': None,
    }
    value = {
        'content': content,
        'location': {'status': 'in_scope', 'table_id': 'tblWork', 'record_id': 'recItem',
                     'project_record_id': 'recProject', 'project_field_id': 'fldProject',
                     'method': 'notify_extra_and_live_record'},
        'thread': {'is_solved': False, 'solver_user_id': None},
    }
    refresh_hashes(value)
    key = comments.event_key({'type': content['resource_type'], 'token': content['resource_token']},
                             content['comment_id'], content['reply_id'])
    return key, value


def refresh_hashes(value):
    value['revision'] = comments.digest({k: value['content'][k]
                                         for k in ('author', 'body', 'update_time')})
    value['context'] = comments.context_fingerprint(value)


def snapshot_id(value):
    return unattended.digest(unattended.stable_snapshot(value))


class OwnEffectsTests(unittest.TestCase):
    def setUp(self):
        key, value = comment()
        self.key = key
        self.start = {
            'format_version': 1, 'observed_at': 'fixture-start',
            'binding': {'base_token': 'FixtureBase', 'scope': {'project_record_id': 'recProject'},
                        'documents': []},
            'records': {'tblWork/recItem': {'role': 'work', 'fields': {
                'fldStatus': ['Pending'], 'fldTitle': 'Fixture item', 'fldCount': 1}}},
            'comments': {key: value}, 'notices': {},
            'coverage': {'feedback': {'mode': 'linked_only'}},
        }
        self.run = 'fixture-run'
        self.sid = snapshot_id(self.start)
        self.receipt = {
            'format_version': 1, 'run_id': self.run, 'snapshot_id': self.sid,
            'record_fields': [{'record_key': 'tblWork/recItem', 'field_id': 'fldStatus',
                               'before': ['Pending'], 'after': ['Done'],
                               'readback_evidence': 'tool-result:fixture-field-readback'}],
            'comments': [],
        }

    def derive(self, receipt=None, start=None, run=None, sid=None):
        return m.expected_snapshot(self.start if start is None else start,
                                   self.receipt if receipt is None else receipt,
                                   self.run if run is None else run,
                                   self.sid if sid is None else sid)

    def add_comment(self, *, edit=False):
        key, value = comment(reply='replyFixture' if edit else 'replyAddedFixture',
                             text='Exact worker readback', update=3)
        entry = {'event_key': key, 'before': copy.deepcopy(self.start['comments'][key]) if edit else None,
                 'after': value, 'readback_evidence': '/private/fixture/comment-readback.json'}
        self.receipt['comments'].append(entry)
        return entry

    def test_exact_readback_matches_while_preserving_inputs_and_other_data(self):
        before, receipt_before = copy.deepcopy(self.start), copy.deepcopy(self.receipt)
        expected = self.derive()
        scan = copy.deepcopy(self.start)
        scan['records']['tblWork/recItem']['fields']['fldStatus'] = ['Done']
        scan['observed_at'] = 'later complete scan'
        self.assertEqual(snapshot_id(expected), snapshot_id(scan))
        self.assertEqual(self.start, before)
        self.assertEqual(self.receipt, receipt_before)
        expected['records']['tblWork/recItem']['fields']['fldTitle'] = 'Derived only'
        self.assertEqual(self.start, before)

    def test_any_concurrent_field_record_or_snapshot_state_change_remains_visible(self):
        derived = self.derive()
        changes = {
            'same field changed again': lambda s: s['records']['tblWork/recItem']['fields'].update(fldStatus=['Human state']),
            'different field': lambda s: s['records']['tblWork/recItem']['fields'].update(fldTitle='Human title'),
            'new record': lambda s: s['records'].update({'tblWork/recHuman': {'role': 'work', 'fields': {}}}),
            'removed record': lambda s: s['records'].pop('tblWork/recItem'),
            'coverage': lambda s: s['coverage']['feedback'].update(mode='project_input'),
            'binding': lambda s: s['binding'].update(base_token='OtherFixtureBase'),
            'unresolved notice': lambda s: s['notices'].update({'other': {'status': 'unresolved'}}),
            'resolved thread': lambda s: s['comments'][self.key]['thread'].update(is_solved=True),
            'moved comment': lambda s: s['comments'][self.key]['location'].update(record_id='recOther'),
            'removed comment': lambda s: s['comments'].pop(self.key),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                actual = copy.deepcopy(derived)
                change(actual)
                self.assertNotEqual(snapshot_id(derived), snapshot_id(actual))

    def test_human_replies_and_edits_stay_visible_even_on_the_worker_account(self):
        self.add_comment()
        derived = self.derive()
        for author in ('another-fixture-user', 'shared-fixture-user'):
            with self.subTest(author=author):
                key, value = comment(reply='humanReplyFixture', author=author, text='Human reply')
                actual = copy.deepcopy(derived)
                actual['comments'][key] = value
                self.assertNotEqual(snapshot_id(derived), snapshot_id(actual))
        actual = copy.deepcopy(derived)
        _, actual['comments'][self.key] = comment(text='Human edit under shared account', update=9)
        self.assertNotEqual(snapshot_id(derived), snapshot_id(actual))

    def test_exact_comment_add_and_same_source_edit(self):
        self.receipt['record_fields'] = []
        added = self.add_comment()
        edited = self.add_comment(edit=True)
        derived = self.derive()
        self.assertEqual(len(derived['comments']), 2)
        self.assertEqual(derived['comments'][added['event_key']], added['after'])
        self.assertEqual(derived['comments'][edited['event_key']], edited['after'])
        self.assertEqual(self.start['comments'][self.key]['content']['text'], 'Worker update')

    def test_rejects_old_run_snapshot_and_wrong_start_digest(self):
        for key, value in (('run_id', 'older-run'), ('snapshot_id', 'older-snapshot')):
            receipt = copy.deepcopy(self.receipt)
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(m.OwnEffectsError):
                self.derive(receipt)
        changed = copy.deepcopy(self.start)
        changed['records']['tblWork/recItem']['fields']['fldTitle'] = 'Changed start'
        with self.assertRaises(m.OwnEffectsError):
            self.derive(start=changed)
        # observed_at is the only exempted top-level value.
        changed = copy.deepcopy(self.start)
        changed['observed_at'] = 'other observation time'
        self.derive(start=changed)

    def test_rejects_wrong_before_unknown_fields_records_and_whole_rows(self):
        changes = {
            'wrong before': {'before': ['Other value']},
            'unknown field': {'field_id': 'fldUnknown'},
            'unknown record': {'record_key': 'tblWork/recUnknown'},
            'whole row': {'field_id': None, 'after': {'fields': {'fldStatus': ['Done']}}},
            'no change': {'after': ['Pending']},
            'no evidence': {'readback_evidence': ''},
            'blank evidence': {'readback_evidence': '  '},
            'non-string evidence': {'readback_evidence': {'claimed': True}},
        }
        for label, fields in changes.items():
            with self.subTest(label=label):
                receipt = copy.deepcopy(self.receipt)
                receipt['record_fields'][0].update(fields)
                with self.assertRaises(m.OwnEffectsError):
                    self.derive(receipt)

    def test_strict_json_types_and_unknown_properties(self):
        mutations = [
            lambda r: r.update(format_version=True),
            lambda r: r.update(record_fields={}),
            lambda r: r.update(comments=None),
            lambda r: r.update(coverage={'feedback': {}}),
            lambda r: r['record_fields'][0].update(operation='replace'),
            lambda r: r['record_fields'][0].update(after=float('nan')),
            lambda r: r['record_fields'][0].update(after=float('inf')),
            lambda r: r['record_fields'][0].update(after={1: 'non-string key'}),
            lambda r: r['record_fields'][0].update(after=('not', 'json')),
            lambda r: r.pop('comments'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                receipt = copy.deepcopy(self.receipt)
                mutate(receipt)
                with self.assertRaises(m.OwnEffectsError):
                    self.derive(receipt)
        for before in (True, 1.0):
            receipt = copy.deepcopy(self.receipt)
            receipt['record_fields'][0].update(field_id='fldCount', before=before, after=2)
            with self.subTest(before=before), self.assertRaises(m.OwnEffectsError):
                self.derive(receipt)

    def test_duplicate_field_or_comment_rejects_entire_receipt(self):
        for kind in ('record_fields', 'comments'):
            with self.subTest(kind=kind):
                if kind == 'comments':
                    self.add_comment()
                receipt = copy.deepcopy(self.receipt)
                receipt[kind].append(copy.deepcopy(receipt[kind][0]))
                with self.assertRaises(m.OwnEffectsError):
                    self.derive(receipt)
                self.assertEqual(self.start['records']['tblWork/recItem']['fields']['fldStatus'], ['Pending'])
                self.assertEqual(len(self.start['comments']), 1)

    def test_invalid_later_entry_rejects_all_without_mutating_either_input(self):
        edited = self.add_comment(edit=True)
        # A valid field write is followed by a stale comment before-value.
        edited['before']['content']['text'] = 'Unobserved earlier text'
        before, receipt_before = copy.deepcopy(self.start), copy.deepcopy(self.receipt)
        with self.assertRaises(m.OwnEffectsError):
            self.derive()
        self.assertEqual(self.start, before)
        self.assertEqual(self.receipt, receipt_before)

    def test_explicit_field_clear_is_distinct_from_record_deletion(self):
        self.receipt['record_fields'][0]['after'] = None
        derived = self.derive()
        self.assertIn('tblWork/recItem', derived['records'])
        self.assertIn('fldStatus', derived['records']['tblWork/recItem']['fields'])
        self.assertIsNone(derived['records']['tblWork/recItem']['fields']['fldStatus'])

    def test_comment_delete_unknown_properties_incomplete_and_bad_hashes_reject(self):
        self.add_comment(edit=True)
        changes = [
            lambda e: e.update(after=None),
            lambda e: e.update(before=None),
            lambda e: e.update(readback_evidence=''),
            lambda e: e.update(author='claimed-worker'),
            lambda e: e['after'].pop('location'),
            lambda e: e['after']['content'].pop('raw_reply_extra'),
            lambda e: e['after']['thread'].update(extra=True),
            lambda e: e['after'].update(revision='fake-hash'),
            lambda e: e['after'].update(context='fake-hash'),
            lambda e: e['after']['content'].update(text='Not its body text'),
            lambda e: e['after']['content'].update(root_reply=1),
            lambda e: e['after']['content'].update(update_time=True),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                receipt = copy.deepcopy(self.receipt)
                change(receipt['comments'][0])
                with self.assertRaises(m.OwnEffectsError):
                    self.derive(receipt)

    def test_comment_identity_location_or_resolution_cannot_be_consumed_as_edit(self):
        self.add_comment(edit=True)
        changes = [
            lambda a: a['content'].update(resource_token='AnotherFixtureBase'),
            lambda a: a['content'].update(comment_id='AnotherFixtureComment'),
            lambda a: a['content'].update(reply_id='AnotherFixtureReply'),
            lambda a: a['content'].update(author='AnotherFixtureAuthor'),
            lambda a: a['content'].update(create_time=99),
            lambda a: a['content'].update(root_reply=True),
            lambda a: a['location'].update(record_id='AnotherFixtureRecord'),
            lambda a: a['thread'].update(is_solved=True, solver_user_id='FixtureSolver'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                receipt = copy.deepcopy(self.receipt)
                after = receipt['comments'][0]['after']
                change(after)
                refresh_hashes(after)
                with self.assertRaises(m.OwnEffectsError):
                    self.derive(receipt)

    def test_added_comment_must_be_new_and_cannot_replace_unverified_notice(self):
        added = self.add_comment()
        receipt = copy.deepcopy(self.receipt)
        receipt['comments'][0]['before'] = copy.deepcopy(added['after'])
        with self.assertRaises(m.OwnEffectsError):
            self.derive(receipt)
        start = copy.deepcopy(self.start)
        start['notices'][added['event_key']] = {'status': 'unresolved'}
        receipt = copy.deepcopy(self.receipt)
        receipt['snapshot_id'] = snapshot_id(start)
        with self.assertRaises(m.OwnEffectsError):
            self.derive(receipt, start=start, sid=receipt['snapshot_id'])

    def test_no_receipt_or_no_writes_cannot_claim_verified_effects(self):
        with self.assertRaises(m.OwnEffectsError):
            m.expected_snapshot(self.start, None, self.run, self.sid)
        self.receipt['record_fields'] = []
        with self.assertRaises(m.OwnEffectsError):
            self.derive()
        entry = self.add_comment(edit=True)
        entry['after'] = copy.deepcopy(entry['before'])
        with self.assertRaises(m.OwnEffectsError):
            self.derive()

    def test_real_watch_normalization_is_accepted_without_external_reads(self):
        # Exercise the actual collector with its offline fake reader, rather than
        # assume our fixture matches the comment normalization contract.
        from test_watch import FakeCli, config
        import watch
        cli = FakeCli()
        with tempfile.TemporaryDirectory() as directory:
            start = watch.collect_with_reader(config(), cli,
                                              comments_state_path=Path(directory) / 'comments.json')
            key = next(iter(start['comments']))
            previous = start['comments'][key]
            cli.replies['c1'][0]['content'] = {'elements': [{'text_run': {'text': 'Fixture own write'}}]}
            cli.replies['c1'][0]['update_time'] = 22
            after = watch.collect_with_reader(config(), cli,
                                              comments_state_path=Path(directory) / 'comments.json')
        receipt = {'format_version': 1, 'run_id': self.run, 'snapshot_id': snapshot_id(start),
                   'record_fields': [], 'comments': [{
                       'event_key': key, 'before': previous, 'after': after['comments'][key],
                       'readback_evidence': 'tool-result:fixture-real-normalization'}]}
        derived = m.expected_snapshot(start, receipt, self.run, receipt['snapshot_id'])
        self.assertEqual(derived, after)

    def test_native_stage_snapshot_preserves_schema_and_does_not_hide_concurrent_changes(self):
        from test_browser_watch import fixture, stage_field
        import browser_watch
        import watch
        binding, access, payload = fixture()
        raw = stage_field()
        binding['tables']['work']['fields']['status'] = browser_watch.field(raw)
        table = payload['tables']['tblWork']
        table['fields'].append(raw)
        cells = table['rows'][0]['cells']
        cells[raw['id']] = {'value': 'optWait'}
        with tempfile.TemporaryDirectory() as directory:
            def scan():
                return watch.collect_with_reader(binding, browser_watch.Reader(binding, access, payload),
                                                 comments_state_path=Path(directory) / 'comments.json')
            start = scan()
            cells['fldTitle'] = {'value': 'Exact worker title readback'}
            after = scan()
            receipt = {'format_version': 1, 'run_id': self.run, 'snapshot_id': snapshot_id(start),
                       'record_fields': [{'record_key': 'tblWork/recWork', 'field_id': 'fldTitle',
                           'before': start['records']['tblWork/recWork']['fields']['fldTitle'],
                           'after': after['records']['tblWork/recWork']['fields']['fldTitle'],
                           'readback_evidence': 'tool-result:fixture-stage-title-readback'}], 'comments': []}
            original, original_receipt = copy.deepcopy(start), copy.deepcopy(receipt)
            derived = m.expected_snapshot(start, receipt, self.run, receipt['snapshot_id'])
            self.assertEqual(derived, after)
            self.assertEqual(derived['stage_fields'], start['stage_fields'])
            state = {'last_result': {'run_id': self.run, 'snapshot_id': receipt['snapshot_id']}}
            effects = Path(directory) / 'runs' / self.run / 'effects.json'
            effects.parent.mkdir(parents=True)
            effects.write_text(json.dumps(receipt), encoding='utf-8')
            self.assertTrue(unattended.matches_own_effects(directory, state, start, snapshot_id(after)))

            # A human advances the node alongside the title write. It is not in
            # the receipt and must still wake the worker.
            cells[raw['id']] = {'value': 'optReady'}
            self.assertFalse(unattended.matches_own_effects(directory, state, start, snapshot_id(scan())))
            cells[raw['id']] = {'value': 'optWait'}
            # Configuration changes must also remain visible when all record
            # values match the explicitly acknowledged writes.
            raw['property']['options'][0]['transitionRule']['permissionFieldIds'] = ['fldAnotherOwner']
            changed_schema = scan()
            self.assertEqual(changed_schema['records'], after['records'])
            self.assertFalse(unattended.matches_own_effects(directory, state, start, snapshot_id(changed_schema)))
            forbidden = copy.deepcopy(receipt)
            forbidden['stage_fields'] = changed_schema['stage_fields']
            with self.assertRaises(m.OwnEffectsError):
                m.expected_snapshot(start, forbidden, self.run, receipt['snapshot_id'])
            derived['stage_fields'].clear()
            self.assertEqual(start, original)
            self.assertEqual(receipt, original_receipt)

    def test_stage_metadata_is_optional_and_unknown_snapshot_properties_still_reject(self):
        self.assertNotIn('stage_fields', self.derive())
        for extra in ({'stage_fields': None}, {'stage_fields': []}, {'stage_fields': 'unknown'},
                      {'stage_fields': {}, 'future_schema': {}}):
            with self.subTest(extra=extra):
                start = dict(self.start, **extra)
                receipt = dict(self.receipt, snapshot_id=snapshot_id(start))
                with self.assertRaises(m.OwnEffectsError):
                    self.derive(receipt, start=start, sid=receipt['snapshot_id'])


if __name__ == '__main__':
    unittest.main()
