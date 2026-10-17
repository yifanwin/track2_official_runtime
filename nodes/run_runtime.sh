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

# ⚠️ **`PAOS_SKILL_ROOT` 必须以本脚本的位置为准，不能信环境里那个。**
#
#    `dora start --detach` 之后节点由**长命的 dora daemon** spawn，拿到的是
#    **daemon 自己的环境** —— 上面 `PAOS_SKILL_ROOT` 可能是**别的 Bundle**
#    （实测：节点环境里是 `.../skills/move-arm-by-ee`）。而 `$0` 就在
#    `<bundle>/nodes/` 下，`_HERE/..` 永远是**本** Bundle。
#    症状：player 节点在错目录找选手包 → 「没有选手包」→ 静默空转，
#    judge 那边一直等不到结果。
# ⚠️ **无条件覆盖**，不能写成 `: "${PAOS_SKILL_ROOT:=...}"` ——
#    那个写法只在「未设置或为空」时才赋值，而这里恰恰是**已设置但错了**。
PAOS_SKILL_ROOT=$(cd -- "${_HERE}/.." && pwd)
export PAOS_SKILL_ROOT

# Judge 私有控制口的标记文件位置（B1/B4）。dora 把节点的 cwd 设成
# `profiles/<profile>/`，所以 `$(pwd)` 就是对的 profile 目录 ——
# 同样不依赖环境里可能串味的变量。
export TRACK2_PROFILE_DIR="$(pwd)"
export TRACK2_CONTROL_MARKER="$(pwd)/.judge_control.json"

exec "${TRACK2_PYTHON}" -u -m track2_maniskill_runtime "$@"