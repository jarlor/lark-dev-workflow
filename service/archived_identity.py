"""Recheck a prior worker's original operator using the same official Feishu reply."""
import re

from prepare_plan import digest
from stage_action import require


SOURCE_KEYS = ('original_history', 'original_person_field', 'original_worker_report', 'original_reply', 'official_reply')


def source_hashes(material, hashes):
    sources = material.get('source_hashes')
    require(isinstance(sources, dict) and set(sources) == set(SOURCE_KEYS)
            and all(isinstance(sources[k], str) and sources[k] in hashes for k in SOURCE_KEYS)
            and len(set(sources.values()) | {material['report_sha256']}) == len(SOURCE_KEYS) + 1,
            '原执行器的点击、人员、结论或新旧评论原件不完整。')


def check(project, record, person_field, person, name, anchor, material):
    """The reply maps identifiers. Only the native stage history establishes approval."""
    old = material.get('old_operator_id')
    require(isinstance(old, str) and re.fullmatch(r'[1-9][0-9]{1,30}', old)
            and old == material.get('old_confirmer_id') == material.get('old_reply_author_id')
            and (material.get('project_record_id'), material.get('table_id'), material.get('record_id'),
                 material.get('stage_field_id'), material.get('confirmer_field_id'), material.get('old_person_name')) ==
                (project.project, project.table, record, project.fields['status']['id'], project.fields[person_field]['id'], name)
            and material.get('original_worker_verified_person') is True
            and material.get('history') == anchor and material.get('operator_open_id') == person,
            '原执行器没有确认同一行、指定人员和这一次点击。')
    witness = material.get('reply')
    require(isinstance(witness, dict) and all(isinstance(witness.get(k), str)
            and re.fullmatch(r'[1-9][0-9]{1,30}', witness[k]) for k in ('comment_id', 'reply_id'))
            and all(type(witness.get(k)) is int and witness[k] > 0 for k in ('create_time', 'update_time'))
            and witness['update_time'] >= witness['create_time']
            and witness.get('original_text_sha256') == witness.get('live_text_sha256')
            and isinstance(witness.get('original_text_sha256'), str)
            and re.fullmatch('[a-f0-9]{64}', witness['original_text_sha256']),
            '原评论与飞书回读的编号、时间或正文不一致。')
    # Read only the registered Base and this exact thread. Never resolve a
    # person through a different credential or silently accept a display name.
    cursor = None
    for _ in range(100):
        args = ['--token', project.token, '--type', 'bitable', '--comment-id', witness['comment_id'], '--page-size', '100']
        if cursor:
            args += ['--page-token', cursor]
        page = project.reader.read('drive', '+list-replies', *args)
        require(page.get('comment_id') == witness['comment_id'] and page.get('file_token') == project.token
                and page.get('file_type') == 'bitable' and isinstance(page.get('items'), list),
                '飞书返回的评论不属于原文件或讨论。')
        matches = [r for r in page['items'] if r.get('reply_id') == witness['reply_id']]
        if matches:
            require(len(matches) == 1, '原讨论出现重复回复编号。')
            live = matches[0]
            elements = live.get('content', {}).get('elements')
            require(live.get('user_id') == person and live.get('create_time') == witness['create_time']
                    and live.get('update_time') == witness['update_time'] and isinstance(elements, list) and elements
                    and all(e.get('type') == 'text_run' and isinstance(e.get('text_run', {}).get('text'), str)
                            for e in elements)
                    and digest(''.join(e['text_run']['text'] for e in elements)) == witness['original_text_sha256'],
                    '飞书原评论的作者、时间或正文已不同。')
            return
        next_cursor = page.get('page_token')
        require(page.get('has_more') is True and isinstance(next_cursor, str) and next_cursor
                and next_cursor != cursor, '飞书没有找到原执行器留存的这条回复。')
        cursor = next_cursor
    require(False, '未能在本次核对中读到原回复。')
