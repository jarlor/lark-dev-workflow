#!/bin/sh
# Linux entry point; JSON/state handling uses the skill's existing Python dependency.
set -eu
task_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON3:-python3}" "$task_script_dir/unattended.py" "$@"
