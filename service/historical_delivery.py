"""Read an old enrolled browser worker's delivery only as a named work dependency."""
import re

from prepare_plan import digest, ids
from stage_action import Pending, require
import verify_confirmation as confirmation


SOURCES = {'owner_instruction', 'enrollment', 'start_prompt', 'delivery_prompt', 'start_command', 'start_result',
           'delivery_command', 'delivery_result', 'start_before', 'start_after', 'delivery_before', 'delivery_after',
           'artifact', 'test_run', 'ci', 'original_test_record'}
TEST_FIELDS = ('key', 'build', 'executor', 'executed_at', 'purpose', 'method', 'environment', 'result',
               'criteria_snapshot', 'summary', 'evidence', 'work', 'project')


def check_sources(evidence, hashes):
    src = evidence.get('source_hashes')
    auxiliary = evidence.get('auxiliary_sha256')
    require(isinstance(src, dict) and set(src) == SOURCES and all(isinstance(s, str) and s in hashes for s in src.values())
            and isinstance(auxiliary, list) and auxiliary and all(isinstance(s, str) and s in hashes for s in auxiliary)
            and len(set(src.values()) | set(auxiliary)) == len(src) + len(auxiliary)
            and evidence.get('report_sha256') in hashes - set(src.values()) - set(auxiliary)
            and digest({k: v for k, v in evidence.items() if k != 'report_sha256'}) == evidence['report_sha256'],
            '缺少原授权、运行器身份与执行、产物或自测原件，或登记报告与原件不同。')


def history(project, row, record, approval, proof, entries):
    """Only the two exact recorded machine steps, initial filing, and progress text may follow approval."""
    revision = approval['proof']['business_revision']
    actor = row['owner'][0]['name']
    require(entries and entries[-1] == approval['anchor'] and approval['proof']['table_revision'] == entries[-1]['rev'],
            '这次历史交付不属于原计划确认。')
    steps = [('可开始', '进行中', proof.get('start')), ('进行中', '已交付', proof.get('delivery'))]
    status = project.fields['status']['id']
    operational = {project.fields[k]['id'] for k in ('next_step', 'summary') if k in project.fields}
    filing = {project.fields[k]['id']: k for k in ('plan_confirmation', 'confirmed_plan_revision') if k in project.fields}
    seen, pos = set(), 0
    for item in reversed(entries[:-1]):
        changes = item.get('field_changes'); require(item.get('activity_type') == 'update' and isinstance(changes, list)
            and len(changes) == 1, '此历史依赖在确认后有混合或未识别的改动。')
        change = changes[0]; fid = change.get('field_id')
        if fid in operational:
            require(change.get('field_type') == 'Text', '历史进度字段类型发生变化。')
        elif fid in filing:
            k = filing[fid]; confirmation.check_history_author(item, actor)
            require(pos == 0 and fid not in seen and change.get('before') in ('', None) and change.get('after') == row[k]
                    and ((k == 'plan_confirmation' and change.get('field_type') == 'Text' and confirmation.inline_text(row[k]))
                         or (k == 'confirmed_plan_revision' and change.get('field_type') == 'Number'
                             and type(row[k]) is int and row[k] == revision)), '原计划的归档栏不是开工前首次补记。')
            seen.add(fid)
        else:
            require(fid == status and pos < len(steps), '历史交付后或原确认后改过计划、人员、关联或未知字段。')
            before, after, step = steps[pos]; confirmation.check_history_author(item, actor)
            require(isinstance(step, dict) and step.get('history_sha256') == digest(item)
                    and step.get('revision') == item['rev'] and step.get('time') == item['create_time']
                    and change.get('field_type') == 'Stage' and (change.get('before'), change.get('after')) == (before, after),
                    '旧执行器的这次操作与飞书的行、版本、时间或节点不符。')
            pos += 1
    require(pos == 2 and row['status'] == ['已交付'], '未核实同一版本的完整开工和交付。')


def verify(business, project, work, key, dependent, proof):
    try:
        return _verify(business, project, work, key, dependent, proof)
    except confirmation.Pending as exc:
        raise Pending(str(exc)) from exc


def _verify(business, project, work, key, dependent, proof):
    import stage_action
    import stage_business
    require(key == project.key == 'work-deliver' and business.key in ('work-start', 'work-deliver')
            and isinstance(dependent, str) and dependent != work and dependent == proof.get('consumer_work_id')
            and work in business.links('work', dependent, 'dependencies', 'work')
            and (proof.get('project_id'), proof.get('table_id'), proof.get('record_id')) == (project.project, project.table, work),
            '只可核实指定研发项直接依赖的历史交付，不可用于本项动作、送验或验收。')
    row = business.row('work', work); revision, human = stage_business.validate_row(project, 'work', row)
    require(ids(row['owner']) == [human] and human == proof.get('account_open_id') != project.bot
            and row['owner'][0].get('name') == row['plan_confirmer'][0].get('name')
            and type(proof.get('plan_revision')) is int and proof['plan_revision'] == revision
            and proof.get('plan_sha256') == digest(row['plan']) and proof.get('stage_field_id') == project.fields['status']['id']
            and proof.get('executor_type') == 'enrolled_browser_worker'
            and proof.get('granted_actions') == ['work-start', 'work-deliver']
            and type(proof.get('authorized_at')) is int and proof['authorized_at'] > 0,
            '原登录账户、执行器的授权范围、负责人或计划版本不符。')
    # This method may never establish a human plan approval. Always query the independently
    # enrolled original-click source and recheck its person mapping against an official live reply.
    first = stage_action.Project(project.path, 'work-start'); require(first.identity == project.identity, '计划绑定已变。')
    approval = stage_business.archived_click(first, 'work', work, row, stage_business.protected_fields('work', first.fields, row))
    require(approval['proof']['check'] == 'archived_runner_history' and approval['proof']['operator_open_id'] == human
            and approval['source_report_sha256'] == proof.get('original_approval_sha256'), '缺少独立的原始人工计划确认。')
    steps = [proof.get('start'), proof.get('delivery')]
    require(all(isinstance(s, dict) and isinstance(s.get('run_id'), str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', s['run_id'])
                and type(s.get('revision')) is int and type(s.get('time')) is int for s in steps)
            and proof['authorized_at'] < approval['proof']['update_time'] < steps[0]['time'] < steps[1]['time'],
            '原授权、人工确认及执行时序不符。')
    history(project, row, work, approval, proof, project.history(work, approval['proof']['table_revision']))
    rid, commit = proof.get('test_id'), proof.get('commit'); require(isinstance(rid, str) and re.fullmatch(r'rec[A-Za-z0-9]+', rid)
            and isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit), '缺少实际测试记录或固定提交。')
    test = business.row('verification', rid)
    require(business.links('verification', rid, 'work', 'work') == [work] and test['project'] and test['purpose'] == ['研发自测']
            and test['method'] == ['自动化测试'] and test['result'] == ['通过'] and test['environment'] == ['本地']
            and isinstance(test.get('build'), str) and re.search(r'(?<![a-f0-9])'+commit+r'(?![a-f0-9])', test['build'])
            and all(confirmation.inline_text(test.get(k)) for k in ('executor', 'criteria_snapshot', 'summary', 'evidence'))
            and proof.get('test_row_sha256') == digest({k: test[k] for k in TEST_FIELDS})
            and steps[0]['time'] * 1000 <= business.timestamp(rid, test) <= steps[1]['time'] * 1000,
            '原自测的固定提交、执行者、标准、结果、时间或关联不再相符。')
    return [steps[1]['revision'], proof['report_sha256'], approval['source_report_sha256'], rid, commit]
