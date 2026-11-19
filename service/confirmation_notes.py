"""Identify later filing of an already verified decision and repairs to its source link."""
import re
from urllib.parse import parse_qs, urlsplit

from stage_action import require
import watch


def source_link(project, record, previous, current):
    # Only repair the URL when the old target was literally the displayed source
    # note, and that note identifies exactly the same Base record as the new URL.
    match = re.fullmatch(r'\[((?:来源|Source)[:：][^\[\]\n]+)\]\((https://[^\s()]+)\)', current)
    require(match and previous == f'[{match[1]}](http://{match[1]})', '原来源文字或可用的链接有变化。')
    # An old, usable link must never be replaced under this exception. In the
    # observed malformed link the entire sentence was pasted as its hostname.
    authority = re.split(r'[/#?]', match[1], maxsplit=1)[0]
    require(re.search(r'\s', authority) and '@' not in authority, '旧链接没有把来源说明误当作网址。')
    numbers = set(re.findall(r'(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*-\d+(?![A-Za-z0-9_-])', match[1]))
    target = urlsplit(match[2])
    base = urlsplit(project.binding['base']['url'])
    require(len(numbers) == 1 and (target.scheme, target.netloc, target.path) == ('https', base.netloc, base.path)
            and not target.fragment and not target.username and not target.password, '来源未唯一指向本项目中的原事项。')
    query = parse_qs(target.query, keep_blank_values=True)
    require(set(query) == {'record', 'table'} and query['table'] == [project.table] and len(query['record']) == 1,
            '来源链接没有指向原表的一条记录。')
    rid = query['record'][0]
    require(re.fullmatch(r'rec[A-Za-z0-9]+', rid) and rid != record, '来源链接没有指向另一条真实记录。')
    number, owner = (project.fields[k] for k in ('number', 'project'))
    require(number['type'] == 'auto_number' and owner['type'] == 'link', '原表缺少唯一的自动编号或项目关联。')
    got = project.reader.read_record(project.token, project.table, rid, [number, owner])
    require(got.get('record_id') == rid and got['values'][number['id']] == next(iter(numbers))
            and watch._one_project(got['values'], owner['id'], project.project),
            '来源链接对应的记录编号或项目不同。')


def original_value(project, key, record, proof, row, change):
    """Return the original API value. A note can never establish or change approval."""
    before, after = change['before'], change['after']
    require(after == row[key], '归档栏或来源链接后来又被修改了。')
    if key == 'confirmed_revision':
        require(change['field_type'] == 'Number' and before in ('', None)
                and type(after) is int and type(row[key]) is int and after == proof['business_revision'],
                '归档的版本与本人确认的版本不一致。')
    elif key == 'confirmation':
        require(change['field_type'] == 'Text' and before in ('', None) and isinstance(after, str) and after.strip(),
                '已有确认说明被改写。')
    else:
        require(key == 'details' and change['field_type'] == 'Url' and isinstance(after, str),
                '这不是来源链接的修正。')
        source_link(project, record, before, after)
        return before
    # Feishu history renders an absent cell as an empty string; record-get
    # represents it as null. A saved original empty string is equivalent here.
    return None
