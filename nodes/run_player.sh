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

exec "${TRACK2_PYTHON}" -u -m track2_player_node "$@"
