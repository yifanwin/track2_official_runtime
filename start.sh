#!/usr/bin/env bash
# Track 2 官方 Tool Runtime —— 启动前准备。
#
# 职责（两件，都幂等）：
#   1. **注入选手包**（T3-b，第 4 步）：`TRACK2_PLAYER_PACKAGE` → profile 下的 player/
#   2. 把标定环境的解释器与 PYTHONPATH **确认下来**，并做前置检查
#
# 不做任何下载。
#
# ⚠️ 不能用 `$BUNDLE/..` 推路径 —— install 之后 Bundle 被搬到
#    `$HOME/.PhyAgentOS/skills/<name>/`，那里没有 toolenv。
#    真正的位置只写在 `skill.yaml` 的 `profiles.<p>.environment` 里，
#    所以从那里读（`PAOS_HOOK_PYTHON` 是 PAOS 提供来解释它自己的 YAML 的）。
#
# ⚠️ PAOS 在 start 时执行本脚本，**非零退出会中止 start**
#    （`manager.py:363-400`）。所以选手包不合格时故意让它失败 ——
#    宁可起不来，也不要带着一个残缺的选手包跑完 5×2 回合。
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
            sub(/^[^:]*:[ ]*/, ""); if (length($0)) { print; exit }
        }
    ' "$BUNDLE/skill.yaml"
}

: "${TRACK2_PYTHON:=$(_setting TRACK2_PYTHON)}"
: "${TRACK2_PHYSICS_ROOT:=$(_setting TRACK2_PHYSICS_ROOT)}"
# 导出：下面注入选手包的那段 python 要读它们（`-I` 会忽略 PYTHONPATH，
# 所以路径是显式传的；但变量本身必须在环境里）。
export TRACK2_PYTHON TRACK2_PHYSICS_ROOT

echo "[track2-runtime] bundle  = ${BUNDLE}"
echo "[track2-runtime] profile = ${PROFILE}"
echo "[track2-runtime] python  = ${TRACK2_PYTHON:-<未配置>}"

if [ -z "${TRACK2_PYTHON}" ] || [ ! -x "${TRACK2_PYTHON}" ]; then
    echo "[track2-runtime] FATAL: 标定环境的解释器不存在或不可执行。" >&2
    echo "  期望值来自 skill.yaml 的 profiles.${PROFILE}.environment.TRACK2_PYTHON。" >&2
    echo "  当前值：'${TRACK2_PYTHON}'" >&2
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4.1 选手包注入（T3-b）
#
# 路径在**每次评测**时才确定，而 PAOS 的 install 是一次性的 ——
# 所以注入放在 hook 里（hook 每次 start 都跑，正好按 case 注入）。

PLAYER_DEST="${BUNDLE}/profiles/${PROFILE}/player"

# 解包与 manifest 校验都在裁判侧实现里（`track2_judge.runner.player_extract` /
# `player_manifest`）—— 与评测器共用同一份判据，不在这里重写一遍 shell 版。
# 解包是原子落盘的：校验失败不会在 $PLAYER_DEST 留下半个包。
_player_runner_py() {
    # 优先 PAOS 的 venv（它已经 import 得到 track2_judge 时最好），
    # 否则用标定环境 —— 反正只是跑一段标准库 + track2_judge 的代码。
    if "$HOOK_PY" -c 'import sys; sys.path.insert(0, "'"$TRACK2_PHYSICS_ROOT"'"); import track2_judge' 2>/dev/null; then
        printf '%s' "$HOOK_PY"
    else
        printf '%s' "$TRACK2_PYTHON"
    fi
}

inject_player() {
    local src="${TRACK2_PLAYER_PACKAGE:-}"

    if [ -z "$src" ]; then
        # ⚠️ 必须**清掉上一轮的残留** —— 否则换个 case 却复用旧选手包。
        # 那种失败是静默的（跑出来的成绩属于上一个包），比报错危险得多。
        if [ -e "$PLAYER_DEST" ]; then
            rm -rf "$PLAYER_DEST"
            echo "[track2-runtime] 未提供选手包，已清除上一轮残留：${PLAYER_DEST}"
        fi
        echo "[track2-runtime] player  = <无选手包（自测模式）>"
        return 0
    fi

    if [ ! -f "$src" ]; then
        echo "[track2-runtime] FATAL: TRACK2_PLAYER_PACKAGE 指向的文件不存在：$src" >&2
        return 1
    fi

    local rc=0
    PYTHONPATH="$TRACK2_PHYSICS_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    TRACK2_PLAYER_DEST="$PLAYER_DEST" \
    TRACK2_PLAYER_SRC="$src" \
    "$(_player_runner_py)" -I - <<'PY' || rc=$?
import os, sys
from pathlib import Path

sys.path.insert(0, os.environ["TRACK2_PHYSICS_ROOT"])
from track2_judge.runner.player_extract import PlayerPackageError, extract_player_package
from track2_judge.runner.player_manifest import PlayerManifestError, validate_player_dir

dest = Path(os.environ["TRACK2_PLAYER_DEST"])
if dest.exists():
    import shutil
    shutil.rmtree(dest)
try:
    extracted = extract_player_package(os.environ["TRACK2_PLAYER_SRC"], dest)
    manifest = validate_player_dir(dest)
except (PlayerPackageError, PlayerManifestError) as exc:
    print(f"[track2-runtime] FATAL: 选手包被拒：{exc}", file=sys.stderr)
    raise SystemExit(1)
print(f"[track2-runtime] player  = {manifest.root}")
print(f"[track2-runtime]           skill={manifest.skill_name} "
      f"version={manifest.version} entrypoint={manifest.relative_entrypoint}")
print(f"[track2-runtime]           tools={','.join(manifest.tool_profile)} "
      f"code={extracted.code_bytes}B model={extracted.model_bytes}B")
PY
    # ⚠️ 不要依赖 `set -e` 让这里提前中止 —— 这个函数是在
    #    `if ! inject_player` 的条件上下文里调的，**函数体内 set -e 被抑制**，
    #    失败会一路走到底。所以上面显式 `|| rc=$?`，这里显式判。
    if [ "$rc" -ne 0 ]; then
        rm -rf "$PLAYER_DEST"
        return 1
    fi
    return 0
}

if ! inject_player; then
    echo "[track2-runtime] 选手包注入失败 —— 中止 start（不带着残缺的选手包跑评测）" >&2
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
