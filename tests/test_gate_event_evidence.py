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

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'optional service SDK is not installed')
    def test_real_sdk_passes_typed_fields_through_existing_receiver(self):
        import lark_oapi as sdk
        self.scan()
        event = self.event()
        event['schema'] = '2.0'
        bridge.dispatcher(sdk, self.service, self.journal)._do_without_validation(json.dumps(event).encode())
        self.assertEqual(self.publish()['rows'][0]['operator_open_id'], 'ou_person')

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
