#!/bin/sh
# track2-maniskill-runtime 的启动包装。
#
# 为什么要这一层：dora 的 spawner 会把 `path:` 里的解释器解析成 **readlink 后的真实路径**
# （uv 建的 venv 是指向 ~/.local/share/uv/python/... 的符号链接），
# 那样 venv 的 site-packages 就丢了 —— 报 `No module named 'pyarrow'`。
# 用脚本包一层，venv 的识别由 python 自己做，dora 只负责执行这个脚本。
set -eu

# `$0` 就是本脚本 —— install 之后它跟着 Bundle 搬到
# `$HOME/.PhyAgentOS/skills/<name>/nodes/`，所以这里算出来的是**安装后**的位置。
_HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # .../<bundle>/nodes

# skill.yaml 的 profiles.<name>.environment 注入这两个（install 时不变，都是机器相关的）：
#   TRACK2_PYTHON        标定环境的解释器
#   TRACK2_PHYSICS_ROOT  物理侧源码树（track2_judge 所在）
# 手工跑（不经 PAOS）时按源码树的相对位置兜底。
# 源码树布局：nodes → bundle → wenyifan（安装后这个兜底不成立，
# 所以正常路径是 skill.yaml 注入的 TRACK2_PHYSICS_ROOT）
_WENYIFAN=$(cd -- "${_HERE}/../.." 2>/dev/null && pwd || echo "")
if [ -z "${_WENYIFAN}" ]; then _WENYIFAN=$(cd -- "${_HERE}/.." && pwd); fi
: "${TRACK2_PYTHON:=${_WENYIFAN}/toolenv/.venv/bin/python}"
: "${TRACK2_PHYSICS_ROOT:=${_WENYIFAN}/track2_final}"

# PYTHONPATH：物理侧 + 本 Bundle 的 nodes（后者由 _HERE 给出，与安装位置无关）
export PYTHONPATH="${TRACK2_PHYSICS_ROOT}:${_HERE}${PYTHONPATH:+:${PYTHONPATH}}"
export TRACK2_PYTHON TRACK2_PHYSICS_ROOT

exec "${TRACK2_PYTHON}" -u -m track2_maniskill_runtime "$@"