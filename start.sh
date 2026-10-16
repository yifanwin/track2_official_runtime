#!/usr/bin/env bash
# Track 2 官方 Tool Runtime —— 启动前准备
#
# 职责：把两个必须由**运行时环境**决定的东西固化下来，并做前置检查。
#   * TRACK2_PYTHON      —— 标定环境的解释器（daemon 默认的 python3 不是它）
#   * TRACK2_PYTHONPATH  —— track2_judge（物理侧）与节点包
#
# 环境（PAOS 在 start 时注入）：
#   PAOS_SKILL_ROOT / PAOS_SKILL_NAME / PAOS_SKILL_VERSION / PAOS_SKILL_PROFILE
#
# 幂等：重复执行结果一致；不下载任何东西。
set -euo pipefail

BUNDLE="${PAOS_SKILL_ROOT:-$(cd -- "$(dirname -- "$0")" && pwd)}"
PROFILE="${PAOS_SKILL_PROFILE:-sim}"

# 标定环境：与 track2_final 的物理侧同源，装在 wenyifan/toolenv
# （见 work/reports/52_tool_surface_decision.md —— 3.12 一个环境全包）
WENYIFAN="$(cd -- "${BUNDLE}/.." && pwd)"
: "${TRACK2_PYTHON:=${WENYIFAN}/toolenv/.venv/bin/python}"
: "${TRACK2_PYTHONPATH:=${WENYIFAN}/track2_final:${BUNDLE}/nodes}"

echo "[track2-runtime] bundle  = ${BUNDLE}"
echo "[track2-runtime] profile = ${PROFILE}"
echo "[track2-runtime] python  = ${TRACK2_PYTHON}"

if [ ! -x "${TRACK2_PYTHON}" ]; then
    echo "[track2-runtime] FATAL: 标定环境的解释器不存在或不可执行" >&2
    exit 1
fi

# 前置检查：标定环境必须同时具备 Tool 面与物理侧
"${TRACK2_PYTHON}" - <<'PY'
import sys
missing = []
for name in ("dora", "pyarrow", "forge_tool", "forge_msgs", "yaml",
             "mani_skill", "sapien", "torch", "numpy", "scipy"):
    try:
        __import__(name)
    except Exception:
        missing.append(name)
if missing:
    print("[track2-runtime] FATAL: 标定环境缺包: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
import torch
print("[track2-runtime] 标定环境 OK | python %s | torch %s (cuda %s, avail %s)"
      % (sys.version.split()[0], torch.__version__, torch.version.cuda,
         torch.cuda.is_available()))
PY

# 让 dora flow 里的 ${TRACK2_PYTHON}/${TRACK2_PYTHONPATH} 能展开。
# PAOS 只把 profile.environment 与节点自带 env 注入 dataflow，所以写进 profile 环境变量。
if [ -n "${PAOS_HOOK_PYTHON:-}" ]; then
    echo "[track2-runtime] 提示：把下面两行加到 skill.yaml 的 profiles.${PROFILE}.environment"
    echo "               TRACK2_PYTHON: ${TRACK2_PYTHON}"
    echo "               TRACK2_PYTHONPATH: ${TRACK2_PYTHONPATH}"
fi

exit 0
