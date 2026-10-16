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


class Track2SimulationEndpoints(QueryToolEndpoint, ActionToolEndpoint):
    """两个 endpoint 的实现。用**同一个实例**，因为都碰同一个仿真环境。

    `forge_tool` 的 `ToolEndpointHandler` 接受 `operations: Mapping[str, object]`，
    所以一个类实例可以同时扮演 `observe` / `joint_control` / `gripper_control` 三个操作。
    """

    def __init__(self, adapter_factory, config):
        self._config = config
        self._adapter_factory = adapter_factory
        self._adapter = None                    # 只在 executor 线程里初始化
        self._executions: dict[str, _Execution] = {}
        self._execution_lock = threading.Lock()
        # Q9：物理串行化。max_workers=1 保证任何时候只有一个东西在碰 env。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="track2-physics")
        self._closed = False

    # ────────────────────────────────────────────── 生命周期

    def open(self) -> None:
        """在 executor 线程里建场景。由节点的启动流程调用。"""
        self._run_blocking(self._open_blocking)

    def _open_blocking(self) -> None:
        self._adapter = self._adapter_factory()

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
        return _shape_observation(raw, include_rgb=arguments.get("include_rgb", True))

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
        execution = _Execution(endpoint_id=context.endpoint_id,
                               operation=context.operation, key=key,
                               arguments=arguments)
        with self._execution_lock:
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
            if record.operation == "joint_control":
                raw = adapter.step({
                    "joint_position": list(record.arguments["joint_position"]),
                    "gripper_opening_m": float(record.arguments["gripper_opening_m"]),
                })
            elif record.operation == "gripper_control":
                raw = adapter.dispatch_public(
                    "piper_gripper_control",
                    {"gripper_opening_m": float(record.arguments["gripper_opening_m"])})
            else:
                raise RuntimeError(f"no executor bound for {record.operation}")

            payload = {"control_steps": int(raw.get("control_steps", 0)),
                       "sim_time_s": float(raw.get("simulation_time_s", 0.0))}
            schema_mod.validate_output(
                _tool_id_for(record.endpoint_id, record.operation), payload)
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
                    error=ToolError(code="TRACK2_ACTION_FAILED",
                                    message=record.error_text, retryable=False))
                record.phase = _PHASE_FAILED
                record.execution_status = EXEC_STOPPED_CONFIRMED
                record.finished_at_s = time.monotonic()
            self._emit_terminal(record, "executor_failed")

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
        return ToolExecutionStatus(phase=phase, details=details)

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
    ("track2.robot", "joint_control"): "track2.robot.joint_control",
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
        if raw.get("first_frame") is not None:
            images["first_frame"] = raw["first_frame"]
        if raw.get("camera_status") is not None:
            images["camera_status"] = str(raw["camera_status"])
        if images:
            out["images"] = images
    return out


def _rejected(message: str) -> Exception:
    """按官方约定构造「入参被拒」的异常（handler 会转成 failed ToolResult）。"""
    from forge_tool import ToolEndpointError
    return ToolEndpointError(
        ToolError(code="TRACK2_ACTION_REJECTED", message=message, retryable=False))
