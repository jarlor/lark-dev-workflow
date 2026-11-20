"""A requirement's approved isolated check, with a separately verified test deployment."""
from pathlib import Path
import re

from prepare_plan import digest
from stage_action import require


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def record_id(value):
    return isinstance(value, str) and re.fullmatch(r'rec[A-Za-z0-9]+', value) is not None


def policy(evidence):
    rules = evidence.get('isolated_independent')
    require(isinstance(rules, dict) and rules and all(record_id(rid) for rid in rules),
            '没有指定获准在隔离环境复测的实际记录。')
    for rule in rules.values():
        require(isinstance(rule, dict) and set(rule) == {'build', 'components', 'plan_sha256', 'authorization_sha256'}
                and isinstance(rule['build'], str) and rule['build'].strip() and sha256(rule['authorization_sha256'])
                and isinstance(rule['plan_sha256'], dict) and rule['plan_sha256']
                and all(record_id(work) and sha256(value) for work, value in rule['plan_sha256'].items()),
                '隔离复测须指定构建、每项研发计划及对应的原授权。')
    return rules


def execution(evidence):
    proof = evidence.get('isolated_execution')
    require(isinstance(proof, dict) and set(proof) == {'deployment_build', 'deployment_components',
            'local_assets', 'deployed_assets', 'comparison_sha256'}, '缺少隔离环境与测试服的构建对照记录。')
    assets = proof['local_assets']
    require(isinstance(assets, dict) and assets and all(isinstance(k, str) and k.strip() and k not in ('.', '..')
            and Path(k).name == k and '\\' not in k and '\x00' not in k
            and sha256(v) for k, v in assets.items()) and assets == proof['deployed_assets']
            and sha256(proof['comparison_sha256']) and proof['comparison_sha256'] not in assets.values(),
            '本地和测试服的同名制品须逐个对应并具有相同的文件摘要。')
    return proof


def check_sources(kind, role, action, evidence, files, identities):
    """Called after each of the enrolled reader's distinct source paths was rehashed."""
    hashes = set(files.values())
    if 'isolated_independent' in evidence:
        require((kind, role, action) == ('target', 'requirements', 'req-accept')
                and all(rule['authorization_sha256'] in hashes for rule in policy(evidence).values()),
                '项目没有提供这条需求允许隔离复测的原始授权文件。')
    if 'isolated_execution' in evidence:
        require((kind, role, action) == ('test', 'verification', 'req-accept'), '隔离复测只适用于已登记的需求送验。')
        proof = execution(evidence)
        require(proof['comparison_sha256'] in hashes, '缺少核对实际文件和构建的原始记录。')
        for name, sha in proof['local_assets'].items():
            sources = [Path(p) for p, value in files.items() if value == sha and Path(p).name == name]
            local = [p for p in sources if p.parent.name == 'local']
            deployed = [p for p in sources if p.parent.name == 'deployed']
            require(len(local) == len(deployed) == 1 and local[0].parent.parent == deployed[0].parent.parent
                    and identities[str(local[0])] != identities[str(deployed[0])],
                    '须分别保留本地与测试服的同名实际文件。')


def validate_target(target, action, role, ready):
    rules = policy(target)
    tests = target['tests']
    require(ready and (action, role) == ('req-accept', 'requirements')
            and target.get('delivery_mode') == 'deployment' and target.get('environment') == '测试'
            and target.get('require_independent') is True and tests.get('manual')
            and set(rules) <= set(tests.get('independent', ()))
            and not any(rid in group for name, group in tests.items() if name != 'independent' for rid in rules),
            '隔离复测仅用于这条需求指定的独立检查，还须核实测试服部署并保留单独的人工验收。')


def test_target(target, rid, evidence, plans, revisions, self_tests):
    rule = target['isolated_independent'][rid]
    proof = execution(evidence)
    require(rule['plan_sha256'] == {w: digest(plan) for w, plan in plans.items()}
            and all(isinstance(plan, str) and plan.strip() for plan in plans.values())
            and evidence.get('subject_revisions') == revisions and all(type(v) is int for v in evidence['subject_revisions'].values()),
            '隔离复测的实际范围或已确认计划与原授权不符。')
    tested = {}
    for test in self_tests:
        for name, sha in test['components'].items():
            require(tested.setdefault(name, sha) == sha, '各研发项自测使用了同一组件的不同版本。')
    require(tested and tested == rule['components'] == evidence.get('components')
            and all(target['components'].get(k) == sha for k, sha in tested.items())
            and evidence.get('environment') == '本地' and evidence.get('build') == rule['build']
            and proof['deployment_build'] == target['build'] and proof['deployment_components'] == target['components'],
            '隔离复测的代码、实测组件或对照的测试服版本与这次交付不符。')
    return dict(target, environment='本地', build=rule['build'], components=tested)
