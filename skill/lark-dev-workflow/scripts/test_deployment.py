"""Check saved deployment and health observations without contacting any service.

The caller supplies the project's complete dependency set and actual URLs. This
checks consistency of supplied evidence, not its authenticity, user access,
functional correctness, human acceptance, or permission to deploy. Public status
JSON is kept under ``deployment`` unchanged; health is a separate observation.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


class EvidenceError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise EvidenceError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _time(value, label):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        _require(isinstance(value, datetime) and value.tzinfo is not None
                 and value.utcoffset() is not None, label + '须为带时区的实际时间。')
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise EvidenceError(label + '须为有效、带时区的实际时间。') from exc


def _url(value, label):
    _require(_text(value), label + '缺少实际地址。')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        valid = False
    _require(valid, label + '须为实际 HTTP(S) 地址，不能包含登录凭据或页面锚点。')


def _repository(value, label):
    _require(type(value) is dict and {'sha', 'workflow_run_id'} <= set(value),
             label + '缺少完整提交或部署工作流编号。')
    _require(isinstance(value['sha'], str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value['sha']),
             label + '必须使用完整提交 SHA，不能只写分支或短编号。')
    _require(type(value['workflow_run_id']) is int and value['workflow_run_id'] > 0,
             label + '部署工作流编号须为正整数。')
    return {key: value[key] for key in ('sha', 'workflow_run_id')}


def _expected(value):
    required = {'environment', 'deployment_url', 'repositories', 'health_urls'}
    _require(type(value) is dict and required <= set(value)
             and set(value) <= required | {'max_age_seconds'}, '项目期望配置缺项或含有未知属性。')
    _require(_text(value['environment']), '请提供目标环境名称。')
    _url(value['deployment_url'], '部署状态')
    repos, urls = value['repositories'], value['health_urls']
    _require(type(repos) is dict and repos and all(_text(name) for name in repos),
             '请列出本次工作依赖的全部仓库与目标提交。')
    for name, repo in repos.items():
        _repository(repo, '目标仓库 ' + name + '：')
        _require(set(repo) == {'sha', 'workflow_run_id'}, '目标仓库配置含有未知属性。')
    _require(type(urls) is dict and urls and all(_text(name) for name in urls),
             '请提供本次必须检查的健康接口。')
    for name, url in urls.items():
        _url(url, '健康检查 ' + name + '：')
    age = value.get('max_age_seconds', 300)
    _require(type(age) is int and age > 0, '观察有效期 max_age_seconds 须为正整数秒数。')
    return age


def _observation(value, expected, now, age, label, historical=False):
    required = {'source_url', 'observed_at', 'deployment', 'health_checks'}
    _require(type(value) is dict and required <= set(value), label + '观察记录不完整。')
    _require(value['source_url'] == expected['deployment_url'], label + '部署状态来源与项目约定地址不同。')
    observed = _time(value['observed_at'], label + '观察时间')
    _require(observed <= now, label + '观察时间在未来。')
    reference = observed if historical else now
    _require((reference - observed).total_seconds() <= age, label + '观察已过期，请重新读取当前状态。')
    status = value['deployment']
    _require(type(status) is dict, label + '缺少原始部署状态。')
    _require(status.get('environment') == expected['environment'], label + '实际环境与目标环境不同。')
    _require(status.get('state') == 'ready',
             label + '部署尚未 ready；构建通过、检查完成、未变化或失败都不能代替部署就绪。')
    deployed = _time(status.get('deployed_at'), label + '部署时间')
    _require(deployed <= observed, label + '部署时间晚于观察时间，不能作为已完成部署。')
    repos = status.get('repositories')
    _require(type(repos) is dict, label + '没有读到实际仓库组合。')
    selected = {}
    for name, target in expected['repositories'].items():
        _require(name in repos, label + '缺少依赖仓库 ' + name + '。')
        actual = _repository(repos[name], label + '仓库 ' + name + '：')
        _require(actual == target, label + '仓库 ' + name + '的实际提交或工作流与目标不符，旧 ready 不能证明新版本已部署。')
        selected[name] = actual
    checks = value['health_checks']
    _require(type(checks) is dict, label + '缺少本次独立读取的健康检查。')
    for name, url in expected['health_urls'].items():
        check = checks.get(name)
        _require(type(check) is dict and {'url', 'checked_at', 'ok'} <= set(check),
                 label + '缺少健康检查 ' + name + '的实际结果、来源或时间。')
        _require(check['url'] == url, label + '健康检查 ' + name + '的来源地址不符。')
        _require(check['ok'] is True, label + '健康检查 ' + name + '没有通过。')
        checked = _time(check['checked_at'], label + '健康检查 ' + name + '时间')
        _require(deployed <= checked <= observed, label + '健康检查 ' + name + '不是此次部署后的有效观察。')
        _require((reference - checked).total_seconds() <= age,
                 label + '健康检查 ' + name + '已过期，请重新检查。')
    return {'environment': status['environment'], 'deployed_at': deployed.isoformat(),
            'repositories': selected}, observed


def check(expected, before, after=None, *, now=None):
    """Return a read-only evidence check; ``ok`` never means human acceptance.

    With only ``before``, status and health must be current at ``now``. With
    ``after``, the start can be old because a test takes time: start health must
    have been fresh at start, while end status and health must be current now.
    Older deployed_at is valid for an unchanged fixed candidate. A new deployment
    time, even with identical commits, invalidates the assumed stable window.
    """
    result = {'ok': False, 'window_checked': after is not None,
              'human_acceptance': 'not_evaluated', 'scope': 'supplied_observations_only', 'errors': []}
    try:
        moment = _time(now if now is not None else datetime.now(timezone.utc), '核对时间')
        result['checked_at'] = moment.isoformat()
        age = _expected(expected)
        first, started = _observation(before, expected, moment, age, '开始：', historical=after is not None)
        if after is not None:
            last, ended = _observation(after, expected, moment, age, '结束：')
            _require(started <= ended, '结束观察早于开始观察，不能作为同一测试窗口。')
            _require(first == last, '测试期间部署发生变化；保留原结果，核对影响后另行复测，不能套用通过结论。')
        result.update(ok=True, deployment=first)
    except EvidenceError as exc:
        result['errors'].append(str(exc))
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'JSON 有重复属性，无法确定应采用哪一个值。')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='只核对保存的测试部署与健康观察；不联网、不部署、不写表、不代替人工验收。')
    parser.add_argument('--expected', required=True, help='项目期望 JSON：环境、状态地址、依赖提交/工作流、健康接口、观察有效期')
    parser.add_argument('--before', required=True, help='开始观察 JSON：source_url、observed_at、原始 deployment、health_checks')
    parser.add_argument('--after', help='可选结束观察 JSON，用于核对测试期间部署是否改变')
    parser.add_argument('--now', help='可选核对参考时间（带时区 ISO 格式）；离线重放时应明确其为历史时间')
    args = parser.parse_args(argv)
    try:
        def load(name):
            return json.loads(Path(name).read_text(encoding='utf-8'), object_pairs_hook=_unique)
        result = check(load(args.expected), load(args.before), load(args.after) if args.after else None, now=args.now)
    except (OSError, UnicodeError, ValueError, RecursionError):
        print(json.dumps({'ok': False, 'errors': ['输入文件无法读取或不是完整、无重复属性的 JSON。']}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
