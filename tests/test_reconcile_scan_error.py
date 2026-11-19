"""A failed browser scan can be rechecked without executing or consuming new work."""
import copy
import functools
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

from test_unattended import RuntimeFixture, write_json
from test_browser_watch import browser, fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import reconcile_scan_error as repair
from reconcile_scan_error import runtime


class ReconcileScan(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.binding, access, self.payload = fixture()
        access['node_bin'] = sys.executable
        self.config.update(format_version=2, access=access)
        for key in ('lark_bin', 'lark_profile'):
            self.config.pop(key)
        self.save_config()
        write_json(self.project_path, self.binding)
        self.dir = Path(self.config['state_dir'])
        self.response = subprocess.CompletedProcess([], 0, json.dumps(self.payload), '')
        transport = mock.Mock(side_effect=lambda *a, **kw: self.response)
        patch = mock.patch.object(browser, 'collect', functools.partial(browser.collect, runner=transport))
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(runtime.tick(self.config_path, executor=self.execute)['status'], 'ran')
        last = self.state()['last_result']
        self.run = self.dir/'runs'/last['run_id']
        write_json(self.run/'result.json', last)
        events = [dict(type='thread.started', thread_id=self.session), dict(type='turn.started'),
                  dict(type='item.completed', item=dict(id='m', type='agent_message')),
                  dict(type='item.started', item=dict(id='c', type='command_execution', status='in_progress')),
                  dict(type='item.completed', item=dict(id='c', type='command_execution', status='failed')),
                  dict(type='turn.completed')]
        (self.run/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        self.response = subprocess.CompletedProcess([], 1, json.dumps(dict(code='RECORDS_INCOMPLETE')), '')
        failed = runtime.tick(self.config_path, executor=self.execute)
        self.assertEqual(failed['status'], 'failed')
        self.log = self.dir/'cron.log'
        self.log.write_text('old log\n'+json.dumps(failed)+'\nwrapper diagnostic\n')
        self.response = subprocess.CompletedProcess([], 0, json.dumps(self.payload), '')
        self.state_file = self.dir/'state.json'

    def test_real_original_reader_journals_only_error_and_retry_is_idempotent(self):
        old = self.state()
        files = {p:p.read_bytes() for p in (self.dir/'checkpoint-snapshot.json', self.dir/'browser-comments-state.json',
                                              self.run/'events.jsonl', self.run/'result.json')}
        review = repair.reconcile(self.config_path)
        self.assertEqual((review['status'], self.state()), ('preview', old))
        with self.assertRaisesRegex(ValueError, '已变化'):
            repair.reconcile(self.config_path, '0'*64)
        self.assertEqual(self.state(), old)
        with mock.patch.object(repair, 'sync_directory', side_effect=OSError('stopped after atomic state replacement')):
            with self.assertRaises(OSError):
                repair.reconcile(self.config_path, review['review_sha256'])
        self.assertEqual(repair.reconcile(self.config_path, review['review_sha256'])['status'], 'already_reconciled')
        result = self.state()
        entry, = result.pop(repair.AUDIT)
        self.assertEqual(entry['original_error'], old.pop('last_error'))
        self.assertEqual(result, old)
        self.assertEqual({p:p.read_bytes() for p in files}, files)
        self.assertEqual(len(self.calls), 1)

    def test_new_business_work_is_preserved_without_advancing_the_checkpoint(self):
        changed = copy.deepcopy(self.payload)
        table = next(t for t in changed['tables'].values() if t['rows'])
        fields = table['rows'][0]['cells']
        fid = next(f['id'] for f in table['fields'] if f['type'] == 1)
        fields[fid]['value'][0]['text'] += ' 更新'
        self.response.stdout = json.dumps(changed)
        old = self.state()
        review = repair.reconcile(self.config_path)
        self.assertNotEqual(review['scan_sha256'], old['checkpoint'])
        self.assertEqual(repair.reconcile(self.config_path, review['review_sha256'])['status'], 'reconciled')
        self.assertEqual(self.state()['checkpoint'], old['checkpoint'])
        self.assertEqual(self.state()['last_observed_snapshot'], old['last_observed_snapshot'])

    def test_refuses_unrecognized_error_inflight_missing_receipt_or_later_worker(self):
        old = self.state()
        for key, value in [('inflight', {'phase':'running'}), ('recovery_note','unknown'), ('pending_effects',['x']),
                           ('last_error',dict(old['last_error'], message='arbitrary failure'))]:
            with self.subTest(key=key):
                write_json(self.state_file, dict(old, **{key:value}))
                with self.assertRaises(ValueError):
                    repair.reconcile(self.config_path)
        write_json(self.state_file, old)
        original = self.log.read_bytes()
        self.log.write_text('{"status":"ran"}\n')
        with self.assertRaisesRegex(ValueError, '回执'):
            repair.reconcile(self.config_path)
        self.log.write_bytes(original)
        self.log.touch()
        newer=self.dir/'runs'/'another';newer.mkdir();(newer/'events.jsonl').touch()
        with self.assertRaisesRegex(ValueError, '其他执行'):
            repair.reconcile(self.config_path)

    def test_missing_checkpoint_or_open_operations_or_ambient_pause_cannot_be_cleared(self):
        old=self.state()
        (self.dir/'PAUSED').touch()
        with self.assertRaisesRegex(ValueError,'暂停'):
            repair.reconcile(self.config_path)
        (self.dir/'PAUSED').unlink()
        write_json(self.run/'REPORT.json', {'active_operations':['still running']})
        with self.assertRaisesRegex(ValueError,'未收尾'):
            repair.reconcile(self.config_path)
        (self.run/'REPORT.json').unlink()
        write_json(self.state_file,dict(old,checkpoint='0'*64))
        self.log.touch()
        with self.assertRaisesRegex(ValueError,'快照'):
            repair.reconcile(self.config_path)

    def test_original_completed_then_new_turn_or_unfinished_item_cannot_be_cleared(self):
        path = self.run / 'events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        open_item = dict(type='item.started', item=dict(id='next', type='command_execution', status='in_progress'))
        incomplete_completion = dict(type='item.completed', item=dict(id='next', type='command_execution', status='in_progress'))
        with self.subTest('appended turn'), self.assertRaisesRegex(ValueError, '还有一次运行'):
            path.write_text('\n'.join(json.dumps(e) for e in events + [dict(type='turn.started')]))
            repair.reconcile(self.config_path)
        for extra in ([open_item], [open_item, incomplete_completion]):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, '未结束|仍在等待'):
                path.write_text('\n'.join(json.dumps(e) for e in events[:-1]+extra+events[-1:]))
                repair.reconcile(self.config_path)
        self.assertIn('last_error', self.state())

    def test_idempotent_receipt_only_belongs_to_its_original_configuration(self):
        review = repair.reconcile(self.config_path)['review_sha256']
        self.assertEqual(repair.reconcile(self.config_path, review)['status'], 'reconciled')
        other = self.config_path.with_name('copied-config.json')
        other.write_bytes(self.config_path.read_bytes())
        with self.assertRaisesRegex(ValueError, '原配置不符'):
            repair.reconcile(other, review)
        self.assertEqual(repair.reconcile(self.config_path, review)['status'], 'already_reconciled')

    def test_real_workspace_lock_and_newer_checkpoint_without_write_evidence_block_review(self):
        state = self.state()
        with runtime.lock_workspace(self.config):
            with self.assertRaisesRegex(ValueError, '工作区正在使用'):
                repair.reconcile(self.config_path)
        checkpoint = self.dir / 'checkpoint-snapshot.json'
        changed = runtime.read_json(checkpoint)
        changed['comments'].clear()
        write_json(checkpoint, changed)
        state['checkpoint'] = runtime.run_context.snapshot_digest(changed)
        write_json(self.state_file, state)
        self.log.touch()
        with self.assertRaisesRegex(ValueError, '写入回执'):
            repair.reconcile(self.config_path)
        self.assertEqual(self.state(), state)

    def test_incomplete_browser_rescan_and_changed_binding_leave_old_error_intact(self):
        old=self.state()
        self.response=subprocess.CompletedProcess([],1,json.dumps({'code':'LOGIN_REQUIRED'}),'')
        with self.assertRaisesRegex(runtime.OnboardingError,'重新登录'):
            repair.reconcile(self.config_path)
        self.assertEqual(self.state(),old)
        self.response=subprocess.CompletedProcess([],0,json.dumps(self.payload),'')
        original=repair.read_original
        def replace_binding(*args):
            response=original(*args)
            write_json(self.project_path,dict(self.binding,project_key='something else'))
            return response
        with mock.patch.object(repair,'read_original',side_effect=replace_binding):
            with self.assertRaisesRegex(ValueError,'已改变'):
                repair.reconcile(self.config_path)
        self.assertEqual(self.state(),old)
