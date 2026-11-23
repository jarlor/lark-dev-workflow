"""Bind a runner to the six AI-owned transitions installed on its own project."""
from pathlib import Path
import sys

from unattended_control import ControlError, load_config

MODE = 'registered_requests'
# Bump only when a changed stage check can unblock a previous waiting result.
STAGE_CHECK_REVISION = 5
SERVICE = Path(__file__).resolve().parents[3] / 'service'


def selected(config, binding, *, runner_path=None, verify_live=False):
    if not (SERVICE / 'workflow_rule.py').is_file():
        raise ControlError('办理助手请求需要同一发布包里的 service 和 skill 两个目录。')
    if str(SERVICE) not in sys.path:
        sys.path.insert(0, str(SERVICE))
    import event_bridge as bridge
    import workflow_rule as rules

    service_path = config['stage_service_config']
    service = bridge.config_from(service_path)
    paths = bridge.runner_pins(service)
    runner = Path(service['runner_config']).resolve()
    if (Path(rules.__file__).resolve().parent != SERVICE or service['mode'] != 'dispatch'
            or load_config(runner) != config or bridge.load(paths[2]) != binding
            or (runner_path is not None and runner != Path(runner_path).resolve())):
        raise ControlError('服务、执行器与项目绑定不一致。')
    registry = rules.private_registry(service, paths)
    expected = {}
    for name, rule in registry.items():
        if rule.get('purpose') != rules.STAGE_REQUEST:
            continue
        key = rule.get('key')
        rules.checked_rule(rule)
        role = rule['role']
        table = binding['tables'][role]
        if (key in expected or rule.get('definition_sha256') is None
                or rule.get('authored_by') != service['bot_open_id']
                or rule['guard'] != {'kind':'project_link','field_id':table['fields']['project']['id'],
                                      'value':service['project_record_id']}
                or rule['table_id'] != table['id'] or rule['table_id'] not in service['table_ids']):
            raise ControlError('每个助手动作须对应本项目唯一且已固定的飞书规则。')
        rules.source_step(rule, table['fields'])
        if verify_live:
            proof = rules.verify(service_path, name)
            if not (proof.get('status') == 'checked' and proof.get('enabled') is True
                    and proof.get('definition_sha256') == rule['definition_sha256']
                    and proof.get('definition_pinned') and proof.get('project_condition_verified')
                    and proof.get('trigger_modifier_verified') and proof.get('new_marker_value_verified')
                    and proof.get('updates_trigger_row_only')
                    and (role != 'work' or proof.get('fixed_bot_owner_verified'))):
                raise ControlError('飞书未能确认这条已启用的助手规则：' + name)
        expected[key] = {'registration': name, 'workflow_id': rule['workflow_id'],
                         'definition_sha256': rule['definition_sha256']}
    if (set(expected) != set(rules.STAGE_REQUESTS)
            or len({r['workflow_id'] for r in expected.values()}) != len(expected)):
        raise ControlError('请登记并分别核实需求、Bug 和研发项的六条助手规则。')
    return {'native_stage_mode': MODE, 'actions': expected, 'direct_stage_writes': False,
            'scope_plan_acceptance_by_human': True}


def retain_clicks(config, binding, snapshot, events):
    """Save only observed scope and plan confirmations before the worker can add links."""
    from stage_business import confirmation_for

    candidates = set()
    for event in events:
        table, record = event['table_id'], event['record_id']
        row = snapshot['records'].get(table + '/' + record)
        if not row or row['role'] not in ('requirements', 'bugs', 'work'):
            continue
        field = binding['tables'][row['role']]['fields']['status']['id']
        after = '可开始' if row['role'] == 'work' else '可研发'
        if field == event['field_id'] and row['fields'].get(field) == [after]:
            candidates.add((row['role'], record))
    results = []
    for role, record in sorted(candidates):
        evidence = confirmation_for(config['stage_service_config'], role, record)
        results.append({'role': role, 'record_id': record, 'status': evidence['status'],
                        **({'reason': evidence['reason']} if 'reason' in evidence else {})})
    return results
