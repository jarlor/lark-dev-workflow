#!/usr/bin/env python3
"""Receive Feishu change hints and schedule the existing local project tick."""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import pwd
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'))
import gate_events
from onboard import atomic_json
import deferred_notices


RECORD = 'drive.file.bitable_record_changed_v1'
COMMENT = 'drive.notice.comment_add_v1'
RETENTION = 4096
PINS = ('app_id', 'tenant_key', 'bot_open_id', 'base_token', 'project_record_id', 'lark_profile')
TIMERS = {'reconcile_seconds': (600, 30, 86400), 'debounce_seconds': (5, 1, 300),
          'retry_seconds': (60, 5, 3600)}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def absolute(value):
    return isinstance(value, str) and Path(value).is_absolute()


def nonempty(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value == value.strip()


BEIJING = timezone(timedelta(hours=8))
DEFAULT_DIGEST_MINUTES = 60


def digest_window_open(timestamp):
    return 9 <= datetime.fromtimestamp(timestamp, BEIJING).hour < 22


def config_from(path):
    config = load(path)
    required = set(PINS) | {'table_ids', 'state_dir'}
    allowed = required | set(TIMERS) | {'mode', 'runner_config', 'tick_argv', 'human_digest', 'human_digest_minutes'}
    if not isinstance(config, dict) or not required <= config.keys() <= allowed:
        raise ValueError('Invalid service config keys.')
    if not all(nonempty(config[key]) for key in PINS) or not absolute(config['state_dir']):
        raise ValueError('Explicit identity pins and an absolute service state_dir are required.')
    tables = config['table_ids']
    if not isinstance(tables, list) or not tables or not all(nonempty(v) for v in tables):
        raise ValueError('table_ids must name the project tables.')
    if len(set(tables)) != len(tables):
        raise ValueError('Duplicate table IDs.')
    config.setdefault('mode', 'observe')
    if config['mode'] not in ('observe', 'dispatch'):
        raise ValueError('mode must be observe or dispatch.')
    digest = config.get('human_digest', False)
    if type(digest) is not bool or (digest and config['mode'] != 'dispatch'):
        raise ValueError('human_digest requires a boolean and an active dispatch service.')
    minutes = config.get('human_digest_minutes', DEFAULT_DIGEST_MINUTES)
    if type(minutes) is not int or minutes not in (5, 10, 30, 60):
        raise ValueError('human_digest_minutes must be 5, 10, 30 or 60.')
    for key, (default, minimum, maximum) in TIMERS.items():
        value = config.setdefault(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'Invalid {key}.')
    if config['mode'] == 'dispatch':
        argv = config.get('tick_argv')
        if (not absolute(config.get('runner_config')) or not isinstance(argv, list)
                or not argv or not all(nonempty(arg) for arg in argv) or not absolute(argv[0])
                or argv.count('{runner_config}') != 1 or not os.access(argv[0], os.X_OK)):
            raise ValueError('Specify the installed local tick command and an exact {runner_config} argument.')
    if 'runner_config' in config:
        if not absolute(config['runner_config']):
            raise ValueError('Runner path must be absolute.')
        runner_pins(config)
    return config


def runner_pins(config):
    runner = load(config['runner_config'])
    if runner.get('format_version') != 3 or runner.get('lark_profile') != config['lark_profile']:
        raise ValueError('Runner must use the selected assistant profile.')
    if runner.get('event_tenant_key', config['tenant_key']) != config['tenant_key']:
        raise ValueError('Runner event tenant differs from the paired service tenant.')
    paths = [runner.get(key) for key in ('workdir', 'state_dir', 'project_config')]
    if not all(absolute(path) for path in paths):
        raise ValueError('Runner binding and state paths must be absolute.')
    binding = load(paths[2])
    actor = {'identity': 'bot', 'profile': config['lark_profile'], 'app_id': config['app_id'],
             'open_id': config['bot_open_id']}
    if (binding.get('cli_actor') != actor or binding.get('base', {}).get('token') != config['base_token']
            or binding.get('scope', {}).get('project_record_id') != config['project_record_id']
            or not set(config['table_ids']) <= {t['id'] for t in binding['tables'].values()}):
        raise ValueError('Runner actor, Base or project scope does not match the event service.')
    return tuple(str(Path(path).resolve()) for path in paths)



def journal_identity(config):
    identity = [*(config[key] for key in PINS), sorted(config['table_ids'])]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def writer_identity(runner, paths):
    pinned = [str(Path(runner).resolve()), *paths]
    return hashlib.sha256(json.dumps(pinned).encode()).hexdigest()

class Journal:
    """Bounded duplicate suppression and optional, project-scoped gate observations."""

    def __init__(self, config):
        directory = Path(config['state_dir'])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o077 or directory.stat().st_uid != os.geteuid():
            raise ValueError('Service state directory must be private to the service account (0700).')
        self.path = directory / 'hints.sqlite'
        self.runner = config.get('runner_config')
        self.runner_paths = runner_pins(config) if self.runner else None
        self.started_at = int(time.time())
        signature = journal_identity(config)
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS seen (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                         'identity TEXT, generation INTEGER, ack INTEGER, last_check INTEGER, result TEXT)')
            conn.execute("INSERT OR IGNORE INTO state VALUES (1,?,0,0,0,'never')", (signature,))
            if conn.execute('SELECT identity FROM state').fetchone()[0] != signature:
                raise ValueError('The journal belongs to another assistant or project.')

            conn.execute('CREATE TABLE IF NOT EXISTS notices(kind TEXT PRIMARY KEY, total INTEGER, latest INTEGER)')
            conn.execute('CREATE TABLE IF NOT EXISTS writer(singleton INTEGER PRIMARY KEY, binding TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS receipt_source(singleton INTEGER PRIMARY KEY, id TEXT)')
            conn.execute('INSERT OR IGNORE INTO receipt_source VALUES(1,?)', (str(uuid.uuid4()),))
            conn.execute('CREATE TABLE IF NOT EXISTS receipts(seq INTEGER PRIMARY KEY, generation INTEGER, '
                         'binding TEXT, table_id TEXT, record_id TEXT, data TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS membership_barriers(table_id TEXT, record_id TEXT, '
                         'closed_at INTEGER, reopened_at INTEGER, PRIMARY KEY(table_id, record_id))')
            if self.runner:
                identity_hash = writer_identity(self.runner, self.runner_paths)
                conn.execute('INSERT OR IGNORE INTO writer VALUES (1,?)', (identity_hash,))
                if conn.execute('SELECT binding FROM writer').fetchone()[0] != identity_hash:
                    raise ValueError('Service state is already bound to another runner.')

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            yield connection

    def scope(self, config):
        if not self.runner:
            return None
        if runner_pins(config) != self.runner_paths:
            raise ValueError('Event receiver runner paths changed. Verify the new binding.')
        _, directory, project = self.runner_paths
        runner = load(self.runner)
        if not runner.get('event_tenant_key'):
            return None
        binding = load(project)
        signature = gate_events.signature(binding, config['tenant_key'])
        try:
            index = load(Path(directory) / gate_events.INDEX)
        except FileNotFoundError:
            return None
        if (index.get('signature') == signature and type(index.get('observed_at')) is int
                and type(index.get('started_at')) is int
                and int(time.time()) - gate_events.MAX_AGE <= index['observed_at']):
            return dict(index, project_scope=binding['scope'])

    @staticmethod
    def evidenced_after(conn, table_id, record_id, index):
        """An observed move requires a full scan that started after it arrived."""
        barrier = conn.execute('SELECT closed_at,reopened_at FROM membership_barriers '
                               'WHERE table_id=? AND record_id=?', (table_id, record_id)).fetchone()
        if barrier is None:
            return 0
        closed, reopened = barrier
        if reopened is None:
            if index['started_at'] <= closed:
                return None
            reopened = index['observed_at']
            conn.execute('UPDATE membership_barriers SET reopened_at=? WHERE table_id=? AND record_id=?',
                         (reopened, table_id, record_id))
        return reopened

    def mark(self, event_id, kind=RECORD, *, index=None, table_id=None, moved=(), changes=(), own=False):
        digest = hashlib.sha256(event_id.encode()).hexdigest()
        with self.connect() as conn:
            if own:
                changes = [c for c in changes if c.get('value_format') == gate_events.ASSISTANT_MARKER]
            if own and not moved and not changes:
                return False
            added = conn.execute('INSERT OR IGNORE INTO seen(id) VALUES (?)', (digest,))
            if not added.rowcount:
                return False
            conn.execute('DELETE FROM seen WHERE seq <= ?', (added.lastrowid - RETENTION,))
            if moved:
                closed = int(time.time())
                conn.executemany('INSERT OR REPLACE INTO membership_barriers VALUES (?,?,?,NULL)',
                                 [(table_id, record_id, closed) for record_id in moved])
                conn.executemany('DELETE FROM receipts WHERE table_id=? AND record_id=?',
                                 [(table_id, record_id) for record_id in moved])
                count, = conn.execute('SELECT count(*) FROM membership_barriers').fetchone()
                if count > RETENTION:
                    # Overflow cannot erase an invalidation and re-enable a stale member.
                    conn.execute('DELETE FROM membership_barriers')
                    conn.execute("INSERT INTO membership_barriers VALUES ('','',?,NULL)", (closed,))
            if not own:
                conn.execute('UPDATE state SET generation=generation+1')
            if changes:
                generation = 0 if own else conn.execute('SELECT generation FROM state').fetchone()[0]
                reset_after = self.evidenced_after(conn, '', '', index)
                allowed = []
                if reset_after is not None:
                    # A quiet project's evidence can outlive the general hint-ID window.
                    retained = (json.loads(row[0]) for row in conn.execute(
                        'SELECT data FROM receipts WHERE binding=? AND table_id=?', (index['signature'], table_id)))
                    keys = {(row['event_id'], row['record_id'], row['field_id']) for row in retained}
                    for change in changes:
                        if (event_id, change['record_id'], change['field_id']) in keys:
                            continue
                        record_after = self.evidenced_after(conn, table_id, change['record_id'], index)
                        if record_after is not None and change['update_time'] > max(reset_after, record_after):
                            allowed.append((generation, index['signature'], table_id, change['record_id'],
                                            json.dumps(change)))
                conn.executemany('INSERT INTO receipts(generation,binding,table_id,record_id,data) VALUES(?,?,?,?,?)',
                                 allowed[-gate_events.LIMIT:])
                # Generation zero holds only the assistant's own marker evidence.
                # Keep its retention separate from human decisions and do not wake a worker for it.
                conn.execute('DELETE FROM receipts WHERE (generation=0)=? AND seq NOT IN '
                             '(SELECT seq FROM receipts WHERE (generation=0)=? ORDER BY seq DESC LIMIT ?)',
                             (own, own, gate_events.LIMIT))
            if own:
                return False
            conn.execute('INSERT OR IGNORE INTO notices VALUES (?,0,0)', (kind,))
            conn.execute('UPDATE notices SET total=total+1, latest=? WHERE kind=?', (int(time.time()), kind))
        return True

    def publish(self, config, directory, generation):
        index = self.scope(config)
        if not index:
            return
        with self.connect() as conn:
            journal_id, = conn.execute('SELECT id FROM receipt_source').fetchone()
            rows = conn.execute('SELECT generation,data FROM receipts WHERE binding=? AND generation>0 AND generation<=? '
                                'ORDER BY seq', (index['signature'], generation)).fetchall()
        filtered = []
        for row_generation, payload in rows:
            row = json.loads(payload)
            table = index['tables'].get(row['table_id'])
            if (table and row['record_id'] in table['records'] and row['field_id'] in table['fields']
                    and (row.get('value_format') != gate_events.NATIVE_STAGE_IDS or
                         row['field_id'] == table.get('native_stage_field_id'))
                    and row['update_time'] > table['since'][row['record_id']]):
                filtered.append(dict(row, generation=row_generation))
        atomic_json(Path(directory) / gate_events.BATCH, dict(signature=index['signature'], journal_id=journal_id,
                    generation=generation, rows=filtered))

    def status(self):
        with self.connect() as conn:
            row = conn.execute('SELECT generation,ack,last_check,result FROM state').fetchone()
            status = dict(zip(('received', 'acknowledged', 'last_tick_epoch', 'last_tick_status'), row))
            status['notices'] = {kind: {'received': total, 'last_epoch': latest}
                                 for kind, total, latest in conn.execute('SELECT kind,total,latest FROM notices')}
            status['retained_gate_changes'] = conn.execute('SELECT count(*) FROM receipts').fetchone()[0]
            return status

    def finish(self, generation, status):
        with self.connect() as conn:
            conn.execute('UPDATE state SET ack=max(ack,?),last_check=?,result=?',
                         (generation if status in ('ran', 'unchanged') else 0, int(time.time()), status))


def field(value, key):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def file_pin_matches(notice, event, key, expected):
    values = [value for value in (field(notice, key), field(event, key)) if value not in (None, '')]
    return bool(values) and all(value == expected for value in values)


def project_link_retained(before, after, field_id, scope):
    """A full record image can repeat the link on an unrelated edit."""
    old = [field(v, 'field_value') for v in before if field(v, 'field_id') == field_id]
    new = [field(v, 'field_value') for v in after if field(v, 'field_id') == field_id]
    if not old and not new:
        return True
    if len(old) != 1 or old != new or not isinstance(old[0], str) or len(old[0]) > 8192:
        return False
    raw = old[0]
    project = scope['project_record_id']
    if raw == project:
        return True
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if value == project:
        return True
    # Native Base field types 18 and 21 serialize the linked record IDs with this key.
    if value == {'link_record_ids': [project]}:
        return True
    # Native record reads can include display text alongside table_id and record_ids.
    return (isinstance(value, dict) and set(value) <= {'table_id', 'record_ids', 'text', 'type'}
            and value.get('table_id') == scope['project_table_id'] and value.get('record_ids') == [project])


def gate_changes(event, event_id, table_id, open_id, table, listening_since, scope):
    members = set(table['records'])
    moved, changes = set(), {}
    revision, timestamp = field(event, 'revision'), field(event, 'update_time')
    for action in field(event, 'action_list') or []:
        record_id = field(action, 'record_id')
        if not isinstance(record_id, str) or record_id not in members:
            continue
        before = field(action, 'before_value') or []
        after = field(action, 'after_value') or []
        action_name = field(action, 'action')
        if (action_name == 'record_deleted'
                or (action_name != 'record_edited'
                    and any(field(v, 'field_id') == table['project_field'] for v in [*before, *after]))
                or not project_link_retained(before, after, table['project_field'], scope)):
            moved.add(record_id)
            continue
        if (action_name != 'record_edited' or not isinstance(open_id, str)
                or not re.fullmatch(r'ou_[a-zA-Z0-9_]{1,128}', open_id)
                or type(revision) is not int or revision <= 0 or type(timestamp) is not int
                or timestamp <= max(table.get('since', {}).get(record_id, sys.maxsize), listening_since,
                                    int(time.time()) - gate_events.MAX_AGE)
                or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event_id)):
            continue
        for field_id in table['fields']:
            old = [field(v, 'field_value') for v in before if field(v, 'field_id') == field_id]
            new = [field(v, 'field_value') for v in after if field(v, 'field_id') == field_id]
            if table['fields'][field_id] == gate_events.ASSISTANT_MARKER and not old:
                old = [None]  # The platform may omit an empty field in before_value.
            if len(old) != 1 or len(new) != 1:
                continue
            try:
                values = gate_events.changed_values(table, field_id, old[0], new[0])
            except (ValueError, TypeError):
                continue
            if values:
                change = dict(event_id=event_id, table_id=table_id, record_id=record_id, field_id=field_id,
                              operator_open_id=open_id, event_revision=revision, update_time=timestamp, **values)
                key = (record_id, field_id)
                if key not in changes:
                    changes[key] = change
                elif changes[key] != change:
                    changes[key] = None
    return moved, [change for change in changes.values() if change]


def receive(envelope, config, journal):
    header, event = field(envelope, 'header'), field(envelope, 'event')
    event_id, kind = field(header, 'event_id'), field(header, 'event_type')
    if (field(header, 'app_id') != config['app_id'] or field(header, 'tenant_key') != config['tenant_key']
            or not nonempty(event_id)):
        return False
    if kind == RECORD:
        table_id = field(event, 'table_id')
        actor = field(field(event, 'operator_id'), 'open_id')
        if (field(event, 'file_type') != 'bitable' or field(event, 'file_token') != config['base_token']
                or table_id not in config['table_ids']):
            return False
        index = journal.scope(config)
        if index and table_id in index['tables']:
            table = index['tables'][table_id]
            if actor == config['bot_open_id'] and config.get('mode') == 'dispatch':
                import workflow_rule
                table = workflow_rule.marker_watch(config, journal.runner_paths, table_id, table)
            moved, changes = gate_changes(event, event_id, table_id, actor, table,
                                          journal.started_at, index['project_scope'])
            return journal.mark(event_id, kind, index=index, table_id=table_id, moved=moved, changes=changes,
                                own=(actor == config['bot_open_id']))
        if actor == config['bot_open_id']:
            return False
    elif kind == COMMENT:
        notice = field(event, 'notice_meta')
        if (not file_pin_matches(notice, event, 'file_token', config['base_token'])
                or not file_pin_matches(notice, event, 'file_type', 'bitable')
                or not nonempty(field(event, 'comment_id'))
                or field(field(notice, 'to_user_id'), 'open_id') != config['bot_open_id']
                or field(field(notice, 'from_user_id'), 'open_id') == config['bot_open_id']
                or field(notice, 'notice_type') not in ('add_comment', 'add_reply')):
            return False
    else:
        return False
    return journal.mark(event_id, kind)


def dispatcher(sdk, config, journal):
    def handle(envelope):
        receive(envelope, config, journal)
    return (sdk.EventDispatcherHandler.builder('', '')
            .register_p2_drive_file_bitable_record_changed_v1(handle)
            .register_p2_customized_event(COMMENT, handle).build())


def service_secret():
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    secret = ((Path(directory) / 'feishu_app_secret').read_text(encoding='utf-8').strip()
              if directory else os.environ.get('FEISHU_APP_SECRET'))
    if not secret:
        raise ValueError('Missing service credential feishu_app_secret.')
    return secret


def socket_client(config, journal):
    import lark_oapi as sdk
    sdk.ws.Client(config['app_id'], service_secret(), event_handler=dispatcher(sdk, config, journal),
                  log_level=sdk.LogLevel.CRITICAL).start()


def pair_dispatcher(sdk, pins, emit):
    def handle(envelope):
        header, event = field(envelope, 'header'), field(envelope, 'event')
        tenant = field(header, 'tenant_key')
        if (field(header, 'event_type') != RECORD or field(header, 'app_id') != pins['app_id']
                or not nonempty(field(header, 'event_id')) or not isinstance(tenant, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', tenant)
                or field(event, 'file_type') != 'bitable' or field(event, 'file_token') != pins['base_token']
                or field(event, 'table_id') != pins['table_id']):
            return
        actions = field(event, 'action_list')
        if isinstance(actions, list) and any(nonempty(field(action, 'record_id'))
                and field(action, 'action') in ('record_added', 'record_edited', 'record_deleted')
                for action in actions):
            emit(tenant)
    return sdk.EventDispatcherHandler.builder('', '').register_p2_drive_file_bitable_record_changed_v1(handle).build()


def pair_receiver(pins, sender, timeout):
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    # Also time out if the calling terminal is killed before it can clean up.
    signal.alarm(timeout + 5)
    try:
        secret = service_secret()
    except (OSError, ValueError):
        sender.send_bytes(b'!credential_unavailable')
        return
    try:
        import lark_oapi as sdk
    except ImportError:
        sender.send_bytes(b'!sdk_unavailable')
        return
    sent = False
    def emit(tenant):
        nonlocal sent
        if not sent:
            sender.send_bytes(tenant.encode('ascii'))
            sent = True
    try:
        sdk.ws.Client(pins['app_id'], secret, event_handler=pair_dispatcher(sdk, pins, emit),
                      log_level=sdk.LogLevel.CRITICAL).start()
    except Exception:
        if not sent:
            sender.send_bytes(b'!receiver_failed')


def pair(pins, timeout, child_entry=pair_receiver):
    if (set(pins) != {'app_id', 'base_token', 'table_id'} or not all(nonempty(val) for val in pins.values())
            or not pins['app_id'].startswith('cli_') or not pins['table_id'].startswith('tbl')
            or not 1 <= timeout <= 900):
        raise ValueError('Require an app, a test Base and table, and 1–900 seconds.')
    ctx = multiprocessing.get_context('spawn')
    reader, writer = ctx.Pipe(duplex=False)
    child = ctx.Process(target=child_entry, args=(pins, writer, timeout), daemon=True)
    def interrupt(*_):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        child.start()
        writer.close()
        if not reader.poll(timeout):
            return {'status': 'timed_out'}
        try:
            result = reader.recv_bytes(256)
        except EOFError:
            return {'status': 'receiver_exited'}
        if result in (b'!credential_unavailable', b'!sdk_unavailable', b'!receiver_failed'):
            return {'status': result[1:].decode('ascii')}
        if not re.fullmatch(rb'[A-Za-z0-9_-]{1,256}', result):
            return {'status': 'receiver_failed'}
        return {'status': 'paired', 'tenant_key': result.decode('ascii')}
    except KeyboardInterrupt:
        return {'status': 'interrupted'}
    finally:
        reader.close()
        writer.close()
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join(timeout=2)
            if child.is_alive():
                child.kill()
                child.join()
            child.close()
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def exclusive(path):
    with path.open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another service owns this project or state directory.') from None
        yield stream


class Schedule:
    def __init__(self, config):
        self.config = config
        self.next_check = 0
        self.hint_not_before = 0

    def due(self, snapshot, now):
        if self.config['mode'] != 'dispatch':
            return False
        return now >= self.next_check or (snapshot['received'] > snapshot['acknowledged']
                                          and now >= self.hint_not_before)

    def finished(self, status, now):
        ok = status in ('ran', 'unchanged')
        self.next_check = now + self.config['reconcile_seconds' if ok else 'retry_seconds']
        self.hint_not_before = now + self.config['debounce_seconds'] if ok else self.next_check


def tick_status(stream, returncode):
    if returncode != 0 or stream.tell() > 1024 * 1024:
        return 'failed'
    stream.seek(0)
    try:
        status = json.load(stream)['status']
    except (KeyError, TypeError, ValueError, UnicodeError):
        return 'unknown'
    return status if status in ('ran', 'unchanged', 'busy', 'paused', 'recovery_required') else 'unknown'


def tick_message(status):
    """Return a short journal message for the person watching the service."""
    messages = {
        'ran': '项目检查：已完成一轮处理。',
        'unchanged': '项目检查：没有新的事项。',
        'busy': '项目检查：上一轮还在处理，暂不重复启动。',
        'paused': '项目检查：当前已暂停，没有执行操作。',
        'recovery_required': '项目检查：需要先恢复上次未完成的工作。',
        'failed': '项目检查失败：本轮没有完成，稍后会重试。',
        'unknown': '项目检查：结果无法确认，请查看前后几行日志。',
    }
    return messages.get(status, '项目检查：结果无法确认，请查看前后几行日志。')


def worker_environment():
    env = {key: value for key, value in os.environ.items()
           if key not in ('FEISHU_APP_SECRET', 'CREDENTIALS_DIRECTORY')}
    if not env.get('HOME'):
        try:
            env['HOME'] = pwd.getpwuid(os.geteuid()).pw_dir
        except KeyError:
            raise OSError('Service account has no registered home directory.') from None
    if not absolute(env['HOME']):
        raise OSError('Service account home must be an absolute directory.')
    return env


def retry_after_tick(path, config, paths, status, listener):
    if config.get('human_digest'):
        return None
    if status not in ('ran', 'unchanged') or listener is None or listener.poll() is not None:
        return None
    try:
        if next(deferred_notices.requests(config['state_dir']), None) is None:
            return None
        return subprocess.Popen([sys.executable, str(Path(deferred_notices.__file__).resolve()),
                                 '--config', str(path), '--pin', deferred_notices.binding_pin(config, paths)],
                                cwd=paths[0], env=worker_environment(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL)
    except (OSError, ValueError):
        print('计划提醒没有启动，原请求已保留，稍后会重试。', file=sys.stderr, flush=True)
        return None


class DigestSchedule:
    """Start on daytime time boundaries independently of the project tick."""

    def __init__(self, path, config, paths):
        self.command = [sys.executable, str(Path(__file__).with_name('human_digest.py')),
                        '--config', str(path), '--pin', deferred_notices.binding_pin(config, paths)]
        self.workdir = paths[0]
        self.interval = config.get('human_digest_minutes', DEFAULT_DIGEST_MINUTES) * 60
        self.process = None
        self.slot = None
        self.retry_at = 0

    def poll(self, wall_time, monotonic):
        if self.process is not None:
            result = self.process.poll()
            if result is None:
                return
            self.process = None
            if result:
                self.retry_at = monotonic + 300
                print('待办汇总没有完成，5 分钟后会重试。', file=sys.stderr, flush=True)
        slot, offset = divmod(int(wall_time), self.interval)
        if not digest_window_open(wall_time) or (self.slot is None and offset >= 60):
            self.slot, self.retry_at = slot, 0
            return
        if self.slot == slot and (not self.retry_at or monotonic < self.retry_at):
            return
        self.slot, self.retry_at = slot, 0
        try:
            self.process = subprocess.Popen(self.command, cwd=self.workdir, env=worker_environment(),
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except OSError:
            self.retry_at = monotonic + 300
            print('待办汇总没有启动，5 分钟后会重试。', file=sys.stderr, flush=True)

    def stop(self):
        if self.process is not None:
            self.process.wait()


def supervise(path, config, journal):
    stop = False
    def request_stop(*_):
        nonlocal stop
        stop = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    expected = runner_pins(config) if config['mode'] == 'dispatch' else None
    schedule = Schedule(config)
    digest = DigestSchedule(path, config, expected) if config.get('human_digest') else None
    listener, worker, output, reminder = None, None, None, None
    next_socket = 0
    with contextlib.ExitStack() as locks:
        locks.enter_context(exclusive(Path(config['state_dir']) / 'listen.lock'))
        if expected:
            lockdir = Path(expected[0]) / '.lark-dev-workflow'
            if not Path(expected[0]).is_dir():
                raise ValueError('Bound workdir does not exist.')
            lockdir.mkdir(mode=0o700, exist_ok=True)
            locks.enter_context(exclusive(lockdir / 'event-service.lock'))
        try:
            while not stop:
                now = time.monotonic()
                if digest:
                    digest.poll(time.time(), now)
                if listener and listener.poll() is not None:
                    print('飞书连接已断开，准备重新连接。定期检查仍会继续。', file=sys.stderr, flush=True)
                    listener = None
                    next_socket = now + config['retry_seconds']
                if listener is None and now >= next_socket:
                    listener = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_receive',
                                                 '--config', str(path)], stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if reminder and reminder.poll() is not None:
                    reminder = None
                if worker and worker.poll() is not None:
                    status = tick_status(output, worker.returncode)
                    journal.finish(captured, status)
                    print(tick_message(status), file=sys.stderr, flush=True)
                    output.close()
                    output, worker = None, None
                    schedule.finished(status, now)
                    if not stop:
                        reminder = retry_after_tick(path, config, expected, status, listener)
                if expected and worker is None and reminder is None:
                    snapshot = journal.status()
                    if schedule.due(snapshot, now):
                        if config_from(path) != config or runner_pins(config) != expected:
                            raise ValueError('Binding changed. Stop and verify the service config.')
                        captured = snapshot['received']
                        journal.publish(config, expected[1], captured)
                        argv = [config['runner_config'] if arg == '{runner_config}' else arg
                                for arg in config['tick_argv']]
                        output = tempfile.TemporaryFile()
                        try:
                            worker = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                                      stderr=subprocess.DEVNULL, cwd=expected[0],
                                                      env=worker_environment())
                        except OSError:
                            output.close()
                            output = None
                            journal.finish(captured, 'failed_to_start')
                            schedule.finished('failed', now)
                            print('本轮处理没有启动，稍后会重试。', file=sys.stderr, flush=True)
                time.sleep(1)
        finally:
            if digest:
                digest.stop()
            if reminder:
                reminder.wait()
            if listener and listener.poll() is None:
                listener.terminate()
                try:
                    listener.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    listener.kill()
                    listener.wait()
            if worker:
                worker.wait()
                journal.finish(captured, tick_status(output, worker.returncode))
            if output:
                output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'status', 'listen', '_receive'):
        commands.add_parser(name).add_argument('--config', type=Path, required=True)
    pairing = commands.add_parser('pair', help='Bounded setup only: identify the tenant from a change in a test table.')
    for arg in ('app-id', 'base-token', 'table-id'):
        pairing.add_argument('--' + arg, required=True)
    pairing.add_argument('--timeout', type=int, default=300, metavar='SECONDS', help='1–900 seconds (default 300).')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'pair':
            result = pair({'app_id': args.app_id, 'base_token': args.base_token, 'table_id': args.table_id}, args.timeout)
            print(json.dumps(result))
            return 0 if result['status'] == 'paired' else 1
        path = args.config.resolve()
        config = config_from(path)
        if args.command == 'check':
            print('服务检查通过：配置有效。网络连接会在服务运行时建立。')
            return 0
        journal = Journal(config)
        if args.command == 'status':
            print(json.dumps({'mode': config['mode'], **journal.status()}))
        elif args.command == '_receive':
            with exclusive(Path(config['state_dir']) / 'receiver.lock') as lease:
                # gate_changes only accepts clicks later than this receiver's start second.
                lease.seek(0)
                lease.truncate()
                lease.write(f'{journal.started_at}\n')
                lease.flush()
                socket_client(config, journal)
        else:
            supervise(path, config, journal)
        return 0
    except (OSError, ImportError, KeyError, ValueError, TypeError, sqlite3.Error) as exc:
        # Includes SDK credential errors. No event payload, subprocess output or secret enters the journal.
        print(f'服务操作“{args.command}”失败（{type(exc).__name__}）。请检查配置、权限和依赖。',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
