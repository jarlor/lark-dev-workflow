import copy
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import work_packet as m


def binding():
    return {'format_version': 2, 'tables': {'work': {'id': 'tableFixture', 'fields': {
        'number': {'id': 'fNumber', 'type': 'auto_number'},
        'title': {'id': 'fTitle', 'type': 'text'},
        'status': {'id': 'fStage', 'type': 'select'},
        'human_action': {'id': 'fHuman', 'type': 'checkbox'},
        'next_step': {'id': 'fNext', 'type': 'text'},
        'original': {'id': 'fOriginal', 'type': 'text'},
        'upstream': {'id': 'fUpstream', 'type': 'link', 'target_table_id': 'tableFeedback'},
    }}}}


def record(title='Original task'):
    return {'role': 'work', 'fields': {
        'fNumber': 'EXAMPLE-001', 'fTitle': title, 'fStage': ['Custom stage'],
        'fHuman': False, 'fNext': 'Check evidence', 'fOriginal': 'Full original scope',
        'fUpstream': [{'id': 'feedbackFixture'}],
    }}


def comment(reply='root', text='Original confirmation', thread='threadFixture', **kwargs):
    content = {'resource_type': 'bitable', 'resource_token': 'baseFixture',
               'comment_id': thread, 'reply_id': reply, 'root_reply': reply == 'root',
               'author': 'sameAccount', 'text': text,
               'body': {'elements': [{'text_run': {'text': text}}]},
               'create_time': 1, 'update_time': 2, 'raw_reply_extra': None}
    content.update(kwargs)
    key = f'bitable:baseFixture/comment:{thread}/reply:{reply}'
    return key, {'revision': 'revision-' + text, 'context': 'contextFixture', 'content': content,
                 'location': {'status': 'in_scope', 'table_id': 'tableFixture', 'record_id': 'recordFixture'},
                 'thread': {'is_solved': False, 'solver_user_id': None}}


def snapshot():
    key, root = comment()
    return {'format_version': 1, 'binding': {'base_token': 'baseFixture'},
            'records': {'tableFixture/recordFixture': record()}, 'comments': {key: root},
            'notices': {}, 'coverage': {'feedback': {'mode': 'linked_only'}},
            'observed_at': 'irrelevant clock'}


class WorkPacketTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.old = snapshot()
        self.current = copy.deepcopy(self.old)
        self.record_key = 'tableFixture/recordFixture'
        self.root_key = next(iter(self.old['comments']))
        self.thread_key = 'bitable:baseFixture/comment:threadFixture'

    def packet(self):
        return m.build_work_packet(self.current, self.old, self.binding)

    def test_baseline_is_existing_backlog_not_fabricated_delta(self):
        packet = m.build_work_packet(self.current, None, self.binding)
        self.assertEqual(packet['mode'], 'baseline')
        self.assertEqual(packet['records']['current'], self.current['records'])
        self.assertNotIn('added', packet['records'])
        self.assertEqual(packet['threads'][self.thread_key], self.current['comments'])
        self.assertIn(self.record_key, packet['record_index'])

    def test_only_changed_fields_and_genuinely_new_records_are_in_delta(self):
        self.current['records'][self.record_key]['fields']['fStage'] = ['Any custom stage']
        self.current['records']['tableFixture/newRecord'] = record('New task')
        packet = self.packet()
        self.assertEqual(packet['mode'], 'delta')
        self.assertEqual(packet['records']['modified'][self.record_key], [{
            'path': '/fields/fStage', 'kind': 'modified',
            'before': ['Custom stage'], 'after': ['Any custom stage']}])
        self.assertEqual(list(packet['records']['added']), ['tableFixture/newRecord'])
        self.assertIn(self.root_key, packet['threads'][self.thread_key])

    def test_no_changes_keeps_every_record_and_thread_in_index(self):
        for number, stage in enumerate(['Anything', 'Done', 'Stopped', 'Waiting', 'Historical']):
            item = record('Unchanged ' + stage)
            item['fields']['fStage'] = [stage]
            self.old['records'][f'tableFixture/record{number}'] = item
        self.current = copy.deepcopy(self.old)
        self.current['observed_at'] = 'a new clock value'
        packet = self.packet()
        self.assertEqual(set(packet['record_index']), set(self.old['records']))
        self.assertEqual(packet['records'], {'added': {}, 'modified': {}, 'absent': {}})
        self.assertEqual(packet['threads'], {})
        self.assertIn(self.thread_key, packet['thread_index'])
        self.assertEqual(packet['metadata']['changes'], [])

    def test_long_index_previews_never_repeat_full_prose_and_locate_links(self):
        fields = self.current['records'][self.record_key]['fields']
        fields['fTitle'] = 'Long title ' * 100
        fields['fNext'] = 'Long next step ' * 100
        fields['fOriginal'] = 'Original full evidence ' * 1000
        self.old = copy.deepcopy(self.current)
        packet = self.packet()
        item = packet['record_index'][self.record_key]
        self.assertTrue(item['title']['truncated'])
        self.assertEqual(len(item['title']['preview']), m.PREVIEW_LIMIT)
        self.assertTrue(item['next_step']['truncated'])
        self.assertNotIn('fOriginal', json.dumps(item))
        self.assertEqual(item['relations']['upstream'], {
            'field_id': 'fUpstream', 'target_table_id': 'tableFeedback'})
        self.assertNotIn('Original full evidence', json.dumps(packet))

    def test_field_removal_null_and_type_change_are_distinct(self):
        fields = self.current['records'][self.record_key]['fields']
        del fields['fOriginal']
        fields['fNext'] = None
        fields['fHuman'] = 0
        changes = {item['path']: item for item in self.packet()['records']['modified'][self.record_key]}
        self.assertEqual(changes['/fields/fOriginal']['kind'], 'absent')
        self.assertNotIn('after', changes['/fields/fOriginal'])
        self.assertIsNone(changes['/fields/fNext']['after'])
        self.assertEqual(changes['/fields/fHuman']['kind'], 'modified')

    def test_nested_list_value_changes_are_not_hidden_by_python_equality(self):
        self.old['records'][self.record_key]['fields']['fStage'] = [{'flag': False}]
        self.current['records'][self.record_key]['fields']['fStage'] = [{'flag': 0}]
        changes = self.packet()['records']['modified'][self.record_key]
        self.assertEqual(changes[0]['path'], '/fields/fStage')
        self.assertEqual(changes[0]['before'], [{'flag': False}])
        self.assertEqual(changes[0]['after'], [{'flag': 0}])

    def test_comment_edit_and_same_account_reply_keep_full_original_thread(self):
        key, reply = comment('humanReply', 'Yes, this scope', author='sameAccount')
        self.current['comments'][key] = reply
        packet = self.packet()
        self.assertEqual(packet['comments']['added'], [key])
        self.assertEqual(set(packet['threads'][self.thread_key]), {key, self.root_key})
        self.assertEqual(packet['threads'][self.thread_key][self.root_key]['content']['text'],
                         'Original confirmation')
        self.old = copy.deepcopy(self.current)
        self.current['comments'][key]['content']['text'] = 'Correction to the confirmation'
        # Even a reused or buggy revision hash cannot hide changed content.
        packet = self.packet()
        changed = packet['comments']['modified'][key]
        self.assertIn('/content/text', changed['changed_paths'])
        self.assertEqual(changed['previous_pointer'], '/comments/' + key.replace('/', '~1'))
        self.assertEqual(packet['threads'][self.thread_key][key]['content']['text'],
                         'Correction to the confirmation')

    def test_solved_reopened_and_location_changes_are_never_filtered(self):
        for solved in (True, False):
            self.current['comments'][self.root_key]['thread']['is_solved'] = solved
            packet = self.packet()
            self.assertIn('/thread/is_solved', packet['comments']['modified'][self.root_key]['changed_paths'])
            self.assertIn(self.root_key, packet['threads'][self.thread_key])
            self.old = copy.deepcopy(self.current)
        self.current['comments'][self.root_key]['location']['record_id'] = 'newLocation'
        packet = self.packet()
        self.assertIn('/location/record_id', packet['comments']['modified'][self.root_key]['changed_paths'])

    def test_missing_and_moved_sources_are_absence_not_deletion(self):
        del self.current['records'][self.record_key]
        del self.current['comments'][self.root_key]
        packet = self.packet()
        self.assertEqual(packet['records']['absent'][self.record_key]['status'], 'not_in_current_snapshot')
        self.assertEqual(packet['comments']['absent'][self.root_key]['observation']['status'],
                         'not_in_current_snapshot')
        self.current['notices'][self.root_key] = {'status': 'moved_out_of_scope', 'reason': 'Fixture movement'}
        packet = self.packet()
        self.assertEqual(packet['comments']['absent'][self.root_key]['observation']['status'], 'moved_out_of_scope')
        self.assertNotIn('Original confirmation', json.dumps(packet))
        self.assertEqual(packet['threads'], {})

    def test_returning_comment_is_distinct_from_a_new_source(self):
        del self.old['comments'][self.root_key]
        self.old['notices'][self.root_key] = {'status': 'not_returned'}
        packet = self.packet()
        self.assertEqual(packet['comments']['added'], [])
        self.assertEqual(packet['comments']['restored'], [self.root_key])
        self.assertIn(self.root_key, packet['threads'][self.thread_key])
        notice_change = next(c for c in packet['metadata']['changes'] if c['path'].startswith('/notices/'))
        self.assertEqual(notice_change['kind'], 'absent')

    def test_metadata_schema_access_coverage_and_notices_changes_are_explicit(self):
        self.current['schema'] = {'fields': ['addedField']}
        self.current['access'] = {'mode': 'browser'}
        self.current['coverage']['feedback']['mode'] = 'project_input'
        self.current['notices']['unresolvedFixture'] = {'status': 'unresolved'}
        changes = {c['path']: c for c in self.packet()['metadata']['changes']}
        for path in ('/schema', '/access', '/coverage/feedback/mode', '/notices/unresolvedFixture'):
            self.assertIn(path, changes)
        self.assertIn('historical mapping equality is unknown', self.packet()['schema_comparison'])

    def test_new_scope_never_claims_cross_scope_delta(self):
        self.current['binding'] = {'base_token': 'otherFixture'}
        packet = self.packet()
        self.assertEqual(packet['mode'], 'full_fallback')
        self.assertEqual(packet['reason'], 'snapshot_binding_changed')
        self.assertEqual(packet['records']['current'], self.current['records'])
        self.assertNotIn('absent', packet['records'])
        self.assertIn('/binding/base_token', [c['path'] for c in packet['metadata']['changes']])

    def test_unknown_current_data_is_preserved_in_full_fallback(self):
        mutations = [lambda s: s.update(format_version=7),
                     lambda s: s['records'][self.record_key].update(fields=['unknown']),
                     lambda s: s['records'][self.record_key]['fields'].update(unmapped='Important detail'),
                     lambda s: s['comments'][self.root_key].pop('content')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                current = snapshot()
                mutate(current)
                packet = m.build_work_packet(current, self.old, self.binding)
                self.assertEqual(packet['mode'], 'full_fallback')
                self.assertEqual(packet['snapshot'], current)

    def test_unknown_checkpoint_and_mapping_do_not_produce_fake_delta(self):
        packet = m.build_work_packet(self.current, {'format_version': 99}, self.binding)
        self.assertEqual(packet['mode'], 'full_fallback')
        self.assertEqual(packet['records']['current'], self.current['records'])
        self.binding['tables']['work']['fields']['duplicate'] = {'id': 'fTitle'}
        packet = self.packet()
        self.assertEqual(packet['mode'], 'full_fallback')
        self.assertEqual(packet['reason'], 'unsupported_current_mapping')

    def test_json_pointer_escaping_and_all_inputs_remain_unchanged(self):
        field = 'field/with~punctuation'
        self.binding['tables']['work']['fields']['custom'] = {'id': field}
        self.current['records'][self.record_key]['fields'][field] = 'literal value'
        before = copy.deepcopy((self.current, self.old, self.binding))
        first = self.packet()
        second = self.packet()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual((self.current, self.old, self.binding), before)
        change = first['records']['modified'][self.record_key][0]
        self.assertEqual(change['path'], '/fields/field~1with~0punctuation')
        first['record_index'][self.record_key]['status'].append('mutated output')
        self.assertEqual((self.current, self.old, self.binding), before)


if __name__ == '__main__':
    unittest.main()
