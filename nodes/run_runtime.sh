#!/bin/sh
# track2-maniskill-runtime 的启动包装。
#
# 为什么要这一层：dora 的 spawner 会把 `path:` 里的解释器解析成 **readlink 后的真实路径**
# （uv 建的 venv 是指向 ~/.local/share/uv/python/... 的符号链接），
# 那样 venv 的 site-packages 就丢了 —— 报 `No module named 'pyarrow'`。
# 用脚本包一层，venv 的识别由 python 自己做，dora 只负责执行这个脚本。
set -eu

# PyInstaller 的 onefile 二进制会解出 /tmp/_MEIxxx —— 我们的解释器与站点包
# 都不能依赖 `$0` 的当前位置，所以显式算出来。
_HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # .../track2_official_runtime/nodes
_BUNDLE=$(cd -- "${_HERE}/.." && pwd)               # .../track2_official_runtime
_WENYIFAN=$(cd -- "${_BUNDLE}/.." && pwd)           # .../wenyifan

# skill.yaml 的 profiles.<name>.environment 会注入这两个；手工跑时退回默认位置。
: "${TRACK2_PYTHON:=${_WENYIFAN}/toolenv/.venv/bin/python}"
: "${TRACK2_PYTHONPATH:=${_WENYIFAN}/track2_final:${_HERE}}"
export TRACK2_PYTHON TRACK2_PYTHONPATH
export PYTHONPATH="${TRACK2_PYTHONPATH}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${TRACK2_PYTHON}" -u -m track2_maniskill_runtime "$@"
