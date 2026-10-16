#!/bin/sh
# track2-player 节点的启动包装。与 run_runtime.sh 同一模式（理由见那个文件）。
set -eu

_HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # .../<bundle>/nodes

_WENYIFAN=$(cd -- "${_HERE}/../.." 2>/dev/null && pwd || echo "")
if [ -z "${_WENYIFAN}" ]; then _WENYIFAN=$(cd -- "${_HERE}/.." && pwd); fi
: "${TRACK2_PYTHON:=${_WENYIFAN}/toolenv/.venv/bin/python}"
: "${TRACK2_PHYSICS_ROOT:=${_WENYIFAN}/track2_final}"

export PYTHONPATH="${TRACK2_PHYSICS_ROOT}:${_HERE}${PYTHONPATH:+:${PYTHONPATH}}"
export TRACK2_PYTHON TRACK2_PHYSICS_ROOT

exec "${TRACK2_PYTHON}" -u -m track2_player_node "$@"
