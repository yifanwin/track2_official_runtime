"""把 ManiSkill 场景包成官方 `forge_tool` 的 ToolEndpoint。

## 三层职责

```text
Track2SimulationEndpoints          ← 业务层：case/seed → 场景
├── observe()      QueryToolEndpoint   ← track2.env.observe
└── joint_control / gripper_control    ← 两个 Action，共享一个 endpoint
      ActionToolEndpoint
        start()  下发一步关节目标
        status() 报执行相位
        result() 报终态结果
        cancel() 请求取消（**不代表物理已停**）
```

## 与第 2 步自建实现的对应关系

第 2 步在 `toolapi/gateway.py` 里自己实现了 Action 生命周期（`ActionRecord` +
`cancel_status` / `cancel_effective` / `execution_status` 三态）。现在这套交给官方
`forge_tool.ToolEndpointHandler` 管，我们只实现它要的四个方法，并**保证三态语义不丢**：

| 第 2 步 | 官方 | 我们在这里怎么给 |
|---|---|---|
| `cancel_status` | `ToolControlResponse.status`（`accepted`/`rejected`/`unsupported`/`terminal`） | `cancel()` 的返回值 |
| `execution_status` | 无官方字段 | 塞进 `ToolResult.details` |
| `cancel_effective` | 无官方字段 | 同上 |

**官方 wire 里没有「取消是否真的阻止了物理效果」这个概念**，所以我们把它放在
`details` 里带着走 —— 这是 Q13 的要求，不能因为换了实现就丢掉。

## 线程模型

ManiSkill/SAPIEN 是**阻塞的、非线程安全**的。而 `forge_tool` 的 provider 接口是 `async`。

所以：
- `start()` 立刻返回 `ToolAccepted`（**admission 不是完成**）
- 物理在**单线程 executor** 里跑（`ThreadPoolExecutor(max_workers=1)` → Q9 的串行化）
- `status()` / `result()` 读执行记录，不碰物理
- `cancel()` 只置标志 + 有界等确认
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from forge_tool import (
    ActionToolEndpoint,
    QueryToolEndpoint,
    ToolAccepted,
    ToolContext,
    ToolControlResponse,
    ToolError,
    ToolEventEmitter,
    ToolExecutionKey,
    ToolExecutionStatus,
    ToolRequest,
    ToolResult,
    ToolResultResponse,
)

from track2_judge.toolapi import schema as schema_mod
from track2_judge.toolapi.validation import SchemaError

__all__ = ["Track2SimulationEndpoints", "EXEC_RUNNING", "EXEC_STOPPED_CONFIRMED", "EXEC_UNKNOWN"]

# ── execution_status 的三个取值（Q13）
EXEC_RUNNING = "running"
EXEC_STOPPED_CONFIRMED = "stopped_confirmed"   # 有正面证据：执行体已退出
EXEC_UNKNOWN = "unknown"                       # 拿不到证据 —— 只能如实说不知道

_PHASE_COMPLETED = "completed"
_PHASE_FAILED = "failed"
_PHASE_CANCELLED = "cancelled"
_PHASE_UNKNOWN = "unknown"

#: 幂等守卫保留的最近 key 数（见 `start()`）。远大于「一条在飞请求的重复投递」
#: 所需的重叠窗口（重复投递发生在毫秒级），所以不会误放行。
_STARTED_KEY_KEEP = 4096


@dataclass
class _Execution:
    """一次 Action 的账。字段与 Q13 的三态语义一一对应。"""

    endpoint_id: str
    operation: str
    key: ToolExecutionKey
    arguments: dict[str, Any]
    phase: str = "accepted"                 # accepted → running → 终态
    result: ToolResult | None = None
    cancel_requested: bool = False
    cancel_effective: bool = False
    execution_status: str = EXEC_STOPPED_CONFIRMED   # 尚未开始执行
    started_at_s: float | None = None
    finished_at_s: float | None = None
    error_text: str | None = None
    # 终态事件要回 event loop 发 —— 执行体在 executor 线程里
    loop: object | None = None
    emitter: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


#: 结束一个 episode 的《终止事件名》，与 forge_tool 的映射一致（见 `_emit_terminal`）。
class _StrictJsonError(ValueError):
    """Oracle plan 里出现了 NaN / Infinity —— `json.loads` 会接受，下游 schema 不认。"""


def _strict_json(value: Any) -> None:
    import json as _json
    try:
        _json.loads(_json.dumps(value, allow_nan=False))
    except ValueError as exc:
        raise _StrictJsonError(str(exc)) from None


class Track2SimulationEndpoints(QueryToolEndpoint, ActionToolEndpoint):
    """两个 endpoint 的实现。用**同一个实例**，因为都碰同一个仿真环境。

    `forge_tool` 的 `ToolEndpointHandler` 接受 `operations: Mapping[str, object]`，
    所以一个类实例可以同时扮演 `observe` / `joint_control` / `gripper_control` 三个操作。
    """

    def __init__(self, adapter_factory, config, frame_sink=None):
        self._config = config
        self._adapter_factory = adapter_factory
        # 每帧推给谁（第 5 步）。`sink(camera_payload|None, control_steps)` ——
        # 由节点接到 dora 的 image 通道上（→ 官方 gateway → `/ws/images`）。
        # `None` 表示相机这一拍失败；**不推陈旧帧**。
        self._frame_sink = frame_sink
        self._frame_count = 0
        self._adapter = None                    # 只在 executor 线程里初始化
        # B1/B2：常驻 session 与当前 episode 的记账（**Judge-only**）
        self._session = None
        self._session_case: str | None = None
        #: 建 session 时用的参数签名。换 case / 换 seed / 换相机都对应**不同**
        #: 的 event policy 与白名单，必须重建 —— 只比 `case_id` 会串味。
        self._session_signature: tuple | None = None
        self._episode: dict = {}
        self._episode_seq: int = 0
        #: 恒为 1（取消 first/final 后不再有追加执行）。
        self._attempt_seq: int = 0
        #: `reset` 之后**立刻**采的初态（judge-only，B2'）。
        #: 判分要「选手动手之前」的布局，末态替代不了 —— 详见 `_reset_blocking`。
        self._initial_physical: dict | None = None
        self._initial_layout: dict | None = None
        #: 当前 case 的 bundle（`_constraints()` 要用它挑事件约束）
        self._bundle_cache: dict | None = None
        self._executions: dict[str, _Execution] = {}
        #: 幂等守卫（2026-10-19）：已 `start()` 过的完整 `ToolExecutionKey`
        #: 集合，按 `(invocation_id, attempt_id)` 去重。
        #: 有界 —— 只保留最近 `_STARTED_KEY_KEEP` 个（用 OrderedDict 当 FIFO），
        #: 免得长跑（250 episode × 多动作）无上限增长。
        self._started_keys: "OrderedDict[ToolExecutionKey, None]" = OrderedDict()
        self._execution_lock = threading.Lock()
        # Q9：物理串行化。max_workers=1 保证任何时候只有一个东西在碰 env。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="track2-physics")
        self._closed = False
        # ★ ②-A1（2026-10-18）：换 instance id 的钩子。
        #
        #   由 `node.py` 注入 —— 它持有 handler（`endpoint_instance_id` 是只读
        #   property）与所有 register 路径，能同时把新 id 同步过去。endpoints
        #   这边只在 `_reset_blocking` 的**最后**触发它，那时本 episode 的
        #   provider 侧工作已全部结束，没有在飞的动作会被误标 ambiguous。
        self.on_episode_started = None

    # ────────────────────────────────────────────── 生命周期

    def open(self) -> None:
        """在 executor 线程里建场景。由节点的启动流程调用。"""
        self._run_blocking(self._open_blocking)

    def _open_blocking(self) -> None:
        self._adapter = self._adapter_factory()

    def reset_episode(self, case_id: str, seed: int) -> dict:
        """**Judge-only**：在一个**常驻 flow** 里换 episode（B1）。

        这是「循环归 Judge，flow 常驻」那条边界的落地 —— Judge 通过
        **私有控制通道**调它，不是 Tool、不注册 ToolSpec、不进 gateway。

        语义（与 `requirement/Track2_边界决策.md` 一致）：

        * 每次调用**重建场景**（`adapter.reset` 内部是 close + factory；
          建场景实测 2.7 s，相对于 211 s 的 episode 可忽略）—— 见 C3
        * 重建后**重新装载公开输入**（新布局的参考图不变，标定不变，
          但 `initial_layout` 变了 —— 所以选手的第一帧必须是新的）
        * **不 reset 物理状态以外的东西**：选手进程由 player 节点负责（B3/B4）

        ★ 2026-10-18 取消 first/final：**不再有 `mode`**。每次调用就是一个
        episode；「失败后追加执行」的机制（`next_attempt`）已删除。
        `recovery` 恒 `False`。
        """
        if self._closed:
            raise ToolError(code="TRACK2_RUNTIME_CLOSED",
                            message="runtime is closed", retryable=False)
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ToolError(code="TRACK2_BAD_SEED",
                            message="seed must be a non-negative integer",
                            retryable=False)
        if not isinstance(case_id, str) or not case_id:
            raise ToolError(code="TRACK2_BAD_CASE",
                            message="case_id must be a non-empty string",
                            retryable=False)
        return self._run_blocking(self._reset_blocking, case_id, seed)

    def _reset_blocking(self, case_id: str, seed: int) -> dict:
        from track2_judge.observation.public_task_view import public_task_inputs
        from track2_judge.scenario.session import build_case_session

        adapter = self._require_adapter()
        previous = getattr(self, "_session_case", None)
        bundle = self._load_bundle(case_id)
        self._bundle_cache = bundle          # `_constraints()` 从这里取事件约束
        # ⚠️ **顺序要紧**：公开输入必须在建 session **之前**算出来。
        #
        #    第一版把 `public_fields=()` 传给了 `build_case_session()`，然后才在
        #    下面算 `public` —— 于是 `adapter.reset()` 里那句
        #    `if set(inputs) - self.__public_fields: raise ValueError('unapproved
        #    public input')` 直接抛错，整条私有通道**第一个 reset 就废**。
        #    白名单就是 `public_fields`，它必须与真正传进去的 dict 是同一套键。
        public = public_task_inputs(bundle, reference_root=self._reference_root(),
                                    case_id=case_id,
                                    camera=self._config.camera_required)

        # ★ G2：Oracle 规划开着时，公开输入的白名单要多放一个 `oracle_plan`
        #   （payload 在 reset 之后才算得出来，见下面那段）。普通评测没有它。
        oracle_on = bool(getattr(self._config, "oracle_planning", False))
        public_fields = tuple(public) + (('oracle_plan',) if oracle_on else ())

        # P5 checkpoints 仍要采（judge_snapshot 的 `physics_health` / P5 靠它，
        # 否则是「NOT CONFIGURED」，评分侧缺证据）。`recovery` 恒 False。
        checkpoints = next(
            (p["checkpoints"] for p in bundle["contract"].get("predicates", [])
             if p["type"] == "P5"), [])
        recovery = False          # ★ 取消 first/final 后恒 False
        # ★ **seed 必须在签名里** —— 否则换 seed 会静默地无效。
        #
        #   `build_case_session()` 建的 adapter，其 backend factory 是
        #   `lambda: create_case_env(bundle, casebank, seed, ...)` —— 那个 `seed`
        #   是**建 session 时**捕获的。而 `adapter.reset(seed=...)` 只是
        #   `close(); env = factory(); env.reset(seed=seed)`：**重新建场景用的是
        #   闭包里的旧 seed**，`env.reset(seed=...)` 本身不做布局随机化
        #   （布局来自 `initial_layouts[seed]`，在 factory 里选）。
        #
        #   实测症状：连着下发 seed=0、seed=3 两个 episode，布局与 RGB 帧的
        #   sha256 **逐位相同** —— 看起来像「选手看到的是旧题」，而日志里
        #   一个错都没有。这正是「两条路径各写一遍构造」这类漂移的典型形态。
        # ★ 2026-10-19：把 `_episode_seq` 也纳入签名 —— 语义是**每次 reset 都重建**。
        #
        #   上面那段注释说的「每次调用重建场景」本来就是本模块的契约，但原来的
        #   签名只含 `(case_id, seed, camera, recovery, checkpoints)`：**同 seed
        #   连续两次 reset 不会重建**，第二次会从第一次留下的物理状态继续。
        #
        #   这在旧口径（first/final 的「失败后不 reset 追加执行」）下是**故意的**；
        #   但 2026-10-18 取消 first/final 之后，它变成了一个**坑**：
        #   episode 级重测（`resident_runner.EPISODE_RETRIES`）要的是「重来一次」，
        #   而同一个 seed 重跑时签名不变 ⇒ 场景不重建 ⇒ 变成「从半途继续」——
        #   那就不是重测，是追加执行，会污染成绩。
        #
        #   `_episode_seq` 每次 `reset_episode` 都递增，所以放进签名就等价于
        #   「每 episode 必重建」，与本文档的契约一致。
        signature = (case_id, int(seed), bool(self._config.camera_required),
                     recovery, tuple(map(repr, checkpoints)),
                     int(getattr(self, "_episode_seq", 0)))
        # 换 case / 换 seed / 换 mode 都必须重建（bundle、布局与事件策略都不同）。
        if previous != case_id or self._session is None \
                or self._session_signature != signature:
            self._session = build_case_session(
                bundle, self._bank(), seed,
                camera=self._config.camera_required,
                public_fields=public_fields,
                live=True,
                checkpoints=checkpoints,
                recovery=recovery,
                max_steps=self._config.max_steps,
                max_sim_time_s=self._config.max_sim_time_s,
                case_id=case_id)
            self._session_case = case_id
            self._session_signature = signature
            self._adapter = self._session.adapter

        self._session.reset(seed, public_task_inputs=public)

        # ★ G2：Oracle 通道。规划**必须在 reset 之后**（env 要有本 episode 的
        #   初始布局），但裁判在常驻结构下没有 env —— 所以在这里做。
        #
        #   与 legacy（`select_case.py` 的 `_planning_factory`）同款地 reset 两次：
        #   第一次不带 plan（只为拿到 env），规划完再带 plan reset 一次。
        #   两次用的是**同一个 adapter / 同一个 bundle / 同一个 seed**，
        #   所以规划看到的物理与选手执行时的初始状态是同一个。
        #   `initial_*` 快照在**第二次 reset 之后**才采（见下）。
        if oracle_on:
            plan = self._oracle_plan_blocking(case_id)
            public = {**public, "oracle_plan": plan}
            self._session.reset(seed, public_task_inputs=public)
        # ★ **reset 之后立刻**采一份物理真值（B2'）。
        #
        #   为什么必须在这里采：判分要 `initial_layout` —— 那是「选手动手**之前**
        #   物体在哪」。原来它由裁判侧 `candidate_runner.py:60` 在 `reset()`
        #   之后立刻读。常驻结构下裁判没有 env，只能从这里拿。
        #   只在快照里给**末态**是不够的：`mesa_eval` 会拿 initial 做配对校验
        #   （`first`/`final` 同 seed 的 initial 必须逐位相等），
        #   `ResultGate` 也会重算 canonical 哈希比一次（`PAIRED_STATE_INVALID`）。
        #   而「末态」用不了 —— 那正是选手操作过的结果。
        # ★ ②-A1（2026-10-18）：换 instance id 的钩子 —— **必须在 `seq` 递增
        #   **之前**调用。
        #
        #   顺序理由：player 是靠轮询 `/episode/snapshot` 看到 `seq` 变了才开始
        #   下一轮 skill 的。若先递增 `seq`、再换 id，player 可能在 gateway 还没
        #   处理完 `endpoint.register` 时就打出第一个动作 —— 那个动作会被
        #   gateway 按**旧** instance 钉住，而我们的 handler 已经换成新 id，
        #   于是路由校验失败。放在 `seq` 之前，注册请求至少已经发出去，
        #   加上 player 侧「轮询间隔（≥1 s）+ 沙箱创建（≈1.3 s）+ initialize」
        #   这段延迟，gateway 有充足时间处理完。
        #
        #   为什么换：一次丢包会让 gateway 的 `max_concurrency = 1` 并发围栏永久
        #   占死（详见 `plan/Track2_丢包自锁修复方案.md`）。换 id 触发 gateway 的
        #   `mark_instance_ambiguous`，把围栏要回来 —— 最坏情况从「本轮 run 剩下
        #   的全部 episode 都废」缩到「1 个 episode 受损」。
        #
        #   钩子由 `node.py` 注入（它持有 handler 与所有 register 路径）；
        #   取不到就跳过（手工起的裸 endpoints，自检场景，不影响）。
        self._initial_physical = self._session.adapter.get_final_state()
        self._initial_layout = self._session.adapter.get_layout()
        hook = getattr(self, "on_episode_started", None)
        if hook is not None:
            try:
                hook()
            except Exception:                                   # noqa: BLE001
                # 换 id 失败**不该让 episode 起不来** —— 它只是止血手段，
                # 不是正确性前提；真失败会在后续的 503 上暴露。
                pass
        # `seq` 单调递增 —— **player 节点靠它判断「有新 episode 了」**（B4）。
        # 不用 `(case_id, seed)` 比对：同一个 seed 重跑是合法操作。
        #
        # attempt 恒为 1（不再有追加执行）。
        self._episode_seq = int(getattr(self, "_episode_seq", 0)) + 1
        self._attempt_seq = 1
        self._episode = {"case_id": case_id, "seed": seed,
                         "seq": self._episode_seq, "attempt": self._attempt_seq}
        # ★ **初态要在这里采**（见上）—— 但 `final` 的第 2/3 次 attempt
        #   用的是**同一份初态**（不 reset），所以 `_initial_*` 不再更新。
        return {"ok": True, **self._episode}

    # ── 私有控制通道需要的三个取数（都不进 Tool 面）

    def _bank(self):
        from pathlib import Path
        from track2_judge.cli.select_case import CASEBANK
        return Path(self._config.casebank) if getattr(self._config, "casebank", None) else CASEBANK

    def _reference_root(self):
        from track2_judge.cli.select_case import REFERENCE_ROOT
        return REFERENCE_ROOT

    def _load_bundle(self, case_id: str) -> dict:
        import json
        path = self._bank() / "cases" / case_id / "case.json"
        if not path.is_file():
            raise ToolError(code="TRACK2_BAD_CASE",
                            message=f"case not found: {case_id}", retryable=False)
        return json.loads(path.read_text(encoding="utf8"))

    def judge_snapshot(self) -> dict:
        """**Judge-only**：当前**实际跑过的那个 env** 的物理快照（B2）。

        ⚠️ 绝对不许另建同 seed 场景再读 —— `same seed` 恢复不了选手执行产生的
        物体终态 / 碰撞 / 滑落 / 接触 / 动态误差。见边界决策 §3。
        """
        adapter = self._require_adapter()
        return self._run_blocking(self._judge_snapshot_blocking)

    def _judge_snapshot_blocking(self) -> dict:
        adapter = self._require_adapter()
        return {
            "episode": dict(getattr(self, "_episode", {}) or {}),
            # ── 末态（选手操作之后）
            "layout": adapter.get_layout(),
            "physical": adapter.get_final_state(),
            # ── 初态（`reset` 之后立刻采的那一份，B2'）
            #
            # 判分要「选手动手**之前**物体在哪」：`mesa_eval` 用它做配对校验，
            # `ResultGate` 用它比 canonical 哈希。**末态替代不了它**。
            # 见 `_reset_blocking` 里那次采样的注释。
            "initial_physical": self._initial_physical,
            "initial_layout": self._initial_layout,
            "physics_health": adapter.get_physics_health(),
            # ── 事件证据（P5 / 事件约束）
            #
            # 73 例 profile 下它是 `{'event_counts': {}, 'coverage': 'NOT CONFIGURED',
            # 'P5': {'status': 'NOT CONFIGURED'}}` —— 够用（那批题的
            # `event_constraints` 是空的）。但**必须随快照给出**，而不是让裁判
            # 自己编一个：一旦将来有题真的启用了事件约束，裁判编的那个会
            # 悄悄变成「证据缺失却通过」。
            "event_evidence": self._event_evidence(),
        }

    def judge_timings(self) -> dict:
        """**Judge-only**：本 flow 至今所有 Action 的执行耗时（Q14 的数据源）。

        ## 为什么需要它

        `runtime.yaml` 的 `action_timeout_s` 现在是 `30.0`，标着
        `MEASURED_73_CASES_P99_PLUS_MARGIN`（Q14：73 case 实测 p99 0.675 / max 0.940 s
        → 取 5.0 s，2026-10-18）。
        **数据其实一直在采**（`_run_action` 里每次都记 `started_at_s` /
        `finished_at_s`），只是没有任何出口 —— 于是「标定 Q14」看起来要专门跑一轮。

        加这个只读出口之后，全量验收跑（73 case）**顺带**就把分布采回来了。

        ## 边界

        * 只暴露**耗时**（秒），不含任何 GT / 位姿 / 目标信息
        * `_executions` 跨 episode 累积（`reset` 不清它）—— 这正是我们要的：
          整轮 flow 的分布，而不是单 episode 的
        * 走**私有控制口**，选手够不着（沙箱无网络，见 B5）
        """
        with self._execution_lock:
            records = list(self._executions.values())
        durations: list[float] = []
        by_operation: dict[str, list[float]] = {}
        unfinished = 0
        for record in records:
            start = getattr(record, "started_at_s", None)
            finish = getattr(record, "finished_at_s", None)
            if start is None or finish is None:
                unfinished += 1
                continue
            delta = float(finish) - float(start)
            durations.append(delta)
            by_operation.setdefault(record.operation, []).append(delta)
        return {
            "count": len(durations),
            "durations_s": durations,
            "by_operation": by_operation,
            "unfinished_or_rejected": unfinished,
            #: provider **实际收到过**的 invoke 请求数（记录在 `start()` 里建）。
            #:
            #: 判据 `received == count + unfinished_or_rejected` 恒成立 ——
            #: 所以它能与裁判侧的「客户端 202 次数」对照，抓出**纯丢包**
            #: （请求在 dora 通道上丢了，provider 连记录都没建）。
            #: 见 `plan/Track2_丢包自锁修复方案.md` §3.2 的盲区说明。
            "received": len(records),
            "action_timeout_s": float(getattr(self._config, "action_timeout_s", 0.0)),
            "action_timeout_source": str(
                getattr(self._config, "action_timeout_source", "")),
        }

    def _oracle_plan_blocking(self, case_id: str) -> dict:
        """在当前（已 reset 的）env 上做一次 GT 规划（G2）。

        ## 为什么规划搬到这里

        常驻结构下**裁判进程没有 env** —— env 在 runtime 节点里。所以
        `release_entry.py` 原来那句
        `raise ORACLE_PLAN_NOT_AVAILABLE_ON_RESIDENT_CHAIN` 是对的：
        在裁判侧根本做不了规划。

        但 runtime 节点**本来持有的就是执行选手代码的那个 env**，
        比 legacy 那条路（`select_case.py` 里 factory 现造一个）**更正确** ——
        规划看到的物理与选手执行时的初始状态**就是同一个**，不需要靠
        「两次 reset 用同一个 adapter」来论证。

        ## 只由 `_reset_blocking` 调用

        时机是硬的：必须在 `reset` **之后**（env 要有本 episode 的初始布局）、
        且在该 episode 的公开输入**定稿之前**（payload 要进 `oracle_plan` 字段）。
        所以它不是一条独立路由，而是 reset 内部的一步。
        规划**只读 env、不写** —— 选手面对的物理状态不受影响。

        ⚠️ 只有 `config.oracle_planning=True` 时才走到这里（见 `_reset_blocking`）。
        """
        from track2_judge.oracle.planning import (
            build_oracle_plan, find_target, oracle_executor_payload, serializable)

        adapter = self._require_adapter()
        bundle = self._load_bundle(case_id)
        task = bundle["contract"]
        # ⚠️ env 是私有属性，但这里是**同包裁判侧**访问 —— 与
        #    `select_case.py` 的 `_planning_factory` 走的是同一个口子。
        env = getattr(adapter, "_ManiSkillSimulatorAdapter__env", None)
        if env is None:
            raise ToolError(code="TRACK2_ORACLE_NO_ENV",
                            message="oracle planning needs a reset environment",
                            retryable=False)
        plan = build_oracle_plan(env, task, find_target(task))
        payload = serializable(oracle_executor_payload(plan))
        # 与 legacy 同款：先序列化，再挡一道 NaN/Infinity（JSON 允许，下游不接受）
        _strict_json(payload)
        return payload

    def _event_evidence(self) -> dict:
        """按本 case 真正启用的约束集采事件证据。

        `enabled` 的取法与 `candidate_runner.py:52` 一致：从
        `contract.constraints` 里挑出属于事件类（`EVENTS`）的那些。
        """
        try:
            from track2_judge.scoring.frozen_scorer import mesa_eval
            events = set(getattr(mesa_eval, "EVENTS", ()))
            enabled = [c for c in self._constraints()
                       if (c.get("type") if isinstance(c, dict) else None) in events]
            return self._require_adapter().get_event_evidence(bool(enabled))
        except Exception as exc:                                # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "coverage": "UNAVAILABLE"}

    def _constraints(self) -> list:
        contract = (self._bundle_cache or {}).get("contract") or {}
        return list(contract.get("constraints") or [])

    def close(self) -> None:
        self._closed = True
        if self._adapter is not None:
            try:
                self._run_blocking(self._adapter.close)
            except Exception:
                pass
            self._adapter = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run_blocking(self, fn, *args):
        """把一个阻塞调用丢到单线程 executor 上等结果。

        **不要在 event loop 里直接调 ManiSkill** —— 它会卡住整个 dora 节点。
        """
        future = self._executor.submit(fn, *args)
        return future.result()

    def _require_adapter(self):
        if self._adapter is None:
            raise ToolError(
                code="TRACK2_SCENE_NOT_READY",
                message="simulation scene is not initialized",
                retryable=True,
            )
        return self._adapter

    # ────────────────────────────────────────────── Query: track2.env.observe

    async def query(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        """读当前观测。**同步语义** —— 立刻返回。

        因为 `observe` 只是读状态（不推进物理），可以直接 await 一个 executor 任务。
        """
        arguments = dict(request.arguments or {})
        try:
            tool_id = _tool_id_for(context.endpoint_id, context.operation)
        except ToolError as error:
            return ToolResult(status="failed", outputs={}, error=error)
        try:
            schema_mod.validate_arguments(tool_id, arguments)
        except SchemaError as exc:
            return ToolResult(
                status="failed",
                outputs={},
                error=ToolError(code="TRACK2_SCHEMA_REJECTED", message=str(exc),
                                retryable=False),
            )
        try:
            payload = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._observe_blocking, arguments)
        except ToolError as error:
            return ToolResult(status="failed", outputs={}, error=error)
        except Exception as exc:
            return ToolResult(
                status="failed", outputs={},
                error=ToolError(code="TRACK2_OBSERVE_FAILED",
                                message=f"{type(exc).__name__}", retryable=True),
            )
        try:
            schema_mod.validate_output(tool_id, payload)
        except SchemaError as exc:
            return ToolResult(
                status="failed", outputs={},
                error=ToolError(code="TRACK2_OUTPUT_SCHEMA_VIOLATION", message=str(exc),
                                retryable=False),
            )
        # 官方把 `ToolResult.outputs` 原样放进 HTTP 的 `data.response`，
        # 所以这里必须是**选手看得见的那一层**（不是再包一层）。
        return ToolResult(status="succeeded", outputs=payload)

    def _observe_blocking(self, arguments: dict) -> dict:
        adapter = self._require_adapter()
        raw = adapter.observe()
        payload = _shape_observation(raw, include_rgb=arguments.get("include_rgb", True))
        self._publish_frame(payload)
        return payload

    def _publish_frame(self, payload: dict) -> None:
        """把刚采的这一帧推给 gateway（经 dora）。

        为什么挂在 `observe` 上而不是挂在 Action 上：`adapter.observe()` 是
        **唯一的**采样点 —— 选手的 `track2.env.observe` 与 gateway 的
        `/ws/images` 都从它取，所以两边看到的帧**一定一致**，
        不会出现「Query 说第 3 步、图上还是第 2 步」。

        ⚠️ 选手不调 `observe` 就不会有新帧 —— 这是**对的**：画面只在被观察时
        才需要产生，而 `joint_control` 的返回本来就不含图像。
        """
        if self._frame_sink is None:
            return
        camera = (payload.get("images") or {}).get("camera")
        try:
            self._frame_sink(camera, int(payload.get("control_steps", 0)))
            if camera is not None:
                self._frame_count += 1
        except Exception:                                      # noqa: BLE001
            # 推帧失败**不能**影响物理或 Query —— 选手的观测照常返回
            pass

    # ────────────────────────────────────────────── Action: joint_control / gripper_control

    async def start(self, request: ToolRequest, context: ToolContext,
                    events: ToolEventEmitter) -> ToolAccepted:
        """接纳一次 Action。**admission 不是完成** —— 物理在后台跑。"""
        arguments = dict(request.arguments or {})
        try:
            tool_id = _tool_id_for(context.endpoint_id, context.operation)
        except ToolError as error:
            raise _rejected(error.message) from None
        try:
            schema_mod.validate_arguments(tool_id, arguments)
        except SchemaError as exc:
            # 直接抛 ToolEndpointError 会被 handler 转成 failed
            raise _rejected(f"schema rejected: {exc}") from None

        key = context.execution_key
        # ★ 2026-10-19 幂等守卫：**同一个 (invocation_id, attempt_id) 只执行一次物理动作**。
        #
        #   背景：②-A2b 的「对齐旧 instance id 重投」（`node.py`）会让一条在飞请求
        #   再走一遍本函数。若 dora 对同一输入做「至少一次」重复投递（同一条 arrow
        #   ⇒ 同 invocation_id **且**同 attempt_id），物理动作就会被**执行两次**
        #   —— 比超时更糟。dora 实测表现为丢包（不是重复），故概率极低；
        #   但守卫本身无副作用。
        #
        #   ⚠️ 键取**完整** `ToolExecutionKey`（invocation_id + attempt_id），
        #      **不是**只有 invocation_id —— gateway 对同一 invocation 的**合法
        #      重试**会换 attempt_id，那种必须放行（否则动作静默不执行，更糟）。
        #      官方 handler 自己也是按这个二元组索引 execution 的
        #      （`forge_tool/handler.py:399` `_execution_key`）。
        dedup_key = key  # `ToolExecutionKey` 是 frozen dataclass，可哈希；含 attempt_id
        with self._execution_lock:
            if dedup_key in self._started_keys:
                return ToolAccepted(details={
                    "operation": context.operation,
                    "max_concurrency": self._config.max_concurrency,
                    "duplicate_invocation": True,
                })
            self._started_keys[dedup_key] = None
            while len(self._started_keys) > _STARTED_KEY_KEEP:
                self._started_keys.popitem(last=False)
            execution = _Execution(endpoint_id=context.endpoint_id,
                                   operation=context.operation, key=key,
                                   arguments=arguments)
            self._executions[key.invocation_id] = execution

        # 关键：官方 handler 只有在收到 **terminal event** 时才把 phase 推到终态。
        # 官方 `_TERMINAL_EVENT_PHASES` 的映射：
        #   executor_completed → completed / executor_failed → failed / cancelled → cancelled
        loop = asyncio.get_running_loop()
        execution.loop = loop
        execution.emitter = events
        loop.run_in_executor(self._executor, self._run_action, execution)
        return ToolAccepted(details={
            "operation": context.operation,
            "max_concurrency": self._config.max_concurrency,
        })

    def _run_action(self, execution: _Execution) -> None:
        """Action 的执行体。**必须在终结前把 execution_status 说清楚。**"""
        record = execution
        with record.lock:
            record.phase = "running"
            record.started_at_s = time.monotonic()

        try:
            adapter = self._require_adapter()
            if record.operation == "trajectory_control":
                raw = self._run_chunk_blocking(record, adapter)
            elif record.operation == "gripper_control":
                raw = adapter.dispatch_public(
                    "piper_gripper_control",
                    {"gripper_opening_m": float(record.arguments["gripper_opening_m"])})
            else:
                raise RuntimeError(f"no executor bound for {record.operation}")

            # ★ 2026-10-19 字段语义统一 + 键名修正（见
            #   `plan/Track2_字段语义统一_修复方案.md`）。
            #
            #   1. `control_steps` 一律是 **episode 累计**（与 `observe` 同义）；
            #      本次真走了几步在 `executed_steps` 里。
            #   2. **`executed_steps` 只在该 ToolSpec 声明了它时才带** ——
            #      `joint_trajectory` 有、`gripper_control` 没有，而两个 spec
            #      都是 `additionalProperties: false`（多一个键 = 校验失败）。
            #      修前这里无条件带上它，导致 **gripper_control 每次调用都被拒**。
            #   3. adapter 的 `observe()` 用的键名是 `simulation_time_s`，
            #      对外协议名是 `sim_time_s` —— 修前这里只读后者，
            #      于是 gripper 返回的 `sim_time_s` **恒为 0**。两个都认。
            tool_id = _tool_id_for(record.endpoint_id, record.operation)
            steps = int(raw.get("control_steps", 0))
            sim_time = float(raw.get("sim_time_s",
                                     raw.get("simulation_time_s", 0.0)))
            payload = {"control_steps": steps, "sim_time_s": sim_time}
            if "executed_steps" in _declared_output_keys(tool_id):
                payload["executed_steps"] = int(raw.get("executed_steps", 0))
            schema_mod.validate_output(tool_id, payload)
            with record.lock:
                record.result = ToolResult(status="succeeded", outputs=payload)
                record.phase = _PHASE_COMPLETED
                record.execution_status = EXEC_STOPPED_CONFIRMED
                record.finished_at_s = time.monotonic()
            self._emit_terminal(record, "executor_completed")
        except Exception as exc:
            with record.lock:
                # 带上原因（有界截断）—— 只记类型名会让调试变成猜谜
                record.error_text = f"{type(exc).__name__}: {str(exc)[:200]}"
                record.result = ToolResult(
                    status="failed", outputs={},
                    error=ToolError(code=self._error_code_for(exc),
                                    message=record.error_text, retryable=False))
                record.phase = _PHASE_FAILED
                record.execution_status = EXEC_STOPPED_CONFIRMED
                record.finished_at_s = time.monotonic()
            self._emit_terminal(record, "executor_failed")

    @staticmethod
    def _error_code_for(exc: Exception) -> str:
        """把执行体异常映射成 gateway 错误码。

        ⚠️ **F1 修法**（2026-10-18）：`control budget exhausted` 以前和别的异常
        一起走 `TRACK2_ACTION_FAILED` → 客户端 `ToolCallError` → `node.py` 的
        `INFRASTRUCTURE_ERRORS` → `infrastructure_timeout` → **Gate BLOCKED**。
        但「预算正常用满」是**可评分**的 `budget_timeout`，不是链路故障。
        所以给它一个**专用错误码**，客户端据此单独映射。

        ★ 2026-10-19 **同一模式再修一类：选手参数非法**。

        `ValueError: target slew exceeded` / `outside Piper limits` /
        `numeric values required` / `dimension or finite check failed` —— 这些是
        **选手下发的动作不合法**（`dispatch.py` 的 `assert_within_limits` /
        `assert_within_slew` 抛的），**不是链路故障**。以前它们和链路故障一起走
        `TRACK2_ACTION_FAILED` → `ToolCallError` → `infrastructure_timeout` →
        整 case BLOCKED ⇒ **把「选手写错了」误报成「基础设施坏了」**，
        既丢分又误导选手。

        现在复用**既有的** `TRACK2_ACTION_REJECTED` 码（同一模块的 `_rejected()`
        早就用它表示「入参被拒」，只是那只覆盖 schema 层）—— 客户端据此归
        **可评分**的 `skill_error`（照常按物理终态判分）。
        """
        if isinstance(exc, RuntimeError) and "control budget exhausted" in str(exc):
            return "TRACK2_CONTROL_BUDGET_EXHAUSTED"
        # ★ 选手参数非法（校验类 ValueError）—— 可评分，不是链路故障。
        if isinstance(exc, ValueError) and _is_caller_argument_error(exc):
            return "TRACK2_ACTION_REJECTED"
        return "TRACK2_ACTION_FAILED"

    def _run_chunk_blocking(self, record: "_Execution", adapter) -> dict:
        """执行一段 Action Chunk（`targets`，8–25 步）。

        **两阶段语义**（`plan/Track2_强制ActionChunk_契约设计.md` §4.1）：

        1. **阶段 1：全链预校验** —— 限位 / 步间限幅 / 预算，**一步物理都不推进**。
           整条不过就整条拒绝（原子性）。预算预校验是本改造**多出来的必需件**：
           chunk 是唯一的移动途径，撞预算必须在**调用前**判掉，否则必然半途
           停在 BLOCKED（F1）。
        2. **阶段 2：逐步执行** —— 每步检查 `cancel_requested`，可打断。

        ⚠️ chunk 内部**不重拍图**（帧仍挂在 `observe` 上）—— 它是一段盲走。
        """
        targets = record.arguments["targets"]
        # ── 阶段 1
        previous = adapter.current_command_target()
        for item in targets:
            previous = adapter.assert_within_slew(
                previous, item["joint_position"], item["gripper_opening_m"])
            previous = [*previous[0].tolist(), float(previous[1])]
        adapter.assert_budget_allows(len(targets))
        # ── 阶段 2
        executed = 0
        for item in targets:
            if record.cancel_requested:
                break
            adapter.step({"joint_position": list(item["joint_position"]),
                          "gripper_opening_m": float(item["gripper_opening_m"])})
            executed += 1
        return {"control_steps": adapter.control_steps,
                "sim_time_s": adapter.control_steps * adapter.control_timestep,
                "executed_steps": executed}

    def _emit_terminal(self, record: "_Execution", event_type: str) -> None:
        """把终态事件推给官方 handler —— **这是 phase 推进的唯一途径**。

        在 executor 线程里调用，所以要 `run_coroutine_threadsafe` 回 event loop。
        事件发不出去也不能让物理线程挂住（有界等待）。
        """
        emitter = getattr(record, "emitter", None)
        loop = getattr(record, "loop", None)
        if emitter is None or loop is None or loop.is_closed():
            return
        try:
            from forge_tool import ToolEvent
            future = asyncio.run_coroutine_threadsafe(
                emitter.emit(ToolEvent(type=event_type, data={})), loop)
            future.result(timeout=self._config.stop_confirm_timeout_s)
        except Exception:
            # handler 拿不到事件时会按它自己的超时/unknown 语义处理 —— 如实即可
            pass

    async def status(self, key: ToolExecutionKey) -> ToolExecutionStatus:
        record = self._executions.get(key.invocation_id)
        if record is None:
            # ★ ②-A2（2026-10-18）：**这是「我从未收到过这条 invocation」的正面证据。**
            #
            #   只有三种可能走到这里：
            #     1. 客户端问了一个不存在的 invocation_id（几乎不可能）；
            #     2. 这条 invoke 请求在 gateway → provider 的 dora 通道上**被丢了**
            #        —— 正是丢包自锁的那个成因；
            #     3. 我们刚换了 instance id，这条属于**旧** instance。
            #
            #   第 3 种其实到不了这里：换 id 后 handler 的 `validate_route` 会先用
            #   `FORGE_PROTOCOL_ROUTE_MISMATCH` 把旧 instance 的请求拒掉，不会进
            #   到 `status()`。所以到这里基本就是第 2 种。
            #
            #   交给 `node.py` 的钩子去**节流地换 instance id** —— 换 id 会触发
            #   gateway 的 `mark_instance_ambiguous`，把被占死的并发围栏释放掉，
            #   后续动作立刻恢复（A1 只能把损失限制在一个 episode，A2 能让它自愈）。
            #
            #   ⚠️ **必须节流**：一个陌生 invocation 会被反复轮询（客户端 5 s 超时
            #   内每 50 ms 一次），不节流会引发注册风暴。
            hook = getattr(self, "on_unknown_invocation", None)
            if hook is not None:
                try:
                    hook(key.invocation_id)
                except Exception:                               # noqa: BLE001
                    pass
            # 官方模型要求：`unknown` / `failed` 相位**必须**带 error
            return ToolExecutionStatus(
                phase="unknown",
                error=ToolError(code="TRACK2_UNKNOWN_INVOCATION",
                                message="unknown invocation", retryable=False),
                details={"note": "unknown invocation"})
        with record.lock:
            details = {
                "execution_status": record.execution_status,
                "cancel_effective": record.cancel_effective,
                "operation": record.operation,
            }
            phase = record.phase
            # Official SPI requires error on failed/unknown terminal status.
            # Preserve the original failure (including required camera failure)
            # instead of raising a second exception while polling it.
            error = record.result.error if record.result is not None else None
            if phase in (_PHASE_FAILED, _PHASE_UNKNOWN) and error is None:
                error = ToolError(code="TRACK2_ACTION_FAILED",
                                  message="execution failed", retryable=False)
        return ToolExecutionStatus(phase=phase, error=error, details=details)

    async def result(self, key: ToolExecutionKey) -> ToolResultResponse:
        record = self._executions.get(key.invocation_id)
        if record is None:
            return ToolResultResponse(status="not_found")
        with record.lock:
            if record.result is None:
                return ToolResultResponse(status="pending")
            result = record.result
        return ToolResultResponse(status="available", result=result)

    async def cancel(self, key: ToolExecutionKey,
                     reason: str | None = None) -> ToolControlResponse:
        """请求取消（Q13）。

        **`accepted` 只说明请求被受理，不证明物理已停。**
        我们把「真的停了吗」放在 `details.execution_status` 里如实报。
        """
        record = self._executions.get(key.invocation_id)
        if record is None:
            return ToolControlResponse(
                command="cancel", status="rejected",
                error=ToolError(code="TRACK2_UNKNOWN_INVOCATION",
                                message="unknown invocation", retryable=False))
        with record.lock:
            if record.phase in (_PHASE_COMPLETED, _PHASE_FAILED,
                                _PHASE_CANCELLED, _PHASE_UNKNOWN):
                # 已经终结：cancel 无害但无效。**如实说**。
                return ToolControlResponse(
                    command="cancel", status="terminal",
                    details={"execution_status": record.execution_status,
                             "cancel_effective": False,
                             "note": "invocation already terminal; cancel had no effect"})
            record.cancel_requested = True

        # 有界等 backend —— 但物理串行在单线程 executor 上，
        # 我们能做的只有「等这一轮跑完」。等不到就是 unknown。
        stopped = self._wait_idle()
        with record.lock:
            if record.phase in (_PHASE_COMPLETED, _PHASE_FAILED):
                record.cancel_effective = False
            elif stopped:
                record.phase = _PHASE_CANCELLED
                record.result = ToolResult(
                    status="cancelled", outputs={},
                    error=ToolError(code="TRACK2_CANCELLED",
                                    message=reason or "cancelled", retryable=False))
                record.execution_status = EXEC_STOPPED_CONFIRMED
                record.cancel_effective = True
                record.finished_at_s = time.monotonic()
            else:
                record.execution_status = EXEC_UNKNOWN
                record.cancel_effective = False
            details = {"execution_status": record.execution_status,
                       "cancel_effective": record.cancel_effective}
            if record.execution_status == EXEC_UNKNOWN:
                details["note"] = ("cancel accepted but physical stop unconfirmed")
        return ToolControlResponse(command="cancel", status="accepted", details=details)

    def _wait_idle(self) -> bool:
        """等单线程 executor 排空。返回「是否**确认**已空闲」。

        executor 只有 1 个 worker：投一个空任务，能执行完就说明前一个跑完了。
        超时 → `TimeoutError` → 返回 False（「拿不到正面证据」）。
        """
        deadline = time.monotonic() + self._config.stop_confirm_timeout_s
        try:
            self._executor.submit(lambda: None).result(
                timeout=max(0.01, deadline - time.monotonic()))
            return True
        except (TimeoutError, Exception):       # noqa: BLE001 —— 等不到就是等不到
            return False


# ───────────────────────────────────────────────────────── 小工具


# `(endpoint_id, operation)` → Tool ID。
#
# **不能假定 provider 侧的 `context.tool_id` 就是 Tool ID** ——
# 官方 wire 里那个字段是 endpoint 粒度的（如 `track2.env`），
# 而我们的 schema 是按 Tool ID（如 `track2.env.observe`）声明的。
# 所以在边界上显式映射，两边各自用自己惯用的标识。
_TOOL_IDS = {
    ("track2.env", "observe"): "track2.env.observe",
    ("track2.robot", "trajectory_control"): "track2.robot.joint_trajectory",
    ("track2.robot", "gripper_control"): "track2.robot.gripper_control",
}


def _tool_id_for(endpoint_id: str, operation: str) -> str:
    """把 provider 侧的 (endpoint, operation) 解析成我们的 Tool ID。"""
    tool_id = _TOOL_IDS.get((endpoint_id, operation))
    if tool_id is None:
        raise ToolError(
            code="TRACK2_UNKNOWN_OPERATION",
            message=f"no Tool ID bound to {endpoint_id}/{operation}",
            retryable=False,
        )
    return tool_id


def _declared_output_keys(tool_id: str) -> frozenset:
    """该 ToolSpec 的 `output_schema` 声明了哪些键。

    用途见 `_run_action`：**只有声明过的键才能出现在 payload 里** ——
    两个 Action spec 都是 `additionalProperties: false`，多带一个键会被
    `validate_output` 直接拒掉（2026-10-19 前 `gripper_control` 就栽在这上面）。

    取不到（spec 不存在/形状意外）时返回**空集**，即「一个可选键都不带」——
    宁可少带（schema 只要求必填项），不要多带而被拒。
    """
    try:
        from track2_judge.toolapi import schema as _schema
        spec = _schema.SPECS[tool_id]
        return frozenset((spec.get("output_schema") or {}).get("properties") or {})
    except Exception:                                           # noqa: BLE001
        return frozenset()


def _shape_observation(raw: dict, *, include_rgb: bool) -> dict:
    """把 adapter 的原始观测收窄成 ToolSpec 声明的形状。

    与第 2 步 `toolapi/dispatch.py:GATEWAYExecutor._as_observation` **同一套逻辑** ——
    它在那边是被测试覆盖过的，这里保持一致，将来收敛到一处。
    """
    observation_id = f"obs_{uuid.uuid4().hex[:16]}"
    out = {
        "observation_id": observation_id,
        "captured_at_s": time.monotonic(),
        "sim_time_s": float(raw.get("simulation_time_s", 0.0)),
        "control_steps": int(raw.get("control_steps", 0)),
        "robot": raw.get("robot", {}),
    }
    if raw.get("task_inputs") is not None:
        out["task_inputs"] = raw["task_inputs"]
    if include_rgb:
        images = {}
        # 内部原始观测键是 `first_frame`（provider 的缓存名），
        # 对选手暴露的 ToolSpec 键是 `camera`（每控制步更新）。
        if raw.get("first_frame") is not None:
            images["camera"] = raw["first_frame"]
        if raw.get("camera_status") is not None:
            images["camera_status"] = str(raw["camera_status"])
        if raw.get("wrist_frame") is not None:
            images["wrist_camera"] = raw["wrist_frame"]
        if raw.get("wrist_camera_status") is not None:
            images["wrist_camera_status"] = str(raw["wrist_camera_status"])
        if images:
            out["images"] = images
    return out


def _rejected(message: str) -> Exception:
    """按官方约定构造「入参被拒」的异常（handler 会转成 failed ToolResult）。"""
    from forge_tool import ToolEndpointError
    return ToolEndpointError(
        ToolError(code="TRACK2_ACTION_REJECTED", message=message, retryable=False))


#: 选手参数非法的**已知文案**（来自 `track2_judge/toolapi/dispatch.py` 的校验）。
#: 只认这些 —— **不要**用「所有 ValueError」兜，否则会把内部 bug 也误判成
#: 「选手的错」而放过（那会让真故障变成可评分，方向反了）。
_CALLER_ARG_ERRORS = (
    "target slew exceeded",
    "outside Piper limits",
    "numeric values required",
    "dimension or finite check failed",
)


def _is_caller_argument_error(exc: BaseException) -> bool:
    """这个异常是不是「选手下发的动作参数不合法」（可评分，非链路故障）？

    与 `_rejected()` 是同一类语义（入参被拒）：`_rejected` 覆盖 **schema 层**
    （handled 得更早），这里覆盖 **物理校验层**（`dispatch.py` 的限位/限幅）。

    ⚠️ 白名单而非黑名单：宁可漏判（退回 `TRACK2_ACTION_FAILED` → 阻断，保守），
    也不要把未知异常当成「选手的错」而放行。
    """
    text = str(exc)
    return any(marker in text for marker in _CALLER_ARG_ERRORS)
