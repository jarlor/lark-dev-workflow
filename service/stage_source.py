"""Read an enrolled project's own test evidence using a fixed, administrator-installed reader.

Only the private service registration selects executable code and evidence roots.
Neither the CLI nor any Feishu cell can select a program, a path, or a command.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import event_bridge as bridge
from stage_action import Pending, require
import workflow_rule


# Opening /dev/fd as the interpreter's script can consume the shared offset on
# macOS. Read the inherited descriptor once and rehash the exact bytes we run.
READER_BOOTSTRAP = """
import hashlib, os, sys
with os.fdopen(int(sys.argv[1]), 'rb') as program:
    source = program.read(4 * 1024 * 1024 + 1)
if len(source) > 4 * 1024 * 1024 or hashlib.sha256(source).hexdigest() != sys.argv[2]:
    raise SystemExit(78)
exec(compile(source, sys.argv[3], 'exec'), {'__name__': '__main__', '__file__': sys.argv[3]})
"""


def checksum(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(chunk)
    return h.hexdigest()


def admin_path(path, directory=False):
    info = path.lstat()
    require((stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
            and info.st_uid == 0 and not info.st_mode & 0o022 and path.resolve() == path,
            '项目证据读取器须由服务器管理员安装，项目不能改写它。')


def response_sources(sources, roots):
    require(isinstance(sources, list) and 0 < len(sources) <= 32, '没有本次实际运行的原始材料。')
    checked = {}
    for source in sources:
        require(isinstance(source, dict) and set(source) == {'path', 'sha256'}
                and isinstance(source['path'], str) and isinstance(source['sha256'], str)
                and re.fullmatch(r'[0-9a-f]{64}', source['sha256']), '原始材料缺少文件和摘要。')
        path = Path(source['path'])
        require(path.is_absolute() and path.resolve() == path and str(path) not in checked
                and any(path.is_relative_to(root) and path != root for root in roots),
                '原始材料超出了项目登记的证据目录。')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
            initial = os.fstat(f.fileno())
            require(stat.S_ISREG(initial.st_mode), '证据来源须为普通文件。')
            observed = checksum(f)
            final = os.fstat(f.fileno())
            require(observed == source['sha256'] and
                    (initial.st_dev, initial.st_ino, initial.st_mtime_ns, initial.st_size) ==
                    (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_size),
                    '原始执行材料已变化。')
        checked[str(path)] = observed
    return checked


def location(project):
    # A root-run installation may keep everything in its existing private state.
    # Non-root service accounts need an admin-owned location outside writable state.
    local = project.directory / 'stage-proof-source'
    managed = Path(project.path).resolve().parent / 'stage-proof-source'
    found = [p for p in dict.fromkeys((managed, local)) if os.path.lexists(p)]
    require(len(found) <= 1, '同一项目登记了两份证据读取器，需先核对。')
    return found[0] if found else None


def query(project, kind, role, record, *, prior_step=None):
    require(kind in ('target', 'test', 'acceptance_plan', 'original_click', 'prior_action') and role in ('requirements', 'work', 'bugs', 'verification'),
            '没有登记这种证据查询。')
    folder = location(project)
    if folder is None:
        raise Pending('本项目尚未接入实际的测试或构建证据来源。')
    workdir = Path(project.paths[0])
    require(not folder.is_relative_to(workdir), '证据读取器不能放在项目可写的工作区里。')
    for parent in (folder, *folder.parents):
        admin_path(parent, directory=True)
    require(not folder.stat().st_mode & 0o007, '项目证据读取器的目录不能向其他系统用户开放。')
    settings = folder / 'registration.json'
    program = folder / 'read_evidence.py'
    admin_path(settings)
    admin_path(program)
    config = bridge.load(settings)
    pins = workflow_rule.service_pins(project.config, project.paths)
    require(set(config) == {'protocol', 'service', 'runner', 'reader_sha256', 'roots'} and config['protocol'] == 1
            and all(config[k] == v for k, v in pins.items()) and isinstance(config['roots'], list)
            and 0 < len(config['roots']) <= 16, '证据源没有和本服务、执行器及目录正式绑定。')
    roots = [Path(s) for s in config['roots']]
    require(all(p.is_absolute() and p.is_dir() and p == p.resolve() and len(p.parts) >= 3 for p in roots),
            '证据目录缺失或过于宽泛。')
    binding = {k: project.config[k] for k in ('app_id', 'tenant_key', 'base_token', 'project_record_id')}
    request = dict(protocol=1, kind=kind, binding=binding, action=project.key, role=role, record_id=record)
    if kind == 'prior_action':
        require(role == 'work' and prior_step in ('work-start', 'work-deliver'), '仅可核对本研发项既有的实际开工或交付。')
        request['prior_step'] = prior_step
    else:
        require(prior_step is None, '此证据请求不能携带额外的步骤。')
    # Execute the exact open inode whose contents were checked. No shell, external
    # credentials, inherited Python search path or project-selected argument.
    with os.fdopen(os.open(program, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as f:
        require(checksum(f) == config['reader_sha256'], '已安装的证据读取器与登记版本不同。')
        f.seek(0)
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', READER_BOOTSTRAP,
                                 str(f.fileno()), config['reader_sha256'], str(program)],
                                input=json.dumps(request), capture_output=True, text=True, timeout=30,
                                pass_fds=(f.fileno(),), cwd=folder, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    require(result.returncode == 0 and len(result.stdout) <= 262144, '项目没有返回可核实的运行证据。')
    data = json.loads(result.stdout)
    require(isinstance(data, dict) and set(data) == {'request', 'evidence', 'sources'}
            and data['request'] == request and isinstance(data['evidence'], dict),
            '项目尚未提供这一动作、记录和版本的实际材料。')
    hashes = set(response_sources(data['sources'], roots).values())
    evidence = data['evidence']
    if kind in ('test', 'acceptance_plan', 'original_click', 'prior_action'):
        require(evidence.get('report_sha256') in hashes, '没有查到与本次原始报告摘要相同的文件。')
    if kind == 'test' and evidence.get('observation') == 'reproduced_failure':
        evidence['verified_input'] = evidence.get('input_sha256') in hashes
    if kind == 'target':
        builds = evidence.get('components')
        evidence['verified_artifact'] = (isinstance(builds, dict) and bool(builds) and
                                         all(isinstance(value, str) and value in hashes for value in builds.values()))
    return evidence
