"""Personal human actions in one complete, current project watch observation."""
from collections import defaultdict
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts'))
import comments
import onboard
import watch


GATES = {
    ('requirements', '待确认'): ('confirmer', 'scope', '请看看要改哪些地方、改好后应该什么样。跟你想的一样就{confirm}，有遗漏请在记录下留言。'),
    ('bugs', '待确认'): ('confirmer', 'scope', '请看看问题写得对不对、准备修哪些地方。如果都对，{confirm}。有遗漏请在记录下留言。'),
    ('work', '待确认计划'): ('plan_confirmer', 'plan', '请看「研发计划」里准备怎么做。同意就{confirm}，想改的地方请在记录下留言。'),
    ('requirements', '待验收'): ('acceptor', 'acceptance', '请按记录里的步骤试用这个功能。结果符合预期就{confirm}，遇到问题请在记录下留言。'),
    ('bugs', '待验收'): ('acceptor', 'acceptance', '请照原来的步骤再试一次。问题解决了就{confirm}，还会出现请在记录下留言。'),
}
# Resolved scope, plan and acceptance invitations must never be replayed from comments.
# A distinct question in the latest addressed comment may still need a reply.
ASK_FOR_INFO = re.compile(r'请.{0,8}(?:补充|提供|发一下|发来|贴一下|告诉|说明|回复)|[？?]')
GATE_INVITE = re.compile(r'完成节点|待确认|待验收|确认.{0,6}(?:范围|计划)|验收(?:通过|标准|步骤)|选.{0,4}负责人')
CLOSED = {'已撤回', '已归并', '已答复', '已取消', '无需修复', '已终止', '已交付'}
NO_COMMENT = object()


def short_text(value, limit):
    text = ' '.join(value.split()) if isinstance(value, str) else ''
    return text if len(text) <= limit else text[:limit - 1] + '…'


def human_id(value, bot):
    if isinstance(value, str) and value != bot and re.fullmatch(r'ou_[a-zA-Z0-9_]{1,128}', value):
        return value
    return None


def person(row, fields, name, bot):
    value = row.get(name)
    if (fields.get(name, {}).get('type') == 'user' and isinstance(value, list)
            and len(value) == 1 and isinstance(value[0], dict)):
        return human_id(value[0].get('id'), bot)
    return None


def latest_comments(snapshot, project, token):
    # Compare creation times, not last edit time. Editing an old question must not
    # outrank a later answer. Ambiguous timestamps never pick a recipient.
    indexed = {}
    for entry in snapshot['comments'].values():
        loc, content = entry['location'], entry['content']
        if (loc.get('status') != 'in_scope' or loc.get('project_record_id') != project
                or content.get('resource_type') != 'bitable' or content.get('resource_token') != token):
            continue
        key = (loc.get('table_id'), loc.get('record_id'))
        value = content.get('create_time')
        if type(value) is int and value > 0:
            previous, _ = indexed.get(key, (0, None))
            if value >= previous:
                indexed[key] = (value, entry if value > previous else None)
    return {key: entry for key, (_, entry) in indexed.items()}


def addressed_person(entry, bot):
    if not isinstance(entry, dict) or entry['thread'].get('is_solved') is not False:
        return None
    content = entry['content']
    if content.get('author') != bot:
        return None
    mentioned = {element.get('person', {}).get('user_id') for element in content['body']['elements']
                 if element.get('type') == 'person' and isinstance(element.get('person'), dict)}
    if len(mentioned) == 1:
        return human_id(mentioned.pop(), bot)
    return None


def instruction(value):
    # Drop introductory progress notes while preserving the actual question.
    text = ' '.join(value.split()) if isinstance(value, str) else ''
    sentences = re.split(r'(?<=[。！？!?])', text)
    start = next((i for i, phrase in enumerate(sentences) if ASK_FOR_INFO.search(phrase) or '请' in phrase), 0)
    return ''.join(sentences[start:]).strip()


def addressed_question(entry, bot):
    recipient = addressed_person(entry, bot)
    text = entry['content'].get('text') if recipient else ''
    # A query string in a record link isn't a question to its recipient.
    plain = re.sub(r'https?://[^\s，。！？]+', '', text or '')
    if plain and ASK_FOR_INFO.search(plain) and not GATE_INVITE.search(plain):
        return recipient, 'clarify', instruction(text)
    return None


def gate_action(role, stage, row, fields, bot):
    gate = GATES.get((role, stage))
    if not gate:
        return None
    name, kind, action = gate
    contract = fields['status'].get('stage_contract')
    if fields['status']['type'] == 'stage':
        step = next((s for s in (contract or {}).get('steps', []) if s['name'] == stage), {})
        if step.get('actor') != 'human' or step.get('person_field') != name:
            return None
        if any(not row.get(key) for key in step['required_fields']):
            return None
        confirm = f'在「{stage}」点「完成节点」'
    elif fields['status']['type'] == 'select':
        target = {'待确认': '可研发', '待确认计划': '可开始', '待验收': '已验收'}[stage]
        confirm = f'把状态改为「{target}」'
    else:
        return None
    recipient = person(row, fields, name, bot)
    return (recipient, kind, action.format(confirm=confirm)) if recipient else None


def row_action(role, row, fields, bot, latest, source_author):
    states = row.get('status')
    if not isinstance(states, list) or len(states) != 1:
        return None
    stage = states[0]
    if stage in CLOSED or row.get('merged_into') or row.get('processing') == ['暂停']:
        return None
    normal = row.get('processing') in (None, [], ['正常'])
    if (role, stage) in GATES:
        return gate_action(role, stage, row, fields, bot) if normal else None
    if row.get('human_action') is not True and (role, stage) != ('feedback', '待补充'):
        return None
    next_step = instruction(row.get('next_step'))
    question = addressed_question(latest, bot)
    if role == 'work' and stage in ('可开始', '进行中'):
        owner = person(row, fields, 'owner', bot)
        if normal and owner and next_step:
            if re.search(r'(?:选|指定|填写|补充|设置|确定).{0,8}负责人|负责人.{0,12}(?:选|填|指定)', next_step):
                # The live owner field already satisfies an older assignment request.
                next_step = ('请按计划开工。开始后，在这条记录里写下进展。' if stage == '可开始'
                             else '请在这条记录里写下已经做了什么，还有什么没做。')
            return owner, 'work', next_step
        # A cleared or subsequently changed assignment supersedes the prior @.
        return question if question and question[0] == owner else None
    if question and role == 'feedback':
        assigned = person(row, fields, 'author', bot)
        if (assigned and question[0] != assigned) or (source_author and question[0] != source_author):
            return None
    if question and stage == '已验收':
        acceptor = person(row, fields, 'acceptor', bot)
        if question[0] != acceptor:
            return None
    if question:
        return question
    if role == 'feedback' and stage == '待补充' and next_step and latest is NO_COMMENT:
        # Older projects store only an author name. It isn't an open ID.
        assigned = person(row, fields, 'author', bot)
        if assigned and source_author and assigned != source_author:
            return None
        if assigned or source_author:
            return assigned or source_author, 'clarify', next_step
    return None


def feedback_source_author(row, binding, snapshot, bot):
    key = row.get('source_event_key')
    entry = snapshot['comments'].get(key) if isinstance(key, str) else None
    if not entry:
        return None
    loc, content = entry['location'], entry['content']
    resource = {'type': content.get('resource_type'), 'token': content.get('resource_token')}
    if (key != comments.event_key(resource, content.get('comment_id', ''), content.get('reply_id', ''))
            or loc.get('status') != 'in_scope' or entry['thread'].get('is_solved') is not False):
        return None
    if resource['type'] == 'bitable':
        if (resource['token'] != binding['base']['token']
                or loc.get('project_record_id') != binding['scope']['project_record_id']):
            return None
    elif resource['type'] != 'docx' or not any(doc['token'] == resource['token'] for doc in binding['documents'].values()):
        return None
    return human_id(content.get('author'), bot)


def in_project(role, row, field, binding):
    scope = binding['scope']
    if field.get('type') != 'link' or field.get('target_table_id') != scope['project_table_id']:
        return False
    value = row.get('project')
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0].get('id') == scope['project_record_id']
    if (role == 'feedback' and value in (None, []) and watch.feedback_intake_mode(binding) == 'project_input'
            and row.get('status') in (['待补充'], ['待受理'])):
        number, selected = binding.get('project_number'), row.get('project_input')
        return (isinstance(number, str) and bool(number) and isinstance(selected, list)
                and len(selected) == 1 and isinstance(selected[0], str)
                and re.fullmatch(re.escape(number) + r'(?:\s*·\s*.+)?', selected[0]) is not None)
    return False


def linked(row, fields, field, target):
    if fields.get(field, {}).get('target_table_id') != target:
        return []
    links = row.get(field)
    if not isinstance(links, list) or not all(isinstance(v, dict) and isinstance(v.get('id'), str) for v in links):
        return []
    return [v['id'] for v in links]


def pending_manual_test(record, row, tables, rows, latest, bot):
    if (row.get('method') != ['人工核验'] or row.get('result') != ['未执行']
            or row.get('executed_at')):
        return None
    parents = {('需求验收',): ('requirements', 'requirements', 'verification'),
               ('Bug复测',): ('bugs', 'retested_bugs', 'retest_tests')}
    relation = parents.get(tuple(row.get('purpose') or []))
    if not relation:
        return None
    role, parent_link, test_link = relation
    parent_table = tables[role]
    tests = tables['verification']
    parent_ids = linked(row, tests['fields'], parent_link, parent_table['id'])
    if len(parent_ids) != 1:
        return None
    key = parent_table['id'], parent_ids[0]
    parent = rows.get(key, (None, {}))[1]
    if (parent.get('merged_into') or parent.get('processing') == ['暂停']
            or record not in linked(parent, parent_table['fields'], test_link, tests['id'])):
        return None
    acceptor = person(parent, parent_table['fields'], 'acceptor', bot)
    if parent.get('status') == ['待验收']:
        # The parent's invitation already directs its named acceptor to this work.
        executor = person(row, tests['fields'], 'executor', bot)
        if executor and executor != acceptor:
            return executor, 'verification', '请照这条记录里的步骤试一次，再写下你做了什么、看到了什么。'
        return None
    number = row.get('number')
    if parent.get('status') != ['已验收'] or not acceptor or not isinstance(number, str) or not number:
        return None
    exact_number = re.compile(r'(?<![A-Za-z0-9-])' + re.escape(number) + r'(?![A-Za-z0-9-])')
    next_step = parent.get('next_step') or ''
    invitation = latest.get(key)
    evidence_gap = re.compile(r'(?:试用|测试|步骤|结果|证据)[^。！？]{0,24}(?:待补|待提供|需补)|(?:待补|请补|需补)[^。！？]{0,24}(?:步骤|结果|证据)')
    if (not any(exact_number.search(part) and evidence_gap.search(part) for part in re.split(r'[。！？]', next_step))
            or not evidence_gap.search(row.get('summary') or '')
            or addressed_person(invitation, bot) != acceptor
            or not exact_number.search(invitation['content'].get('text') or '')):
        return None
    if (tests['id'], record) in latest:
        reply = addressed_question(latest[tests['id'], record], bot)
        if not reply or reply[0] != acceptor:
            return None
    details = {'角色': '当时用的是什么身份', '时间': '什么时候试的', '步骤': '做了哪些操作', '结果': '看到了什么结果'}
    requested = [label for word, label in details.items() if word in row['summary']]
    follow_up = '写下' + '、'.join(requested) if requested else '写下当时试用的情况'
    return acceptor, 'evidence', '请补一下试用记录。' + follow_up + '。'


def by_recipient(binding, snapshot):
    """Return current actions keyed by candidate open ID; the sender checks directory identity."""
    scope, tables = watch._binding(binding)
    bot = onboard.bot_actor(binding)
    token, url = onboard.parse_base_url(binding['base']['url'])
    if (not bot or token != binding['base']['token'] or snapshot.get('format_version') != 1
            or snapshot.get('binding') != comments.binding_identity(binding)
            or not isinstance(snapshot.get('records'), dict) or not isinstance(snapshot.get('comments'), dict)):
        raise ValueError('提醒清单的快照与绑定的项目或助手不一致。')
    project = scope['project_record_id']
    item = snapshot['records'].get(scope['project_table_id'] + '/' + project, {})
    fields = tables['projects']['fields']
    if (item.get('role') != 'projects' or not isinstance(item.get('fields'), dict)
            or item['fields'].get(fields['number']['id']) != binding.get('project_number')):
        raise ValueError('提醒清单缺少已绑定的项目记录。')
    if item['fields'].get(fields['status']['id']) not in (['进行中'], ['准备中']):
        return {}
    latest = latest_comments(snapshot, project, token)
    rows = {}
    for key, item in snapshot['records'].items():
        role = item['role']
        if role not in ('feedback', 'requirements', 'bugs', 'work', 'verification'):
            continue
        table = tables[role]
        fields = table['fields']
        table_id, record_id = key.split('/')
        if (table_id != table['id'] or not re.fullmatch(r'tbl[a-zA-Z0-9]+', table_id)
                or not re.fullmatch(r'rec[a-zA-Z0-9]+', record_id)):
            raise ValueError('提醒清单中的记录与项目表绑定不一致。')
        row = {name: item['fields'][f['id']] for name, f in fields.items()}
        if row.get('nature') != ['当前工作'] or not in_project(role, row, fields['project'], binding):
            continue
        rows[table_id, record_id] = (role, row)
    output = defaultdict(list)
    for (table_id, record_id), (role, row) in rows.items():
        if role == 'verification':
            action = pending_manual_test(record_id, row, tables, rows, latest, bot['open_id'])
        else:
            source_author = feedback_source_author(row, binding, snapshot, bot['open_id']) if role == 'feedback' else None
            action = row_action(role, row, tables[role]['fields'], bot['open_id'],
                                latest.get((table_id, record_id), NO_COMMENT), source_author)
        if action:
            recipient, kind, instruction = action
            output[recipient].append(dict(role=role, table_id=table_id, record_id=record_id, kind=kind,
                number=short_text(row.get('number'), 32), title=short_text(row.get('title'), 70),
                action=instruction, url=url + '?' + urlencode({'table': table_id, 'record': record_id})))
    for recipient, items in output.items():
        # If the parent explicitly directs this person to a pending linked test,
        # show that test once and link directly to where the evidence belongs.
        forwarded = set()
        for item in items:
            if item['kind'] != 'evidence':
                continue
            test_key = item['table_id'], item['record_id']
            test = rows[test_key][1]
            test_fields = tables['verification']['fields']
            for parent_role, relation in (('requirements', 'requirements'), ('bugs', 'retested_bugs')):
                table_id = tables[parent_role]['id']
                for parent_id in linked(test, test_fields, relation, table_id):
                    entry = latest.get((table_id, parent_id))
                    if (addressed_person(entry, bot['open_id']) == recipient and
                            re.search(r'(?:请在|请到|请打开)\s*[「【]?' + re.escape(item['number'])
                                      + r'(?![A-Za-z0-9-])[^。！？]{0,30}(?:补充|补上|补填|填写)',
                                      entry['content'].get('text') or '')):
                        forwarded.add((table_id, parent_id))
        items[:] = [item for item in items if item['kind'] != 'clarify'
                    or (item['table_id'], item['record_id']) not in forwarded]
        items.sort(key=lambda action: (action['number'], action['table_id'], action['record_id']))
    return dict(output)
