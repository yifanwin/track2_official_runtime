#!/usr/bin/env bash
# Track 2 官方 Tool Runtime —— 启动前准备。
#
# 职责：把标定环境的解释器与 PYTHONPATH **确认下来**，并做前置检查。
# 幂等；不下载任何东西。
#
# ⚠️ 不能用 `$BUNDLE/..` 推路径 —— install 之后 Bundle 被搬到
#    `$HOME/.PhyAgentOS/skills/<name>/`，那里没有 toolenv。
#    真正的位置只写在 `skill.yaml` 的 `profiles.<p>.environment` 里，
#    所以从那里读（`PAOS_HOOK_PYTHON` 是 PAOS 提供来解释它自己的 YAML 的）。
set -euo pipefail

BUNDLE="${PAOS_SKILL_ROOT:-$(cd -- "$(dirname -- "$0")" && pwd)}"
PROFILE="${PAOS_SKILL_PROFILE:-sim}"
HOOK_PY="${PAOS_HOOK_PYTHON:-python3}"

_setting() {
    # 从 skill.yaml 的 profiles.<PROFILE>.environment 里取一个键。
    # 优先用 HOOK_PYTHON（PAOS venv，带 yaml）；它没有 yaml 时退回**纯文本扫描**
    # —— skill.yaml 的这段结构固定（`environment:` 下每行 `KEY: value`），
    # 扫得准，且不引入依赖。
    if "$HOOK_PY" -c 'import yaml' 2>/dev/null; then
        "$HOOK_PY" - "$BUNDLE/skill.yaml" "$PROFILE" "$1" <<'PY'
import sys, yaml
path, profile, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    doc = yaml.safe_load(open(path, encoding="utf8"))
    env = (doc.get("profiles", {}).get(profile, {}) or {}).get("environment", {}) or {}
    value = env.get(key)
except Exception:
    value = None
if isinstance(value, str) and value.strip():
    print(value.strip())
PY
        return
    fi
    awk -v p="  ${PROFILE}:" -v k="      ${1}:" '
        $0 == p { in_p = 1; next }
        in_p && /^  [^ ]/ { in_p = 0 }
        # 前缀匹配 —— 行里还有 ": value"，不能用整行相等
        in_p && substr($0, 1, length(k)) == k {
            sub(/^[^:]*:[ ]*/, "")
            if (length($0)) { print; exit }
        }
    ' "$BUNDLE/skill.yaml"
}

: "${TRACK2_PYTHON:=$(_setting TRACK2_PYTHON)}"
: "${TRACK2_PHYSICS_ROOT:=$(_setting TRACK2_PHYSICS_ROOT)}"

echo "[track2-runtime] bundle  = ${BUNDLE}"
echo "[track2-runtime] profile = ${PROFILE}"
echo "[track2-runtime] python  = ${TRACK2_PYTHON:-<未配置>}"

if [ -z "${TRACK2_PYTHON}" ] || [ ! -x "${TRACK2_PYTHON}" ]; then
    echo "[track2-runtime] FATAL: 标定环境的解释器不存在或不可执行。" >&2
    echo "  期望值来自 skill.yaml 的 profiles.${PROFILE}.environment.TRACK2_PYTHON。" >&2
    echo "  当前值：'${TRACK2_PYTHON}'" >&2
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

exit 0
