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
from track2_judge.sandbox.rpc import (                    # noqa: E402
    BudgetTimeout, SecurityNotReady, SkillTimeout,
)
from track2_judge.toolapi.client import (                # noqa: E402
    ActionStoppedUnknown, ActionTimeout, HttpTransport, SandboxBridge,
    ToolCallError, ToolClient,
)

#: 选手侧「链路故障」的显式归类（2026-10-18）。
#
# 它们是 `ToolCallError` 的子类，**不继承** `BudgetTimeout` —— 归到
# `infrastructure_timeout`。这样 `ResultGate` 能一眼把「正常预算耗尽」与
# 「gateway/链路坏了」分开：前者放行（按当前物理状态判分），后者阻断。
INFRASTRUCTURE_ERRORS = (ActionStoppedUnknown, ActionTimeout, ToolCallError)

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
        self.action_timeout_s = float(env.get("TRACK2_ACTION_TIMEOUT_S") or 5)
        self.mode = (env.get("TRACK2_MODE") or "final").strip() or "final"
        # 等 gateway + provider 注册就绪的上限（建场景要几十秒）
        self.ready_timeout_s = float(env.get("TRACK2_PLAYER_READY_TIMEOUT_S") or 300)
        # Judge 私有控制口的地址（B1/B4）。**不给 = 空串 = 关闭常驻**，
        # 此时跑完一次 `run_episode()` 就空转 —— 回归/单例评测依赖这个行为。
        self.control_url = self._control_url(env)

    def _control_url(self, env: dict[str, str]) -> str:
        """从环境变量**或** Bundle 里的控制口文件取端口。

        ⚠️ **两个来源，缺一不可**：

        | 场景 | 谁 spawn 本节点 | 环境变量在不在 |
        |---|---|---|
        | `dora run`（前台，测/调试） | 本地 CLI | ✅ 在 |
        | `dora start --detach`（正式） | **长命的 dora daemon** | ❌ **不在** |

        daemon 是常驻进程，它的环境里没有评测时设的变量（实测
        `/proc/<daemon>/environ` 里 0 个 `TRACK2*`）。只读环境变量的话，
        正式形态下本节点会**静默地**不常驻 —— 表现是「judge 下发 episode
        之后一直等不到结果」，而日志里一个错都没有。

        所以正式形态走**文件**：裁判在起 flow 之前把端口写到
        `profiles/<profile>/.judge_control.json`，收工后删掉。
        那个路径在 Bundle 里，而选手沙箱只挂 `player/` 一个目录 —— 选手看不到。
        """
        port = (env.get("TRACK2_JUDGE_CONTROL_PORT") or "").strip()
        if not port:
            # ⚠️ **不要用 `self.bundle_root` 拼这个路径**。`bundle_root` 来自
            #    `PAOS_SKILL_ROOT`，而这个变量在 `--detach` 形态下不可靠
            #    （实测节点拿到的是**别的** Bundle 的值）。包装脚本
            #    `run_player.sh` 已经用 `$(pwd)`（= dora 设的 `profiles/<p>/`）
            #    算好了绝对路径，直接用。
            marker = Path(env.get("TRACK2_CONTROL_MARKER")
                          or (self.bundle_root / "profiles" / self.profile
                              / ".judge_control.json"))
            try:
                port = str(json.loads(marker.read_text(encoding="utf8"))
                           .get("port") or "").strip()
            except (OSError, ValueError, AttributeError):
                port = ""
        if not port:
            return ""
        host = (env.get("TRACK2_JUDGE_CONTROL_HOST") or "127.0.0.1").strip()
        return f"http://{host}:{int(port)}"

    @property
    def results_path(self) -> Path | None:
        """常驻模式下把每个 episode 的结果**追加**到这里（JSON Lines）。

        ⚠️ 为什么落到文件而不是只发 dora 输出通道：正式形态是
        `dora start --detach`，那种形态下节点 stdout **不回流**到 Judge 的
        日志文件（日志住在 daemon 里，只能 `dora logs` 取）。而 Judge 要
        「下发 episode → 等这一轮真的跑完」。文件是最直接的握手：
        Judge 起 flow 前清空、每轮读新增行。

        不常驻（`control_url` 为空）时**不写** —— 回归/单例评测不受影响。
        ⚠️ 这个方法挂在 **`PlayerConfig`** 上 —— 里面是 `self.control_url`，
        不是 `self.config.control_url`（第一版写错了，异常被 `_emit` 的兜底
        `except` 吃掉，表现成「文件一直不出现」而**没有任何报错**）。
        """
        if not self.control_url:
            return None
        # 与 `_control_url` 同一个理由：用包装脚本算好的 cwd，不用 `bundle_root`。
        base = os.environ.get("TRACK2_PROFILE_DIR")
        if base:
            return Path(base) / ".episode_results.jsonl"
        return (self.config.bundle_root / "profiles" / self.config.profile
                / ".episode_results.jsonl")

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
        # 沙箱进程的**宿主侧 pid**。进 summary 是给裁判核 B4 用的：
        # 「第 2 个 episode 的选手进程是不是新的」。沙箱内部 `os.getpid()` 永远
        # 是 1（自己的 PID 命名空间），**选手自己报不出来这件事**。
        sandbox_pid = executor.process.p.pid
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
            except BudgetTimeout:
                # ★ 正常预算耗尽（600 s 墙钟 / 控制步预算）—— **不是执行异常**。
                #   语义：到时间 → 停止执行 → 冻结当前物理状态 → 照常按 P1/P2/C1 判分。
                #   `ResultGate` 放行这个终态。
                status = "budget_timeout"
            except INFRASTRUCTURE_ERRORS:
                # ★ 链路故障（丢包 / 503 / 动作超时且物理状态不明）——
                #   **不能伪装成正常超时**，`ResultGate` 要按执行异常阻断。
                status = "infrastructure_timeout"
            except SkillTimeout:
                # 兜底：仍是 `SkillTimeout` 但不属于上面两类（理论上不该出现）。
                status = "infrastructure_timeout"
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
            "sandbox_pid": sandbox_pid,
            "tool_calls": len(bridge.calls),
            "lifecycle": lifecycle,
            "result": skill_result,
        }

    # ── 主循环

    def run(self) -> int:
        self.prepare()
        if self.manifest is None:
            self._idle()
            return 0
        try:
            if not self.wait_ready():
                raise PlayerConfigError(
                    f"gateway at {self.config.gateway_url} never became ready")
        except (PlayerConfigError, OSError, ValueError, RuntimeError) as exc:
            self._report_error(exc)
            self._idle()
            return 0

        # ── B4：Judge 起了控制口 → **常驻**，一个 flow 里连续跑多个 episode
        #
        # 以前跑完一次 `run_episode()` 就直接**空转等 STOP**，所以「循环归 Judge，
        # flow 常驻」那条边界落不了地（Judge 只能靠起停 flow 来换 episode，
        # 而每次起停要 ~33 s，且 20 个回合 × 73 个 case 完全不可行）。
        #
        # 常驻时的形状：
        #
        #   Judge ──(私有通道 POST /episode/reset)──▶ runtime 节点（换 case/seed）
        #     │                                        │
        #     └──── 轮询 GET /episode/snapshot ◀───────┘
        #                    ▲
        #                    │ 本节点（player）看 `seq` 变了就跑一轮 skill
        #
        # ⚠️ **不设 `TRACK2_JUDGE_CONTROL_PORT` 时行为完全不变**：跑一次
        #    `run_episode()` 就进空转 —— 21 项回归与单例评测都依赖它。
        #    常驻路径下**不预跑** episode：第一个 episode 由 Judge 的第一次
        #    `reset` 下发（`seq=1`），否则会白跑一轮。
        if self.config.control_url:
            self._follow_episodes(self.config.control_url)
            return 0

        try:
            self.summary = self.run_episode()
        except (PlayerConfigError, OSError, ValueError, RuntimeError) as exc:
            self._report_error(exc)
        self._report_summary()
        self._emit(self.summary)
        self._idle()
        return 0

    # ── 收尾 / 空转

    def _idle(self) -> None:
        """dora 节点必须**持续消费输入**，否则上游发消息会阻塞。"""
        for event in self.node:
            if event["type"] == "STOP":
                return

    def _report_error(self, exc: BaseException) -> None:
        self.summary = {"status": "error",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "skill": self.manifest.skill_name if self.manifest else None}
        # ⚠️ 打印**完整**回溯。只打 `str(exc)` 会得到
        #    `package execution/protocol rejected` 这种
        #    来自协议层的笼统消息，真正的根因（HTTP 失败、
        #    物理拒绝、超时）就丢了 —— 白绕一轮。
        import traceback
        print("[track2-player] " + traceback.format_exc(), file=sys.stderr, flush=True)
        self._report_summary()

    def _report_summary(self) -> None:
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

    # ── 常驻模式（B4）

    _POLL_S = 0.5

    def _follow_episodes(self, control: str) -> None:
        """轮询私有控制口的 `seq`，每变一次就跑一个 episode。

        **为什么是轮询而不是推送**：dora 不提供外部 publish（`dora topic` 只有
        list/echo/hz/info，实测），而选手侧将来要连的 gateway 也不能被这套
        私有通道污染。`0.5 s` 的轮询相对于 `211 s` 的 episode 完全可忽略。

        **两种等待用同一个超时**（`Node.next(timeout)`）—— 一个循环同时
        「收 dora 事件」与「看控制口」，不需要另起线程。收到 `STOP` 立即退出。
        """
        import json as _json
        import time
        import urllib.request

        seen = 0
        seen_attempt = 0
        print(f"[track2-player] 常驻模式，控制口 {control}",
              file=sys.stderr, flush=True)
        while True:
            if self._stop_requested():
                print("[track2-player] 收到 STOP，退出常驻",
                      file=sys.stderr, flush=True)
                return

            try:
                with urllib.request.urlopen(
                        control + "/episode/snapshot", timeout=10) as r:
                    snapshot = _json.loads(r.read())
            except Exception as exc:                            # noqa: BLE001
                print(f"[track2-player] 控制口不可用（{type(exc).__name__}），稍后重试",
                      file=sys.stderr, flush=True)
                time.sleep(2.0)
                continue

            seq = int((snapshot.get("episode") or {}).get("seq") or 0)
            attempt = int((snapshot.get("episode") or {}).get("attempt") or 1)
            # `seq == 0` = **还没有任何 episode**（runtime 的计数器从 0 起，
            # 第一次 `reset` 才变 1）。不排除它的话，节点一起来就会先跑一轮
            # 「空 episode」—— 而第一轮观测还是未 reset 的场景。
            if seq <= 0 or (seq == seen and attempt <= seen_attempt):
                continue
            fresh = seq > seen                 # 新 episode（要新建沙箱进程）
            seen, seen_attempt = seq, attempt
            self._current_seq = seq
            self._current_attempt = attempt
            episode = snapshot.get("episode") or {}
            print(f"[track2-player] 新 episode seq={seq} attempt={attempt} "
                  f"case={episode.get('case_id')} seed={episode.get('seed')} "
                  f"mode={episode.get('mode')}"
                  + ("" if fresh else "（同一 episode 的追加执行，**不 reset**）"),
                  file=sys.stderr, flush=True)
            try:
                # ⚠️ 每一轮都**新建** `PackageExecutor`（B3-a）：选手进程是新的，
                #    模块级状态不会跨 episode 泄漏 —— 否则第 2 个 episode 会拿到
                #    上一轮残留的全局变量，而且「同一份 skill 两次结果不一致」
                #    这种 bug 会被裁判当成随机性。
                #
                #    `attempt`（同一 episode 的第 2/3 次）**同样**新建 ——
                #    `final` 的语义是「**裁判**看到失败后再给一次机会」，
                #    每次机会都该是干净的一次尝试，而不是接在某个残留的
                #    进程状态上。
                self.summary = self.run_episode()
                self._report_summary()
            except Exception as exc:                            # noqa: BLE001
                # 常驻循环里的兜底 = **链路/编排层故障**（不是选手预算耗尽）。
                # 归到 `infrastructure_timeout`，让 `ResultGate` 按执行异常阻断 ——
                # 绝不伪装成正常超时。
                self.summary = {"status": "infrastructure_timeout",
                                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                "skill": self.manifest.skill_name}
                print("[track2-player] episode 失败：" + self.summary["error"],
                      file=sys.stderr, flush=True)
            self._emit(self.summary)

    def _stop_requested(self) -> bool:
        """带超时地看一眼 dora 输入；`timeout` 到了就返回 False。

        ⚠️ 不能用 `for event in self.node:` —— 那是**阻塞**的，常驻循环会被
        卡死在这里，HTTP 轮询再也不执行（第一版就是这么写的，等于没常驻）。
        """
        try:
            event = self.node.next(timeout=self._POLL_S)
        except Exception:                                      # noqa: BLE001
            return False
        if not event:
            return False
        return event.get("type") == "STOP"

    def _emit(self, payload: dict[str, Any]) -> None:
        """把结果发到 `player_result` 输出通道（没有下游时静默忽略），
        并在常驻模式下**同时落盘**（Judge 靠它握手，见 `results_path`）。"""
        text = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            self.node.send_output("player_result", pa.array([text]))
        except Exception:                                      # noqa: BLE001
            pass
        try:
            path = self.config.results_path
        except Exception:                                      # noqa: BLE001
            return
        if path is None:
            return
        try:
            # `seq` 要跟着走 —— Judge 靠它把「结果」与「哪一轮」对上。
            record = {"seq": int(getattr(self, "_current_seq", 0) or 0),
                      "attempt": int(getattr(self, "_current_attempt", 1) or 1),
                      "result": payload}
            with path.open("a", encoding="utf8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
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
