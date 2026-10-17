"""`track2-player` —— 在沙箱里跑选手包的 dora 节点（第 4 步 / A′-1）。

## 它在链路里的位置

```text
   evaluator          dora flow（官方 Bundle）
      │                 ┌────────────────────────────────────────────┐
      │  start.sh 注入  │  gateway  ◀─ HTTP ─  player ─▶ 沙箱(选手码) │
      └────────────────▶│     ▲                            │         │
        profiles/sim/   │     └─ dora ─▶ runtime ◀ 物理 ◀──┘         │
        player/         └────────────────────────────────────────────┘
```

* **runtime 节点**提供三个 Tool（本地 provider，经 dora 通道接 gateway）。
* **player 节点**（本文件）把选手代码跑起来，并把它的 Tool 调用打到 gateway。
* **gateway** 是官方二进制，负责 `/tools` 与 `/invocations` 路由。

## 为什么不直连 HTTP

沙箱**保留了 `--net`**（自己的网络命名空间），所以沙箱内**连不到宿主上的
gateway**（实测：`work/scripts/check_sandbox_official.py`）。走既有的 JSON-lines 桥：

```text
沙箱内 tools.joint_control(q, g)
  → 协议 stdout/stdin（package_host.py:ToolClient）
  → 本节点（SandboxBridge → ToolClient(HttpTransport)）
  → POST http://127.0.0.1:<port>/tools/...:invoke
  → 官方 gateway → dora 通道 → runtime 节点 → ManiSkill
```

## 一档沙箱（2026-10-17 起）

**所有选手包都在同一档里跑**：官方 ML runtime（Python 3.12.14 + torch/CUDA +
官方预装依赖）+ 一张 GPU。旧契约里的 `requires_gpu` 字段**已删除**
（`player_manifest.py` 会拒掉它，不静默忽略）。

隔离强度没有退化 —— 独立 mount/pid/user namespace、seccomp、只读挂载、
看不到宿主与裁判目录、网络仍隔离（工具调用走 JSON-lines 桥）。取消的只是
「不带模型的选手享受更弱权限」这条区分。

官方 runtime 起不来时**明确失败**，不静默降级 —— 带着一个残缺环境跑完
20 个回合，比直接失败糟得多。

## 节点什么时候结束

选手包跑完（或超时/出错）就结束 `run()`。`start.sh` 在**注入阶段**已经校验过
一次选手包，这里是**第二道**（也是真正执行的那一道）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pyarrow as pa                                     # noqa: E402
from dora import Node                                    # noqa: E402

from track2_judge.runner.player_manifest import (        # noqa: E402
    PlayerManifest, PlayerManifestError, validate_player_dir,
)
from track2_judge.runner.skill_manifest import (         # noqa: E402
    SkillPackage, SkillValidationError,
)
from track2_judge.sandbox.package import PackageExecutor  # noqa: E402
from track2_judge.sandbox.rpc import SecurityNotReady, SkillTimeout  # noqa: E402
from track2_judge.toolapi.client import (                # noqa: E402
    HttpTransport, SandboxBridge, ToolCallError, ToolClient,
)

__all__ = ["PlayerConfig", "PlayerConfigError", "PlayerNode", "main", "start_player"]

# 选手侧方法名 → Tool ID。与 `toolapi/client.py` 的别名表同一套。
METHOD_TO_TOOL = {
    "observe": "track2.env.observe",
    "joint_control": "track2.robot.joint_control",
    "step": "track2.robot.joint_control",
    "piper_joint_control": "track2.robot.joint_control",
    "gripper_control": "track2.robot.gripper_control",
    "piper_gripper_control": "track2.robot.gripper_control",
}


class PlayerConfigError(RuntimeError):
    """配置/环境不满足 —— **不降级、宁可不起**。"""


class PlayerConfig:
    """运行参数。全部来自环境（由 `skill.yaml` 的 `environment` 或 start.sh 注入）。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.profile = (env.get("PAOS_SKILL_PROFILE") or "sim").strip() or "sim"
        self.bundle_root = Path(env.get("PAOS_SKILL_ROOT") or ".").expanduser()
        self.gateway_url = (env.get("TRACK2_GATEWAY_URL")
                            or "http://127.0.0.1:19110").strip()
        default_player = self.bundle_root / "profiles" / self.profile / "player"
        self.player_dir = Path(
            env.get("TRACK2_PLAYER_DIR") or default_player).expanduser()
        self.timeout_s = float(env.get("TRACK2_PLAYER_TIMEOUT_S") or 600)
        self.tool_call_budget = int(env.get("TRACK2_TOOL_CALL_BUDGET") or 2400)
        self.action_timeout_s = float(env.get("TRACK2_ACTION_TIMEOUT_S") or 30)
        self.mode = (env.get("TRACK2_MODE") or "final").strip() or "final"
        # 等 gateway + provider 注册就绪的上限（建场景要几十秒）
        self.ready_timeout_s = float(env.get("TRACK2_PLAYER_READY_TIMEOUT_S") or 300)

    @property
    def gateway_port(self) -> int:
        from urllib.parse import urlparse
        return urlparse(self.gateway_url).port or 80


class ProfileGatedBridge(SandboxBridge):
    """在 `SandboxBridge` 上再加一道「选手**声明过**的 Tool 集合」门禁。

    为什么 Gateway 之外还要这一层：两者的 allowlist 不是一回事 ——
      * Gateway 的来自 Bundle 的 `required_tools`（**官方全集**）；
      * 这里的是选手 `tool_profile`（**本人子集**）。
    两层的关系是「全集」与「子集」，都在调用路径上，缺一不可。
    """

    def __init__(self, client: ToolClient, allowed: tuple[str, ...]) -> None:
        super().__init__(client)
        self.allowed = frozenset(allowed)

    def __call__(self, method: str, payload: dict | None = None):
        tool_id = METHOD_TO_TOOL.get(method, method)
        if tool_id not in self.allowed:
            raise ToolCallError(
                f"tool {tool_id!r} is not declared in this skill's tool_profile "
                f"({', '.join(sorted(self.allowed))})")
        return super().__call__(method, payload)


class PlayerNode:
    """dora 节点。**没有选手包时干净空转**（自测模式）。"""

    def __init__(self, config: PlayerConfig) -> None:
        self.config = config
        self.node = Node()
        self.manifest: PlayerManifest | None = None
        self.summary: dict[str, Any] = {"status": "not_started"}

    # ── 准备

    def prepare(self) -> None:
        root = self.config.player_dir
        if not root.is_dir():
            self.summary = {"status": "no_player",
                            "reason": f"no contestant package at {root}"}
            print(f"[track2-player] 没有选手包（{root}）—— 节点空转。",
                  file=sys.stderr, flush=True)
            return
        try:
            self.manifest = validate_player_dir(root, where="player node")
        except PlayerManifestError as exc:
            self.summary = {"status": "rejected", "reason": str(exc)}
            print(f"[track2-player] 选手包被拒：{exc}", file=sys.stderr, flush=True)
            return
        print(f"[track2-player] 选手包 OK skill={self.manifest.skill_name} "
              f"entrypoint={self.manifest.relative_entrypoint} "
              f"tools={','.join(self.manifest.tool_profile)}",
              file=sys.stderr, flush=True)

    # ── 沙箱

    def _sandbox_spec(self, manifest: PlayerManifest):
        """唯一一档：官方 runtime（Python 3.12.14 + 预装依赖 + 一张 GPU）。

        旧的 `stdlib` 档已取消（2026-10-17）。它存在的前提是「不带模型的选手
        不该拿到 torch」—— 但两档的**隔离机制本来就是同一套**，差的只是
        Python 版本、有没有第三方包、`/dev/shm`、`/proc` 与 CPU 配额。
        留着它反而制造了一个真实的坑：**选手在 3.12 上写、到 3.10 档炸**。

        起不来就**明确失败** —— 带着残缺环境跑完 20 个回合比直接失败糟得多。
        """
        from track2_judge.sandbox.official_runtime import (
            RuntimeSpecError, check_official_runtime, official_spec,
        )

        try:
            spec = official_spec(gateway_port=self.config.gateway_port)
        except RuntimeSpecError as exc:
            raise PlayerConfigError(
                f"official runtime is unavailable: {exc}") from exc
        problems = check_official_runtime(spec)
        if problems:
            raise PlayerConfigError(
                "the sandbox cannot be provided: " + "; ".join(problems))
        return spec, "official"

    # ── 等 gateway 就绪

    def wait_ready(self, timeout_s: float | None = None) -> bool:
        """轮询 `GET /tools` 直到 200。

        **必须有这一步**：`runtime` 节点建场景（ManiSkill/SAPIEN）要几十秒，
        而 player 节点几乎是立刻起来的。不等就调 `observe()` 会拿到
        `TRACK2_SCENE_NOT_READY` —— 看起来像「选手代码有问题」。

        另外，`/tools` 返回 200 只说明 gateway 起来了；
        runtime 是否注册成功看 `revision`（`endpoint.register` 被接受后才会 > 0）。
        """
        import urllib.error
        import urllib.request

        deadline = time.monotonic() + (timeout_s if timeout_s is not None
                                       else self.config.ready_timeout_s)
        url = self.config.gateway_url + "/tools"
        last = "no attempt"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    body = json.loads(response.read().decode("utf-8"))
                revision = (body.get("data") or {}).get("revision")
                if revision:
                    print(f"[track2-player] gateway 就绪 revision={revision}",
                          file=sys.stderr, flush=True)
                    return True
                last = f"revision={revision!r}"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = f"{type(exc).__name__}: {str(exc)[:80]}"
            time.sleep(2.0)
        print(f"[track2-player] gateway 等待超时（{last}）", file=sys.stderr, flush=True)
        return False

    # ── 跑一个 episode

    def _initial_observation(self, client: ToolClient,
                             bridge: "ProfileGatedBridge") -> dict[str, Any]:
        """进 `run()` 之前的**第一帧观测**（A2）。

        走的是**选手的那条路**（`bridge` → `ToolClient` → HTTP → gateway →
        runtime），不另开捷径 —— 所以这个观测与选手随后自己调
        `tools.observe()` 拿到的**完全同形**（同一个 ToolSpec 收窄、
        同样的 `images.camera`）。

        用 `bridge` 而不是 `client`：那一层是「选手声明过哪些 Tool」的门禁
        （`tool_profile` 子集）。选手没声明 `track2.env.observe` 时
        **这里就该失败** —— 否则等于评测器替他调了一个他没声明的 Tool。

        采不到时**明确失败**，不退回空 dict —— 空 dict 正是这条改动要消灭的
        东西：选手会在 `observation["robot"]` 上莫名其妙地 `KeyError`。
        """
        try:
            return bridge("observe", {})
        except Exception as exc:                                # noqa: BLE001
            raise PlayerConfigError(
                f"initial observation failed ({type(exc).__name__}: "
                f"{str(exc)[:200]})") from exc

    def run_episode(self, observation: dict | None = None) -> dict[str, Any]:
        assert self.manifest is not None
        manifest = self.manifest
        work = (self.config.bundle_root / "profiles" / self.config.profile
                / "player_work")
        work.mkdir(parents=True, exist_ok=True)

        try:
            package = SkillPackage(manifest.root)
        except (SkillValidationError, ValueError) as exc:
            raise PlayerConfigError(f"skill package rejected: {exc}") from exc

        spec, sandbox_profile = self._sandbox_spec(manifest)
        print(f"[track2-player] 沙箱档 = {sandbox_profile}", file=sys.stderr, flush=True)

        client = ToolClient(
            HttpTransport(self.config.gateway_url, timeout_s=30.0),
            action_timeout_s=self.config.action_timeout_s,
            max_action_steps=self.config.tool_call_budget,
        )
        bridge = ProfileGatedBridge(client, manifest.tool_profile)

        status = "ok"
        skill_result: dict[str, Any] = {}
        lifecycle: list[str] = []
        executor = PackageExecutor(package, work, isolated=True, trusted=False,
                                   timeout_s=self.config.timeout_s, spec=spec)
        try:
            executor.initialize({
                "skill_api_version": manifest.api_version,
                "mode": self.config.mode,
                "environment": self.config.profile,
                "tool_call_budget": self.config.tool_call_budget,
                # 沙箱档是**运行事实**，选手需要知道 GPU 能不能用。
                # 它不含任何裁判私有信息。
                "sandbox": {"profile": sandbox_profile, "gpu": True},
            })
            try:
                # ⚠️ **初始观测必须真的传进去**（A2 / 2026-10-17）。
                #
                #    以前这里是 `observation or {}`，而调用点是
                #    `self.run_episode()`（无实参）—— 于是选手的
                #    `run(observation, tools)` 收到的**永远是空 dict**。
                #    官方示例写的 `observation["robot"]["joint_position"]`
                #    会直接 `KeyError`；而裁判侧直跑那条路传的是**完整观测** ——
                #    同一份 skill 在两条路上拿到的东西不一样。
                #
                #    现在统一成「先采一次再进去」，与裁判侧同形：
                #    `run()` 的第一个参数 = `tools.observe()` 的返回（经 ToolSpec 收窄）。
                #
                #    ⚠️ 位置：**在 `executor.initialize()` 之后** —— 选手的
                #    `initialize(context)` 可能要先做自己的准备；采观测不依赖选手，
                #    但顺序上放在 initialize 之后更贴近「选手先就绪，再拿第一帧」。
                #
                #    ⚠️ 这一次 observe 也**会推一帧**（`_publish_frame` 挂在 observe 上），
                #    与「`observe` 是唯一采样点」的语义一致，不是多余的。
                if observation is None:
                    observation = self._initial_observation(client, bridge)
                skill_result = executor.run(
                    observation, bridge,
                    timeout_s=self.config.timeout_s,
                    max_requests=self.config.tool_call_budget)
            except SkillTimeout:
                status = "timeout"
        except SecurityNotReady as exc:
            status = "security_not_ready"
            skill_result = {"error": str(exc)}
        finally:
            lifecycle = list(executor.lifecycle)
            executor.close()

        return {
            "status": status,
            "skill": manifest.skill_name,
            "sandbox_profile": sandbox_profile,
            "tool_calls": len(bridge.calls),
            "lifecycle": lifecycle,
            "result": skill_result,
        }

    # ── 主循环

    def run(self) -> int:
        self.prepare()
        if self.manifest is not None:
            try:
                if not self.wait_ready():
                    raise PlayerConfigError(
                        f"gateway at {self.config.gateway_url} never became ready")
                self.summary = self.run_episode()
            except (PlayerConfigError, OSError, ValueError, RuntimeError) as exc:
                self.summary = {"status": "error",
                                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                "skill": self.manifest.skill_name}
                # ⚠️ 打印**完整**回溯。只打 `str(exc)` 会得到
                #    `package execution/protocol rejected` 这种
                #    来自协议层的笼统消息，真正的根因（HTTP 失败、
                #    物理拒绝、超时）就丢了 —— 白绕一轮。
                import traceback
                print("[track2-player] " + traceback.format_exc(),
                      file=sys.stderr, flush=True)
            # 摘要一行（不含 result，避免刷屏）
            print("[track2-player] 结束：" +
                  json.dumps({k: v for k, v in self.summary.items() if k != "result"},
                             ensure_ascii=False), file=sys.stderr, flush=True)
            # 选手返回的东西单独打，**有界**截断 —— 它是选手自己写的，
            # 可能很大，但正是排查时最需要看的。
            skill_result = self.summary.get("result")
            if skill_result:
                text = json.dumps(skill_result, ensure_ascii=False, default=str)
                print("[track2-player] 选手返回：" + text[:8000],
                      file=sys.stderr, flush=True)
            self._emit(self.summary)

        # dora 节点要**持续消费输入**，否则上游发消息会阻塞。
        for event in self.node:
            if event["type"] == "STOP":
                break
        return 0

    def _emit(self, payload: dict[str, Any]) -> None:
        """把结果发到 `player_result` 输出通道（没有下游时静默忽略）。"""
        try:
            self.node.send_output(
                "player_result",
                pa.array([json.dumps(payload, ensure_ascii=False, default=str)]))
        except Exception:                                      # noqa: BLE001
            pass


def start_player(config: PlayerConfig | None = None) -> int:
    return PlayerNode(config or PlayerConfig()).run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Track 2 contestant player node")
    parser.add_argument("--config", default=None,
                        help="保留位（与 runtime 节点的 CLI 形状一致）")
    parser.parse_args()
    return start_player()


if __name__ == "__main__":
    raise SystemExit(main())
