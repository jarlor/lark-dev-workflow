#!/usr/bin/env python3
"""Retry one explicitly requested plan reminder after the single project runner has scanned."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


QUEUE = 'pending-plan-notices'
NAME = re.compile(r'[0-9a-f]{64}\.json')


def binding_pin(config, paths):
    payload = json.dumps([config, paths], sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def requests(directory):
    queue = Path(directory) / QUEUE
    try:
        mode = queue.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
        raise ValueError('The reminder queue must be a private directory of the service account.')
    for item in queue.iterdir():
        if NAME.fullmatch(item.name):
            yield item


def retry_one(config_path, pin):
    import event_bridge as bridge
    import plan_notice
    import unattended

    config = bridge.config_from(config_path)
    if config['mode'] != 'dispatch' or binding_pin(config, bridge.runner_pins(config)) != pin:
        return 'binding_changed'
    project = plan_notice.plan.Project(config_path)
    with unattended.lock_workspace(project.runner) as lock:
        if lock is None:
            return 'busy'
        project.unchanged()
        state_path = unattended.execution_directory(project.runner) / 'state.json'
        if not state_path.is_file():
            return 'no_completed_scan'
        try:
            unattended.other_mode_state(project.runner, project.binding)
            state = unattended.load_state(state_path, unattended.identity(project.runner, project.binding))
        except unattended.RunnerError:
            return 'runner_unsettled'
        if state.get('inflight') or state.get('recovery_note') or not state.get('checkpoint'):
            return 'runner_unsettled'

        pending = sorted(requests(project.directory))
        if not pending:
            return 'empty'
        # Advance even for an unreadable or stale request so it cannot starve another row.
        cursor = project.directory / 'plan-notice-retry-cursor.json'
        last = bridge.load(cursor) if cursor.exists() else ''
        path = next((p for p in pending if p.name > last), pending[0])
        bridge.atomic_json(cursor, path.name)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 8192):
            return 'unverified_request'
        request = bridge.load(path)
        record = request['anchor']['base_record_id']
        table = project.tables['work']['id']
        revision = request['revision']
        key = plan_notice.plan.digest([project.token, project.project, table, record, revision])
        if (path.stem != key or request['binding'] != project.signature
                or request['anchor']['block_id'] != table):
            return 'unverified_request'
        prefix = project.directory / ('plan-notice-' + key)
        if os.path.lexists(prefix.with_suffix('.intent')) and not prefix.with_suffix('.accepted').is_file():
            return 'previous_send_unconfirmed'
        # The sender re-reads the original request, current plan/recipient and send-once receipt.
        result = plan_notice.run(config_path, record, request['confirmer'], revision, expected_request=request)
        return result['status']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--pin', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        status = retry_one(args.config, args.pin)
        print('Plan reminder: ' + status, file=sys.stderr)
        return 0
    except (OSError, KeyError, ValueError, TypeError, AttributeError, IndexError):
        print('Could not verify the plan reminder. The original request is preserved.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
