#!/usr/bin/env python3
"""Refuse dispatch when this account still schedules the project with cron."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def verify(bridge, previous_paths, lines):
    if bridge.get('mode', 'observe') == 'observe':
        return
    if bridge.get('mode') != 'dispatch' or not isinstance(previous_paths, list):
        raise ValueError('Specify previous runner config paths (an empty list for a new project).')
    paths = [bridge['runner_config'], *previous_paths]
    if not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
        raise ValueError('Use absolute runner config paths.')
    markers = {'# lark-dev-workflow:' + hashlib.sha256(
        str(Path(path).resolve()).encode()).hexdigest()[:16] for path in paths}
    for line in lines.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if any(line.rstrip().endswith(marker) for marker in markers):
            raise ValueError('The previous project cron is still active.')
        if any(path in line for path in paths):
            words = shlex.split(line, comments=True)
            if any(word in paths or word.removeprefix('--config=') in paths for word in words):
                raise ValueError('A cron command still uses a project runner.')


def main(bridge_path, previous_path):
    bridge = json.loads(Path(bridge_path).read_text())
    if bridge.get('mode', 'observe') == 'observe':
        return
    previous = json.loads(Path(previous_path).read_text())
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True,
                            timeout=10, env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode and not (result.returncode == 1 and re.fullmatch(
            r'(?:crontab:\s*)?no crontab for [^\r\n]+', result.stderr.strip())):
        raise ValueError('Could not verify the service account crontab.')
    verify(bridge, previous, result.stdout)


if __name__ == '__main__':
    try:
        main(*sys.argv[1:])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        sys.exit(f'Scheduler ownership check failed: {error}')
