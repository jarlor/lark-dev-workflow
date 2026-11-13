"""Check observed native Stage settings against portable logical field aliases.

This checks configuration, not the identity of the person clicking a node.
Business confirmation and read-back evidence still belong to the worker.
"""


def problems(field, contract, fields):
    issues = []
    if (not isinstance(contract, dict) or set(contract) != {'steps', 'done', 'cancelled'}
            or not isinstance(contract['steps'], list) or not contract['steps']
            or not isinstance(contract['done'], str) or not contract['done'].strip()
            or not isinstance(contract['cancelled'], str) or not contract['cancelled'].strip()):
        return ['流程约定缺少步骤、完成名称或取消名称。']
    steps = contract['steps']
    if any(not isinstance(s, dict) or set(s) != {'name', 'actor', 'person_field', 'required_fields'}
           or not isinstance(s['name'], str) or not s['name'].strip()
           or s['actor'] not in ('human', 'ai')
           or not (s['person_field'] is None or isinstance(s['person_field'], str))
           or not isinstance(s['required_fields'], list)
           or any(not isinstance(k, str) or not k for k in s['required_fields']) for s in steps):
        return ['流程步骤的名称、操作人或必填字段约定不完整。']
    names = [s['name'] for s in steps] + [contract['done'], contract['cancelled']]
    if len(names) != len(set(names)):
        return ['流程步骤、完成和取消名称不能重复。']
    observed = field.get('stage_config', {}).get('property', {}).get('options')
    if not isinstance(observed, list) or any(not isinstance(o, dict) for o in observed):
        return ['没有读到真实流程节点配置。']
    defaults = [o for o in observed if o.get('type') == 'Default']
    if [o.get('name') for o in defaults] != [s['name'] for s in steps]:
        issues.append('流程步骤及顺序与约定不一致。')
    for kind, name in [('EndDone', contract['done']), ('EndCancel', contract['cancelled'])]:
        if [o.get('name') for o in observed if o.get('type') == kind] != [name]:
            issues.append('流程的完成或取消节点与约定不一致。')
    by_name = {o.get('name'): o for o in defaults}
    for step in steps:
        node = by_name.get(step['name'])
        if node is None:
            continue
        prefix = step['name'] + '：'
        person = step['person_field']
        if person is not None:
            mapped_person = fields.get(person, {})
            if mapped_person.get('type') != 'user' or not mapped_person.get('id'):
                issues.append(prefix + '完成人必须映射到同表的人员字段。')
                continue
            people = [mapped_person['id']]
        else:
            people = []
        required = step['required_fields']
        if len(required) != len(set(required)) or any(k not in fields for k in required):
            issues.append(prefix + '必填字段没有完整映射。')
            continue
        required_ids = {fields[k].get('id') for k in required}
        if None in required_ids:
            issues.append(prefix + '必填字段缺少真实编号。')
            continue
        rule = node.get('transitionRule')
        if not isinstance(rule, dict):
            issues.append(prefix + '没有读到节点完成条件。')
            continue
        if rule.get('type') != ('ConditionAndPerson' if people else 'Condition') or rule.get('permissionFieldIds', []) != people:
            issues.append(prefix + '谁能完成此步骤与约定不一致。')
        condition = rule.get('condition')
        conditions = condition.get('conditions') if isinstance(condition, dict) else []
        if not isinstance(conditions, list):
            issues.append(prefix + '未读到完整的必填条件。')
            continue
        valid_conditions = all(isinstance(c, dict) and c.get('operator') == 'isNotEmpty'
                               and c.get('value') is None and isinstance(c.get('fieldId'), str)
                               for c in conditions)
        if ((conditions and (not isinstance(condition, dict) or condition.get('conjunction') != 'and'))
                or not valid_conditions or {c.get('fieldId') for c in conditions if isinstance(c, dict)} != required_ids
                or len(conditions) != len(required_ids)):
            issues.append(prefix + '完成前的必填检查与约定不一致。')
        displayed = node.get('fieldsConfigInfo')
        if (not isinstance(displayed, list) or any(not isinstance(x, dict) or not x.get('id') for x in displayed)
                or not required_ids <= {x.get('id') for x in displayed if isinstance(x, dict)}):
            issues.append(prefix + '必填字段没有在此步骤中展示。')
    return issues
