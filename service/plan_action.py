#!/usr/bin/env python3
"""Prepare one plan and deliver its confirmation request from an authorized local project worker."""
import argparse
import json
from pathlib import Path
import sys

import plan_notice
import prepare_plan


def run(config, requirement, revision, confirmer, *, draft=None, reconcile=False, confirm=None):
    if reconcile:
        plan = prepare_plan.get_prepared(config, requirement)
        if plan['status'] == 'prepared' and (
            plan['requirement_revision'] != revision or plan['confirmer'] != confirmer
        ):
            return {'status': 'conflict', 'reason': '查回的计划不属于这版需求或指定确认人。',
                    'record_id': plan['record_id'], 'plan_revision': plan['plan_revision']}
    else:
        plan = prepare_plan.prepare(config, requirement, revision, confirmer, draft, confirm=confirm)
    if plan['status'] != 'prepared':
        return {'status': 'waiting' if plan['status'] == 'pending' else 'conflict', 'reason': plan['reason'],
                'record_id': plan.get('record_id'), 'plan_revision': plan.get('plan_revision')}

    record, plan_revision = plan['record_id'], plan['plan_revision']
    notice = plan_notice.run(config, record, confirmer, plan_revision)
    if notice['status'] == 'scheduled_digest':
        return dict(notice, status='prepared', notice='scheduled_digest')
    if notice['status'] == 'notified':
        return {'status': 'prepared', 'notice': 'notified', 'record_id': record, 'plan_revision': plan_revision,
                'comment_id': notice['comment_id'], 'reply_id': notice['reply_id'],
                'reason': '计划已写好，提醒已在研发项下读回。'}
    return {'status': 'prepared', 'notice': 'pending', 'record_id': record,
            'plan_revision': plan_revision, 'reason': notice['reason']}


def main(*, confirm=None):
    parser = argparse.ArgumentParser(description='为已确认的需求准备研发计划，并在研发项下提醒计划确认人。')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--requirement', required=True)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--confirmer', required=True)
    parser.add_argument('--reconcile', action='store_true', help='只查回已创建的计划，核对并补齐提醒。')
    args = parser.parse_args()
    draft = None
    if not args.reconcile:
        try:
            raw = sys.stdin.read(64001)
            if len(raw) > 64000:
                raise ValueError('draft too long')
            draft = json.loads(raw)
        except (ValueError, OSError):
            print(json.dumps({'status': 'conflict', 'reason': '请通过标准输入提供四项计划说明的 JSON。'},
                             ensure_ascii=False))
            return 1
    result = run(args.config, args.requirement, args.revision, args.confirmer, draft=draft,
                 reconcile=args.reconcile, confirm=confirm)
    print(json.dumps(result, ensure_ascii=False))
    return int(result['status'] == 'conflict')


if __name__ == '__main__':
    sys.exit(main())
