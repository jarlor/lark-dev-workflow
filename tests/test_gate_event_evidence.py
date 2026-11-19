"""Offline test of a complete scan, the existing SDK callback, and the normal worker."""
import json
import importlib.util
import copy
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

from test_unattended import RuntimeFixture, write_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import event_bridge as bridge
import comments
import gate_events
import watch
import browser_watch
from test_browser_watch import stage_field
from test_watch import FakeCli


class GateEventTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.actor = dict(identity='bot', profile='fixture-profile', app_id='cli_demo', open_id='ou_assistant')
        self.config.update(format_version=3, event_tenant_key='tenant_test')
        self.save_config()
        self.binding['cli_actor'] = self.actor
        self.binding['tables']['work']['fields'].update({
            'project': dict(id='fldProject', type='link', target_table_id='tblProjects'),
            'status': dict(id='fldStatus', type='stage', options=[dict(id='optWait', name='待确认计划'),
                                                                  dict(id='optReady', name='可开始')]),
            'plan_revision': dict(id='fldRev', type='number'),
            'confirmed_plan_revision': dict(id='fldConfirmRev', type='number')})
        write_json(self.project_path, self.binding)
        self.directory = Path(self.config['state_dir'])
        self.row = self.snapshot['records']['tblWork/recItem']
        self.row['fields'].update(fldProject=[{'id':'recProject'}], fldRev=1, fldStatus=['待确认计划'])
        self.snapshot['records']['tblProjects/recProject'] = dict(role='projects', fields={})
        self.snapshot.update(binding=comments.binding_identity(self.binding), coverage={'feedback': {'mode':'linked_only'}})
        self.snapshot['gate_status_options'] = {
            'tblWork/fldStatus': self.binding['tables']['work']['fields']['status']['options']}
        self.service = dict(lark_profile='fixture-profile', app_id='cli_demo', tenant_key='tenant_test',
                            bot_open_id='ou_assistant', base_token='FixtureBase', project_record_id='recProject',
                            table_ids=['tblWork', 'tblProjects'], runner_config=str(self.config_path),
                            state_dir=str(self.root / 'event-state'))
        self.clock_sec = int(time.time())
        with mock.patch.object(bridge.time, 'time', return_value=self.clock_sec-20):
            self.journal = bridge.Journal(self.service)

    def scan(self, end=None, start=None):
        end = self.clock_sec-5 if end is None else end
        with mock.patch.object(gate_events.time, 'time', return_value=end):
            return gate_events.publish_scope(self.config, self.binding, self.snapshot, self.directory,
                                            started_at=start)

    def event(self, event_id='evtA', actor='ou_person', record='recItem', field_id='fldStatus',
              before='optWait', after='optReady'):
        return dict(header=dict(app_id='cli_demo', tenant_key='tenant_test', event_type=bridge.RECORD,
                                event_id=event_id), event=dict(file_token='FixtureBase', file_type='bitable',
                    table_id='tblWork', operator_id={'open_id':actor}, revision=99, update_time=self.clock_sec,
                    action_list=[dict(record_id=record, action='record_edited',
                                      before_value=[dict(field_id=field_id, field_value=json.dumps(before))],
                                      after_value=[dict(field_id=field_id, field_value=json.dumps(after))])]))

    def receive(self, *args, **kwargs):
        return bridge.receive(self.event(*args, **kwargs), self.service, self.journal)

    def publish(self):
        self.journal.publish(self.service, self.directory, self.journal.status()['received'])
        return json.loads((self.directory / gate_events.BATCH).read_text())

    def test_full_scan_to_worker_once_for_unchanged_snapshot(self):
        self.assertEqual(self.tick()['status'], 'ran')
        self.scan()
        self.assertTrue(self.receive())
        self.assertFalse(self.receive())
        self.assertTrue(self.receive('evtB', field_id='fldConfirmRev', before=None, after=1))
        self.publish()
        result = self.tick()
        self.assertEqual(result['status'], 'ran')
        work = json.loads((self.calls[-1]['dir'] / 'work-packet.json').read_text())
        self.assertNotIn('ou_person', json.dumps(work))
        packet = json.loads(Path(work['gate_event_observations_file']).read_text())
        self.assertEqual(packet.pop('run_id'), work['gate_event_run_id'])
        self.assertEqual({k:v for k,v in packet.items() if k!='items'}, dict(tenant_key='tenant_test',
                         app_id='cli_demo', base_token='FixtureBase', project_record_id='recProject'))
        self.assertEqual([(e['field_id'], e['before'], e['after']) for e in packet['items']],
                         [('fldStatus','待确认计划','可开始'), ('fldConfirmRev',None,1)])
        self.assertEqual(packet['items'][0]['operator_open_id'], 'ou_person')
        self.assertEqual(packet['items'][0]['event_revision'], 99)
        self.assertEqual(self.state()['gate_event_checkpoint'][1], 2)
        self.assertEqual(self.tick()['status'], 'unchanged')

    def test_only_scanned_project_members_and_allowlisted_fields_and_values(self):
        self.snapshot['records']['tblWork/recOther'] = dict(role='work',fields={'fldProject':[{'id':'recOtherProject'}]})
        self.snapshot['records']['tblWork/recBad'] = dict(role='work',fields={'fldProject':['recProject']})
        self.receive('preScan')
        self.scan()
        self.receive('nonmember', record='recOther')
        self.receive('badlink', record='recBad')
        self.receive('unseen', record='recNew')
        self.receive('freeform', field_id='fldTitle', after='private business title')
        self.receive('unknownStage', after={'name':'private business title'})
        self.receive('noActor', actor='')
        self.receive('namedOnly', actor='Personnel Name')
        self.receive('nonIntegral', field_id='fldRev', before=1, after=2.3)
        self.assertFalse(self.receive('self', actor='ou_assistant'))
        self.assertTrue(self.receive('owned'))
        stale = self.event('old')
        stale['event']['update_time'] -= 200
        bridge.receive(stale, self.service, self.journal)
        missing_before = self.event('missingBefore')
        missing_before['event']['action_list'][0].pop('before_value')
        bridge.receive(missing_before, self.service, self.journal)
        self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['owned'])
        for secret in [b'private business title', b'recOther', b'recBad', b'Personnel Name']:
            self.assertNotIn(secret, self.journal.path.read_bytes())

    def test_move_including_own_invalidates_receipts_without_self_wake(self):
        self.snapshot['records']['tblWork/recSecond'] = self.row
        self.scan()
        self.receive()
        moved = self.event('changedLink', actor='ou_assistant', field_id='fldProject', before=[], after=['other'])
        self.assertFalse(bridge.receive(moved, self.service, self.journal))
        self.assertEqual(self.journal.status()['received'], 1)
        self.receive('whileInvalid')
        self.receive('unaffected', record='recSecond')
        self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['unaffected'])
        self.scan(end=self.clock_sec+1,start=self.clock_sec-1)
        self.receive('duringOverlap')
        self.scan(end=self.clock_sec+2)
        self.assertFalse(bridge.receive(moved, self.service, self.journal))
        self.receive('delayedAcrossMove')
        self.clock_sec += 3
        self.receive('afterScan')
        self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['unaffected','afterScan'])

    def test_full_record_images_repeating_current_project_keep_the_status_change(self):
        self.scan()
        compact = json.dumps(['recProject'])
        rich = json.dumps([{'table_id':'tblProjects', 'record_ids':['recProject'], 'text':'项目', 'type':'text'}])
        links = ('recProject', compact, rich, json.dumps({'link_record_ids':['recProject']}))
        for i, link in enumerate(links):
            event = self.event(f'full{i}')
            action = event['event']['action_list'][0]
            for side in ('before_value','after_value'):
                action[side].append(dict(field_id='fldProject', field_value=link))
                action[side].append(dict(field_id='fldRev', field_value='1'))
            self.assertTrue(bridge.receive(event, self.service, self.journal))
            self.assertFalse(bridge.receive(event, self.service, self.journal))
        self.assertEqual([(row['event_id'],row['field_id']) for row in self.publish()['rows']],
                         [(f'full{i}','fldStatus') for i in range(len(links))])

    def test_full_images_with_unverified_or_changed_membership_invalidate_each_record(self):
        current = json.dumps(['recProject'])
        foreign = json.dumps(['recElsewhere'])
        wrong_table = json.dumps([{'table_id':'tblWrong','record_ids':['recProject']}])
        multiple = json.dumps(['recProject','recOther'])
        bad = (([current],[]), ([],[current]), ([current],[foreign]), ([foreign],[foreign]),
               ([current,current],[current]), ([current],[current,current]), ([''],['']),
               ([wrong_table],[wrong_table]), ([multiple],[multiple]), (['unknown'],['unknown']))
        for i in range(len(bad)):
            self.snapshot['records'][f'tblWork/recCase{i}'] = self.row
        self.scan()
        for i,(before,after) in enumerate(bad):
            record = f'recCase{i}'
            self.receive(f'previous{i}', record=record)
            event = self.event(f'move{i}', record=record)
            action = event['event']['action_list'][0]
            for side,values in [('before_value',before),('after_value',after)]:
                action[side].extend(dict(field_id='fldProject',field_value=v) for v in values)
            self.assertTrue(bridge.receive(event, self.service, self.journal))
            self.receive(f'after{i}', record=record)
        self.assertEqual(self.publish()['rows'],[])

    def test_delayed_event_for_member_across_scans_and_new_member_same_second(self):
        self.scan()
        self.snapshot['records']['tblWork/recSecond'] = self.row
        self.scan(end=self.clock_sec)
        envelope = self.event('delayed')
        envelope['event']['update_time'] -= 2
        bridge.receive(envelope, self.service, self.journal)
        self.receive('existingSameSecond')
        self.receive('newSameSecond', record='recSecond')
        self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['delayed','existingSameSecond'])
        self.snapshot['records'].pop('tblWork/recItem')
        self.scan(end=self.clock_sec+1)
        self.snapshot['records']['tblWork/recItem'] = self.row
        self.scan(end=self.clock_sec+2)
        self.receive('beforeRejoin')
        self.clock_sec += 3
        self.receive('afterRejoin')
        self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['afterRejoin'])

    def test_receiver_start_and_bounded_lookback(self):
        self.scan(end=self.clock_sec-100)
        before = self.event('beforeListener')
        before['event']['update_time'] -= 21
        bridge.receive(before, self.service, self.journal)
        self.receive('valid')
        self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['valid'])
        self.scan(end=self.clock_sec + gate_events.MAX_AGE + 1)
        self.receive('outsideWindow')
        self.assertEqual(self.publish()['rows'], [])

    def test_single_select_rename_and_missing_live_schema_never_use_bound_options(self):
        self.binding['tables']['work']['fields']['status']['type'] = 'select'
        write_json(self.project_path, self.binding)
        self.scan()
        self.snapshot['gate_status_options']['tblWork/fldStatus'] = [
            dict(id='optWait',name='待确认计划'), dict(id='optReady',name='需复查')]
        self.scan(end=self.clock_sec-1)
        self.receive('renamed')
        self.receive('oldName', after='可开始')
        self.assertEqual([(r['event_id'],r['after']) for r in self.publish()['rows']], [('renamed','需复查')])
        self.snapshot.pop('gate_status_options')
        scope = self.scan()
        self.receive('missing')
        self.assertEqual(self.publish()['rows'], [])
        self.assertEqual(scope['tables'], {})

    def test_scan_exports_the_current_options_for_a_bot_on_a_single_select(self):
        reader = FakeCli()
        reader.config['cli_actor'] = self.actor
        live_status = dict(id='fldState', type='select', options=[dict(id='optCurrent', name='需复查')])
        reader.config['tables']['work']['fields']['status'] = live_status
        reader.data['tblWork'][0]['values']['fldState'] = ['需复查']
        binding = copy.deepcopy(reader.config)
        binding['tables']['work']['fields']['status']['options'] = [dict(id='optCurrent', name='可开始')]
        snapshot = watch.collect_with_reader(binding, reader, comments_state_path=self.root / 'comments.json')
        self.assertEqual(snapshot['gate_status_options']['tblWork/fldState'], live_status['options'])
        self.assertEqual(gate_events.project_records(binding, snapshot)['tblWork']['options']['optCurrent'], '需复查')

    def test_batch_record_field_tuple_dedup_conflicts_suppress_only_one_record(self):
        self.snapshot['records']['tblWork/recSecond'] = self.row
        self.scan()
        envelope = self.event()
        actions = envelope['event']['action_list']
        first = actions[0]
        second = dict(first, record_id='recSecond')
        actions.extend([copy.deepcopy(first), second, copy.deepcopy(second),
                        dict(first, after_value=[dict(field_id='fldStatus', field_value='null')])])
        bridge.receive(envelope, self.service, self.journal)
        self.assertEqual([(r['event_id'],r['record_id']) for r in self.publish()['rows']], [('evtA','recSecond')])

    def inspected_native_field(self):
        self.snapshot['coverage']['native_stage_inspection'] = {
            'node_settings_verified': False, 'dispatch_verified': False}
        self.snapshot['native_stage_inspection'] = {
            'tblWork/fldStatus': dict(native_type=24, ui_type='Stage', cli_type='select')}

    def test_inspected_stage_preserves_opaque_wire_ids_and_requires_history(self):
        self.inspected_native_field()
        scope = self.scan()
        self.assertEqual(scope['tables']['tblWork']['native_stage_field_id'], 'fldStatus')
        self.receive('quoted', before='optUnknownA', after='optUnknownB')
        raw = self.event('plain')
        action = raw['event']['action_list'][0]
        action['before_value'][0]['field_value'] = 'optUnknownB'
        action['after_value'][0]['field_value'] = 'optUnknownC'
        bridge.receive(raw, self.service, self.journal)
        self.receive('unchanged', before='optA', after='optA')
        self.receive('mixedContent', before='optA', after='公开文字')
        self.receive('nested', before=['optA'], after=['optB'])
        self.receive('long', before='opt'+'A'*100, after='optB')
        self.receive('unknownRecord', record='recOther', before='optA', after='optB')
        self.receive('otherField', field_id='fldRev', before='optA', after='optB')
        self.assertFalse(self.receive('self', actor='ou_assistant', before='optA', after='optB'))
        self.assertEqual([(r['event_id'], r['value_format'], r['before'], r['after'])
                          for r in self.publish()['rows']], [
            ('quoted', 'native_stage_option_ids', 'optUnknownA', 'optUnknownB'),
            ('plain', 'native_stage_option_ids', 'optUnknownB', 'optUnknownC')])
        rows, checkpoint = gate_events.pending(self.directory, scope, {})
        self.assertEqual([r['event_id'] for r in rows], ['quoted', 'plain'])
        self.assertEqual(gate_events.pending(self.directory, scope, {'gate_event_checkpoint':checkpoint}), ([],None))

    def test_opaque_ids_do_not_transfer_to_select_unproven_stage_or_moved_record(self):
        self.inspected_native_field()
        self.scan()
        self.receive('valid', before='optSecretA', after='optSecretB')
        saved = self.publish()
        self.snapshot.pop('native_stage_inspection')
        self.snapshot['coverage'].pop('native_stage_inspection')
        scope = self.scan()
        self.assertNotIn('native_stage_field_id', scope['tables']['tblWork'])
        self.assertEqual(gate_events.pending(self.directory, scope, {})[0], [])
        self.receive('unproven', before='optUnprovenA', after='optUnprovenB')
        self.binding['tables']['work']['fields']['status']['type'] = 'select'
        self.inspected_native_field()
        write_json(self.project_path, self.binding)
        scope = self.scan()
        self.assertNotIn('native_stage_field_id', scope['tables']['tblWork'])
        self.receive('select', before='optUnmappedA', after='optUnmappedB')
        self.assertEqual(self.publish()['rows'], [])
        self.assertNotIn(b'optUnmappedA', self.journal.path.read_bytes())
        self.assertNotIn(b'optUnprovenA', self.journal.path.read_bytes())
        self.binding['tables']['work']['fields']['status']['type'] = 'stage'
        write_json(self.project_path, self.binding)
        self.row['fields']['fldProject'] = [{'id':'elsewhere'}]
        scope = self.scan()
        write_json(self.directory / gate_events.BATCH, saved)
        self.assertEqual(gate_events.pending(self.directory, scope, {})[0], [])

    def test_strict_worker_discovers_opaque_native_observation_once(self):
        actual = browser_watch.field(stage_field())
        actual['id'] = 'fldStatus'
        self.snapshot['stage_fields'] = {'tblWork/fldStatus': actual}
        fields = self.binding['tables']['work']['fields']
        fields.update(plan_confirmer=dict(id='fldOwner', type='user'), plan=dict(id='fldPlan', type='text'))
        fields['status']['stage_contract'] = {'steps': [
            dict(name='待确认计划', actor='human', person_field='plan_confirmer', required_fields=['plan']),
            dict(name='可开始', actor='ai', person_field=None, required_fields=[])], 'done':'已交付', 'cancelled':'已取消'}
        write_json(self.project_path, self.binding)
        self.assertEqual(self.tick()['status'], 'ran')
        self.assertEqual(self.scan()['tables']['tblWork']['native_stage_field_id'], 'fldStatus')
        self.receive('eventNative', before='optUnknownA', after='optUnknownB')
        self.publish()
        self.assertEqual(self.tick()['status'], 'ran')
        packet = json.loads((self.calls[-1]['dir'] / 'work-packet.json').read_text())
        items = json.loads(Path(packet['gate_event_observations_file']).read_text())['items']
        self.assertEqual([(i['record_id'], i['value_format'], i['before'], i['after']) for i in items],
                         [('recItem', 'native_stage_option_ids', 'optUnknownA', 'optUnknownB')])
        self.assertEqual(self.tick()['status'], 'unchanged')

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'optional service SDK is not installed')
    def test_real_sdk_passes_typed_fields_through_existing_receiver(self):
        import lark_oapi as sdk
        self.inspected_native_field()
        self.scan()
        event = self.event(before='optAlpha', after='optBeta')
        event['schema'] = '2.0'
        bridge.dispatcher(sdk, self.service, self.journal)._do_without_validation(json.dumps(event).encode())
        row, = self.publish()['rows']
        self.assertEqual((row['operator_open_id'], row['value_format'], row['before'], row['after']),
                         ('ou_person', 'native_stage_option_ids', 'optAlpha', 'optBeta'))

    def test_new_successful_scan_scope_and_changed_pins_fail_closed(self):
        self.scan()
        self.receive()
        batch = self.publish()
        self.row['fields']['fldProject'] = [{'id':'otherProject'}]
        scope = self.scan()
        self.assertEqual(gate_events.pending(self.directory, scope, {})[0], [])
        self.assertEqual(self.publish()['rows'], [])
        write_json(self.directory / gate_events.BATCH, dict(batch, signature='other'))
        self.assertEqual(gate_events.pending(self.directory, scope, {}), ([], None))
        self.config['event_tenant_key'] = 'other'
        self.save_config()
        with self.assertRaisesRegex(ValueError, 'tenant'):
            self.receive('foreign')
        self.config.update(event_tenant_key='tenant_test', workdir=str(self.root / 'other'))
        self.save_config()
        with self.assertRaisesRegex(ValueError, 'paths changed'):
            self.receive('otherRunner')

    def test_bounded_retention_and_restarted_receiver(self):
        self.scan()
        with mock.patch.object(gate_events, 'LIMIT', 3):
            for n in range(7):
                self.receive('evt'+str(n))
            self.journal = bridge.Journal(self.service)
            self.assertFalse(self.receive('evt6'))
            self.assertEqual([r['event_id'] for r in self.publish()['rows']], ['evt4','evt5','evt6'])

    def test_retained_evidence_survives_general_hint_id_eviction_without_redelivery(self):
        self.scan()
        with mock.patch.object(bridge, 'RETENTION', 2):
            self.receive('same')
            for n in range(5):
                self.receive('hint'+str(n), field_id='fldTitle')
            self.receive('same')
        self.assertEqual([(r['event_id'], r['generation']) for r in self.publish()['rows']], [('same', 1)])

    def test_supplement_is_never_needed_to_admit_or_ack_generic_hint(self):
        self.config.pop('event_tenant_key')
        self.save_config()
        self.assertTrue(self.receive())
        self.assertIsNone(self.scan())
        self.assertFalse((self.directory / gate_events.INDEX).exists())
        self.assertEqual(self.journal.status()['received'], 1)
