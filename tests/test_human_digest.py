import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import human_digest as digest


def action(number):
    return {'role': 'work', 'table_id': 'tblA', 'record_id': 'rec' + str(number), 'kind': 'plan',
            'number': f'WORK-{number}', 'title': '检查科研账号', 'action': '请核对计划，确认后点击完成节点。',
            'url': 'https://example.feishu.cn/base/baseA?table=tblA&record=rec' + str(number)}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.slot = 3600  # 09:00 in Beijing
        self.project = digest.Project.__new__(digest.Project)
        self.project.actor = {'identity': 'bot', 'app_id': 'cli_test', 'profile': 'private', 'open_id': 'ou_bot'}
        self.project.directory = Path(self.temp.name)
        self.project.binding = {'project_name': '研发', 'base': {'url': 'https://example.feishu.cn/base/baseA'}}
        self.project.config = {'app_id': 'cli_test', 'tenant_key': 'tenant', 'base_token': 'baseA',
                               'project_record_id': 'recProject', 'table_ids': ['tblA']}
        self.project.unchanged = Mock()
        self.project.reader = Mock()
        self.project.reader.runner.side_effect = self.cli
        self.groups = {'ou_a': [action(1), action(2)], 'ou_b': [action(3)]}
        self.calls = []
        self.timeout = None
        self.break_preview = False
        self.block_contact = None

    def cli(self, argv, **opts):
        self.assertIn('--as', argv)
        self.assertEqual(argv[argv.index('--as') + 1], 'bot')
        self.assertEqual(argv[argv.index('--profile') + 1], 'private')
        self.assertGreater(opts['timeout'], 0)
        def success(data, **extra):
            return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'identity': 'bot', 'data': data, **extra}))
        if argv[1] == 'api':
            target = argv[3].split('/')[-1]
            if target == self.block_contact:
                return subprocess.CompletedProcess(argv, 1, '', json.dumps({'ok': False, 'error': {'code': 41050}}))
            return success({'user': {'open_id': target}})
        person = argv[argv.index('--user-id') + 1]
        self.assertEqual(argv[argv.index('--msg-type') + 1], 'post')
        text = argv[argv.index('--content') + 1]
        key = argv[argv.index('--idempotency-key') + 1]
        if '--dry-run' in argv:
            return success({'context': {'app_id': 'cli_test'}, 'api': [{
                'method': 'POST', 'url': '/open-apis/im/v1/messages', 'params': {'receive_id_type': 'open_id'},
                'body': {'receive_id': 'ou_wrong' if self.break_preview else person,
                         'msg_type': 'post', 'content': text, 'uuid': key}}]}, dry_run=True)
        self.calls.append((person, key, text))
        if self.timeout == person:
            raise subprocess.TimeoutExpired('im', 30)
        return success({'message_id': 'om_' + str(len(self.calls))})

    def send(self, slot=None, preview=False, clock=None):
        slot = self.slot if slot is None else slot
        db = sqlite3.connect(Path(self.temp.name) / 'digest.sqlite')
        try:
            return digest.deliver(self.project, self.groups, slot, digest.Ledger(db, slot),
                                  preview=preview, clock=clock or (lambda: slot + 1))
        finally:
            db.close()

    def test_development_execution_actions_are_never_sent_to_humans(self):
        self.groups = {'ou_a': [dict(action(1), kind='work', role='work')]}
        self.assertEqual(self.send()['messages'], 0)
        self.assertEqual(self.calls, [])

    def test_recipient_privacy_and_receipts_survive_restart(self):
        self.assertEqual(self.send()['messages'], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('WORK-1', self.calls[0][2])
        self.assertIn('WORK-2', self.calls[0][2])
        self.assertNotIn('WORK-3', self.calls[0][2])
        self.assertNotIn('WORK-2', self.calls[1][2])
        self.assertEqual(self.send()['messages'], 0)
        self.groups.pop('ou_b')
        self.assertEqual(self.send(slot=self.slot + 3600)['messages'], 1)
        self.assertNotEqual(self.calls[0][1], self.calls[-1][1])
        self.groups.clear()
        self.assertEqual(self.send(slot=self.slot + 7200)['messages'], 0)

    def test_timeout_replays_only_identical_payload_and_uuid_and_preserves_other_acks(self):
        self.timeout = 'ou_a'
        self.assertEqual(self.send()['status'], 'partial')
        self.timeout = None
        self.assertEqual(self.send()['messages'], 1)
        self.assertEqual(self.calls[0], self.calls[2])
        self.assertEqual([person for person, _, _ in self.calls], ['ou_a', 'ou_b', 'ou_a'])
        self.timeout = 'ou_a'
        self.send(slot=self.slot + 3600)
        self.timeout = None
        self.groups['ou_a'][1]['action'] = '经核对后执行新操作'
        self.assertEqual(self.send(slot=self.slot + 3600)['messages'], 0)
        self.assertEqual(self.send(slot=self.slot + 7200)['messages'], 2)

    def test_preview_and_invalid_targets_never_deliver(self):
        report = self.send(preview=True)
        self.assertEqual(report['status'], 'preview')
        self.assertEqual(report['messages'], 2)
        self.assertEqual(self.calls, [])
        self.break_preview = True
        self.assertEqual(self.send()['messages'], 0)
        self.break_preview = False
        self.groups['ou_bot'] = [action(5)]
        self.block_contact = 'ou_a'
        result = self.send()
        self.assertEqual(result['messages'], 1)
        self.assertEqual(len(result['errors']), 2)
        self.assertIn('41050', result['errors'][0]['reason'])
        self.assertEqual(self.calls[0][0], 'ou_b')

    def test_ordinary_execution_is_never_sent_as_a_human_digest_item(self):
        # This is the stale-snapshot case: an old read may still say that a
        # person owns the work after the live record was handed to the assistant.
        self.groups = {'ou_a': [dict(action(1), kind='work',
                                     action='请按计划开工。开始后，在这条记录里写下进展。')]}
        report = self.send(preview=True)
        self.assertEqual(report['messages'], 0)
        self.assertEqual(report['recipients'], [])
        self.assertEqual(self.calls, [])

    def test_changed_binding_during_preview_or_slot_change_suppresses_post(self):
        self.project.unchanged.side_effect = digest.DigestError('绑定已变')
        self.assertEqual(self.send()['status'], 'partial')
        self.project.unchanged.side_effect = None
        self.groups.pop('ou_b')
        self.assertEqual(self.send(clock=Mock(side_effect=[self.slot, self.slot + 3600]))['status'], 'partial')
        self.assertEqual(self.calls, [])

    def test_quiet_hours_cover_the_last_check_before_post(self):
        self.groups.pop('ou_b')
        last_slot = 13 * 3600  # 21:00 in Beijing
        clock = Mock(side_effect=[last_slot + 3599, last_slot + 3600])
        self.assertEqual(self.send(slot=last_slot, clock=clock)['messages'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.send(slot=last_slot + 3600, preview=True)['messages'], 1)
        self.assertEqual(self.calls, [])

    def test_quiet_run_skips_project_scan(self):
        self.project.current_actions = Mock()
        with patch.object(digest, 'Project', return_value=self.project), patch.object(digest.time, 'time', return_value=0):
            self.assertEqual(digest.run('private.json', 'pin')['status'], 'quiet_hours')
            self.project.current_actions.assert_not_called()

    def test_configured_intervals_and_project_namespaces(self):
        self.groups.pop('ou_b')
        for minutes in [5, 10, 30, 60]:
            self.project.config['human_digest_minutes'] = minutes
            self.assertEqual(self.send()['messages'], 1)
            self.assertEqual(self.send()['messages'], 0)
        self.project.config['project_record_id'] = 'recElsewhere'
        self.assertEqual(self.send()['messages'], 1)
        self.assertEqual(len({item[1] for item in self.calls}), 5)

    def test_summary_starts_with_action_and_each_item_has_its_own_link(self):
        first, second = self.groups['ou_a']
        first.update(kind='clarify', role='feedback', title='旧反馈里的长标题与今天这个问题无关' * 4,
                     action='请发一下预览页截图，图片和按钮都拍进去。如果按钮是灰色的，也请告诉我。')
        report = self.send(preview=True)
        post = report['recipients'][0]['post']['zh_cn']
        self.assertEqual(post['content'][0][0]['text'], '1. 请发一下预览页截图')
        self.assertNotIn(first['title'][:8], report['recipients'][0]['text'])
        self.assertIn('如果按钮是灰色的', report['recipients'][0]['text'])
        self.assertEqual([node['href'] for row in post['content'] for node in row if node['tag'] == 'a'],
                         [first['url'], second['url']])

    def test_long_summary_rotates_after_delivery_with_one_message_per_slot(self):
        self.groups = {'ou_a': [action(i) for i in range(24)]}
        expected = {item['url'] for item in self.groups['ou_a']}
        seen = set()
        with patch.object(digest, 'MAX_BODY', 1400):
            self.timeout = 'ou_a'
            self.assertEqual(self.send()['status'], 'partial')
            self.timeout = None
            self.assertEqual(self.send()['messages'], 1)
            self.assertEqual(self.calls[0], self.calls[1])
            for i in range(10):
                slot = self.slot + i * 3600
                if i:
                    self.assertEqual(self.send(slot=slot)['messages'], 1)
                self.assertEqual(self.send(slot=slot)['messages'], 0)
                body = json.loads(self.calls[-1][2])['zh_cn']
                links = [node['href'] for row in body['content'] for node in row if node['tag'] == 'a']
                self.assertIn(f'共 24 件待办，这次列出 {len(links)} 件', body['title'])
                self.assertEqual(len(links), len(set(links)))
                self.assertTrue(set(links) - seen)
                self.assertLessEqual(len(self.calls[-1][2].encode()), 1400)
                seen.update(links)
                if seen == expected:
                    break
        self.assertEqual(seen, expected)

    def test_disabled_project_and_changed_pin_abort_before_any_reads_or_messages(self):
        with patch.object(digest.bridge, 'config_from', return_value={'mode': 'dispatch'}), \
                self.assertRaisesRegex(digest.DigestError, '未启用'):
            digest.run('private.json', 'pin')
        with patch.object(digest.bridge, 'config_from', return_value={'mode': 'dispatch', 'human_digest': True}), \
                patch.object(digest.bridge, 'runner_pins', return_value=()), \
                self.assertRaisesRegex(digest.DigestError, '不符'):
            digest.run('private.json', 'wrongpin')

    def test_only_same_project_previously_verified_human_receipts_replace_directory_check(self):
        project = self.project
        folder = project.directory / 'stage-confirmations'
        folder.mkdir(mode=0o700)
        project.paths = ('/wi', '/state', '/binding')
        project.config.update(bot_open_id='ou_bot', lark_profile='private', runner_config='/runner')
        fields = {name: {'id': name} for name in ('confirmer', 'status', 'project')}
        project.binding['tables'] = {'requirements': {'id': 'tblA', 'fields': fields}}
        signature = digest.stage_action.digest([digest.stage_action.rules.service_pins(project.config, project.paths),
                                                project.binding])
        proof = dict(status='verified', role='requirements', check='archived_runner_history',
                     project_record_id='recProject', operator_open_id='ou_a', record_id='rec1',
                     table_id='tblA', field_id='status', table_revision=22, update_time=3600)
        seal = {'proof': proof, 'fields': {'confirmer': ['ou_a'], 'project': ['recProject']},
                'source_report_sha256': 'a' * 64, 'anchor': {'rev': 22, 'create_time': 3600}}
        book = {'binding': signature, 'confirmations': [seal]}
        file = folder / 'tblA-rec1.json'
        file.touch(mode=0o600)
        file.write_text(json.dumps(book))
        self.block_contact = 'ou_a'
        project.confirm_person('ou_a')
        project.reader.runner.assert_not_called()
        for changes in ({'binding': 'other-service'}, {'confirmations': [dict(seal, source_report_sha256='')]},
                        {'confirmations': [dict(seal, proof=dict(proof, record_id='recOther'))]},
                        {'confirmations': [dict(seal, fields={'confirmer': ['ou_other'], 'project': ['recProject']})]}):
            del project._verified_people
            file.write_text(json.dumps(dict(book, **changes)))
            with self.assertRaisesRegex(digest.DigestError, '41050'):
                project.confirm_person('ou_a')


if __name__ == '__main__':
    unittest.main()
