#!/usr/bin/env python3
"""Read the current project and send each person their pending actions at the configured interval."""
import argparse
import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

import event_bridge as bridge
import deferred_notices
import human_actions
import run_context
import stage_action
import unattended
import unattended_control as control
from onboard import OnboardingError
import watch


SCAN_SECONDS = 300
MAX_BODY = 24_000


class DigestError(ValueError):
    pass


def check(condition, reason):
    if not condition:
        raise DigestError(reason)


class Project:
    def __init__(self, path, pin, preview):
        self.path = path
        self.config = bridge.config_from(path)
        check(self.config['mode'] == 'dispatch' and (preview or self.config.get('human_digest') is True),
              '本项目尚未启用定时私信。')
        self.paths = bridge.runner_pins(self.config)
        check(deferred_notices.binding_pin(self.config, self.paths) == pin, '服务与启动时的项目绑定不符。')
        self.runner = control.load_config(self.config['runner_config'])
        self.binding = bridge.load(self.paths[2])
        self.actor = control.actor_for_runner(self.runner, self.binding)
        self.directory = Path(self.config['state_dir'])
        info = self.directory.lstat()
        check(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
              '提醒收据需要本服务账号的私有目录。')
        self.unchanged()

        def invoke(argv, **kwargs):
            kwargs['env'] = dict(kwargs.pop('env', os.environ), PATH=self.runner['path'],
                                 LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
            return subprocess.run(argv, **kwargs)

        self.reader = watch.Cli(self.actor['profile'], executable=self.runner['lark_bin'], actor=self.actor,
                                runner=invoke, scan_timeout=SCAN_SECONDS)

    def unchanged(self):
        check(bridge.config_from(self.path) == self.config and bridge.runner_pins(self.config) == self.paths
              and control.load_config(self.config['runner_config']) == self.runner
              and bridge.load(self.paths[2]) == self.binding and not (Path(self.paths[1]) / 'PAUSED').exists(),
              '服务、执行器、项目绑定或暂停状态已变化。')

    def recent_checkpoint(self):
        directory = unattended.execution_directory(self.runner)
        try:
            state = unattended.load_state(directory / 'state.json', unattended.identity(self.runner, self.binding))
            scanned = int(datetime.fromisoformat(state['last_scan_at']).timestamp())
            scope = bridge.load(Path(self.paths[1]) / bridge.gate_events.INDEX)
            started, ended = scope['started_at'], scope['observed_at']
            now = time.time()
            if (state.get('inflight') or state.get('recovery_note') or not state.get('checkpoint')
                    or state['checkpoint'] != state.get('last_observed_snapshot')
                    or scope['signature'] != bridge.gate_events.signature(self.binding, self.config['tenant_key'])
                    or not now - 180 <= started <= scanned <= ended <= now or ended - scanned > 1):
                return None
            db = self.directory / 'hints.sqlite'
            with contextlib.closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                result = conn.execute('SELECT identity,generation=ack,result,(SELECT MAX(latest) FROM notices),'
                                      '(SELECT binding FROM writer WHERE singleton=1) FROM state WHERE singleton=1').fetchone()
            if not result or (result[0] != bridge.journal_identity(self.config) or not result[1]
                              or result[2] != 'unchanged' or (result[3] or 0) >= started or
                              result[4] != bridge.writer_identity(self.config['runner_config'], self.paths)):
                return None
            snapshot = run_context.checkpoint_snapshot(directory, state)
            if (snapshot and snapshot.get('binding') == watch.comments.binding_identity(self.binding)
                    and isinstance(snapshot.get('comments'), dict) and isinstance(snapshot.get('records'), dict)):
                return snapshot, now - started
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, unattended.RunnerError):
            return None

    def current_actions(self):
        # The digest never holds the business worker's lock or advances its comment checkpoint.
        cached = self.recent_checkpoint()
        if cached:
            snapshot, age = cached
        else:
            age = 0
            inspect = any(field['type'] == 'stage' for table in self.binding['tables'].values()
                          for field in table['fields'].values())
            with tempfile.TemporaryDirectory(prefix='lark-digest-') as directory:
                snapshot = watch.collect_with_reader(self.binding, self.reader,
                                                     comments_state_path=Path(directory) / 'comments.json',
                                                     inspect_native_stages=inspect)
        self.unchanged()
        return human_actions.by_recipient(self.binding, snapshot), age

    def command(self, args, purpose, *, preview=False):
        result = self.reader.runner(['lark-cli', *args, '--as', 'bot', '--profile', self.actor['profile']],
                                    capture_output=True, text=True, timeout=30)
        try:
            reply = json.loads(result.stdout if result.returncode == 0 else result.stderr or result.stdout)
        except (TypeError, ValueError):
            raise DigestError(f'{purpose}未返回有效回执（退出码 {result.returncode}）。') from None
        if result.returncode or reply.get('ok') is not True:
            error = reply.get('error', {})
            code = error.get('code') or error.get('subtype') or result.returncode
            scopes = error.get('missing_scopes', [])
            scope = ', '.join(s for s in scopes if isinstance(s, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]+', s))
            raise DigestError(f'{purpose}失败（{code}）' + (f'，缺少权限 {scope}' if scope else '') + '。')
        check(reply.get('identity') == 'bot' and (reply.get('dry_run') is True) == preview
              and isinstance(reply.get('data'), dict), f'{purpose}的机器人身份或回执格式不符。')
        return reply['data']

    def confirm_person(self, person):
        check(isinstance(person, str) and re.fullmatch(r'ou_[A-Za-z0-9_]{1,128}', person)
              and person != self.actor['open_id'], '收件人编号不属于可核实的人员。')
        if person in self.verified_people():
            return
        data = self.command(['api', 'GET', '/open-apis/contact/v3/users/' + person,
                             '--params', '{"user_id_type":"open_id"}'], '用当前助手核对收件人')
        check(data.get('user', {}).get('open_id') == person, '通讯录中没有与收件人匹配的人员账号。')

    def verified_people(self):
        # These private receipts have already passed the worker's original human identity
        # checks. They identify an account; only the current snapshot assigns a message.
        if hasattr(self, '_verified_people'):
            return self._verified_people
        folder = self.directory / 'stage-confirmations'
        people = set()
        if folder.is_dir():
            stage_action.private(folder, stat.S_ISDIR)
            signature = stage_action.digest([stage_action.rules.service_pins(self.config, self.paths), self.binding])
            for path in folder.glob('*.json'):
                stage_action.private(path, stat.S_ISREG)
                book = bridge.load(path)
                if book.get('binding') != signature:
                    continue
                for seal in book.get('confirmations', []):
                    proof = seal['proof']
                    role = proof.get('role')
                    if role not in ('requirements', 'bugs', 'work') or proof.get('status') != 'verified':
                        continue
                    table = self.binding['tables'][role]
                    fields = table['fields']
                    receiver = fields['plan_confirmer' if role == 'work' else 'confirmer']['id']
                    person = proof.get('operator_open_id')
                    method = proof.get('check')
                    original = (method == 'current_record_human_transition' and proof.get('event_id')
                                and (seal.get('event') or {}).get('operator_open_id') == person
                                and seal['event'].get('event_id') == proof['event_id'])
                    recovered = (method in ('archived_runner_history', 'official_history_profile')
                                 and re.fullmatch(r'[a-f0-9]{64}', seal.get('source_report_sha256', '')))
                    if ((original or recovered) and proof['project_record_id'] == self.config['project_record_id']
                            and proof['table_id'] == table['id'] and proof['field_id'] == fields['status']['id']
                            and path.name == f"{table['id']}-{proof['record_id']}.json"
                            and seal['fields'].get(receiver) == [person]
                            and seal['fields'].get(fields['project']['id']) == [proof['project_record_id']]
                            and seal['anchor']['rev'] == proof['table_revision']
                            and seal['anchor']['create_time'] == proof['update_time']):
                        people.add(person)
        self._verified_people = people
        return people

    def message(self, person, body, key, *, preview, before_send):
        args = ['im', '+messages-send', '--user-id', person, '--text', body, '--idempotency-key', key]
        data = self.command([*args, '--dry-run'], '私信预览', preview=True)
        calls = data.get('api')
        check(data.get('context', {}).get('app_id') == self.actor['app_id']
              and isinstance(calls, list) and len(calls) == 1, '私信预览的应用或请求数不符。')
        expected_body = {'receive_id': person, 'uuid': key, 'msg_type': 'text'}
        call = calls[0]
        actual = dict(call.get('body', {}))
        content = json.loads(actual.pop('content', 'null'))
        check(call.get('method') == 'POST' and call.get('url') == '/open-apis/im/v1/messages'
              and call.get('params') == {'receive_id_type': 'open_id'} and actual == expected_body
              and content == {'text': body}, '私信预览的接收人、文字或去重编号不符。')
        self.unchanged()
        before_send()
        if preview:
            return None
        data = self.command(args, '发送机器人私信')
        receipt = data.get('message_id')
        check(isinstance(receipt, str) and re.fullmatch(r'om_[A-Za-z0-9_]+', receipt),
              '飞书尚未返回可核实的私信编号。')
        return receipt


def single_line(text, limit):
    line = ' '.join(text.split()).replace('<', '＜').replace('>', '＞')
    return line if len(line) <= limit else line[:limit - 1] + '…'


def entry(item):
    return (f"{single_line(item['number'], 32)}　{single_line(item['title'], 80)}\n"
            f"{single_line(item['action'], 300)}\n{item['url']}")


def render(binding, items):
    heading = f"{single_line(binding['project_name'], 60)} · 有 {len(items)} 项等你处理"
    body = '\n\n'.join([heading, *(entry(item) for item in items)])
    if len(body.encode()) <= MAX_BODY:
        return body
    # Keep one message even for a large backlog. Use the bound tables and record numbers
    # for the overflow, without including another person's assignments.
    lines = [heading]
    groups = {}
    for item in items:
        groups.setdefault(item['table_id'], []).append(item)
    for table_id, rows in groups.items():
        numbers = '、'.join(single_line(item['number'], 32) for item in rows)
        lines.extend((f"{len(rows)} 项 · {numbers}", binding['base']['url'] + '?table=' + table_id))
    compact = '\n'.join(lines)
    if len(compact.encode()) <= MAX_BODY:
        return compact
    lines = [heading]
    for table_id, rows in groups.items():
        lines.append(f"{len(rows)} 项 · {single_line(rows[0]['number'], 32)} 等"
                     + '\n' + binding['base']['url'] + '?table=' + table_id)
    return '\n\n'.join(lines)


class Ledger:
    def __init__(self, connection, window):
        self.db = connection
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS digest_delivery (service TEXT, slot INTEGER, person TEXT, '
                            'body_hash TEXT NOT NULL, message_id TEXT, PRIMARY KEY(service,slot,person))')
            self.db.execute('DELETE FROM digest_delivery WHERE slot < ?', (window - 30 * 86400,))

    def pending(self, route, body):
        # Persist the exact body hash before POST. A lost response retries only the identical
        # message with the same UUID. A changed list is reconsidered in the next interval.
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO digest_delivery VALUES (?,?,?,?,NULL)', (*route, digest))
            record = self.db.execute('SELECT body_hash,message_id FROM digest_delivery '
                                     'WHERE service=? AND slot=? AND person=?', route).fetchone()
        return record == (digest, None)

    def ack(self, route, message_id):
        with self.db:
            self.db.execute('UPDATE digest_delivery SET message_id=? WHERE service=? AND slot=? AND person=?',
                            (message_id, *route))


def deliver(project, groups, slot, ledger, *, preview=False, clock=time.time, deadline=None):
    interval = project.config.get('human_digest_minutes', 30) * 60
    report = {'status': 'preview' if preview else 'complete', 'slot': slot, 'recipients': [],
              'messages': 0, 'errors': []}
    # Stable across restarts, directory moves, CLI upgrades and changes to polling settings.
    identity = [project.config[key] for key in ('app_id', 'tenant_key', 'base_token', 'project_record_id')]
    service = json.dumps([*identity, interval])

    def still_current():
        check(int(clock()) // interval * interval == slot and (deadline is None or time.monotonic() < deadline),
              '本次读取已超过发送时间，请按当前事项重新核对。')

    for person, items in sorted(groups.items()):
        if not items:
            continue
        route = (service, slot, person)
        try:
            check(all(item['table_id'] in project.config['table_ids'] for item in items),
                  '待办所属表超出当前服务范围。')
            body = render(project.binding, items)
            if not preview and not ledger.pending(route, body):
                continue
            still_current()
            project.reader.start_read_window()
            project.confirm_person(person)
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(['lark-human-digest-v1', *route])))
            receipt = project.message(person, body, key, preview=preview, before_send=still_current)
            if preview:
                report['recipients'].append({'open_id': person, 'text': body})
            else:
                ledger.ack(route, receipt)
                report['recipients'].append({'open_id': person, 'message_id': receipt, 'items': len(items)})
            report['messages'] += 1
        except (OSError, KeyError, AttributeError, ValueError, TypeError, sqlite3.Error,
                subprocess.SubprocessError, OnboardingError) as exc:
            report['errors'].append({'open_id': person, 'reason': str(exc) if isinstance(exc, DigestError)
                                     else f'私信处理未完成（{type(exc).__name__}）。'})
    if report['errors']:
        report['status'] = 'partial'
    return report


def run(path, pin, *, preview=False):
    project = Project(path, pin, preview)
    with contextlib.ExitStack() as stack:
        if not preview:
            stack.enter_context(bridge.exclusive(project.directory / 'human-digest.lock'))
        started = time.monotonic()
        groups, age = project.current_actions()
        interval = project.config.get('human_digest_minutes', 30) * 60
        slot = int(time.time()) // interval * interval
        ledger = None
        if not preview:
            connection = stack.enter_context(contextlib.closing(sqlite3.connect(project.directory / 'human-digest.sqlite')))
            ledger = Ledger(connection, slot)
        return deliver(project, groups, slot, ledger, preview=preview, deadline=started + SCAN_SECONDS + 60 - age)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--pin', required=True)
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = run(args.config, args.pin, preview=args.preview)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, sqlite3.Error,
            subprocess.SubprocessError, OnboardingError) as exc:
        result = {'status': 'failed', 'reason': str(exc) if isinstance(exc, (ValueError, OnboardingError))
                  else type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] in ('complete', 'preview') else 1


if __name__ == '__main__':
    raise SystemExit(main())
