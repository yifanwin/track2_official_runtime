"""`track2-maniskill-runtime` 的配置。

配置形状参照 `operator-simulator-mujoco` 的 `config.py`（官方仿真节点的做法）：
一个 YAML，节点启动时 `--config` 读入，**不接受命令行覆盖 case**。

⚠️ **`case_id` 与 `seed` 是裁判私有输入**：它们本身不泄漏答案，但节点**不得**
把它们回显到任何对外输出（Tool 响应、`sim_status`、日志）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

__all__ = ["RuntimeConfig", "load_config", "ConfigError", "rotate_instance_id"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    """节点运行参数。"""

    # ── 场景（裁判私有）
    case_id: str
    seed: int = 0
    casebank: Path = field(default_factory=lambda: Path(""))

    # ── episode 预算（与 runner 的语义一致）
    max_steps: int = 400
    max_sim_time_s: float = 20.0

    # ── Action 治理（Q9 / Q12 / Q14）
    # Q9：关节控制天然串行
    max_concurrency: int = 1
    # Q12：每个 Action 必须有执行超时
    # Q14：秒数待 73 case 实测 —— 实测前的保守占位值
    action_timeout_s: float = 5.0
    action_timeout_source: str = "MEASURED_73_CASES_P99_PLUS_MARGIN"
    # Q13：cancel 之后最多等这么久「backend 确认真实停止」
    stop_confirm_timeout_s: float = 5.0

    # ── 观测
    # 给选手的自然语言指令（模式 B 的公开输入之一）。
    # 它不含任何答案 —— 「移动哪个物体」本来就在题面里。
    text_instruction: str = "Move the target object to the position shown in the reference image."

    # 相机是否必需。**第 5 步起默认 True** —— 模式 B（Q4）下原始图文是
    # 选手唯一的感知来源，没有相机这条链路就不成立。
    # 保留开关是为了诊断场景（`camera_required=false` 时不建相机，跑得更快）。
    #
    # ⚠️ 2026-10-17（G3）：这个字段以前**没有生效路径** —— 评测器的
    #    `--no-camera` 把开关传到 `ResidentRunner` 就断了。现在
    #    `PersistentFlow.start()` 会把它渲染进本文件，所以它**真的管用**。
    camera_required: bool = True

    # 渲染用的**物理 GPU 卡号**（八卡并行，2026-10-19）。
    #
    # 为什么需要它：`env_factory` 写死 `render_backend='sapien_cuda:0'`，
    # 而 `cuda:0` 指的是「`CUDA_VISIBLE_DEVICES` 里的第 0 个」。串行时
    # 无所谓；**8 路并行时若不设，全部落回物理卡 0** ⇒ 物理步变慢 ⇒
    # action 撞 5 s 超时 ⇒ 围栏槽位不释放 ⇒ `concurrency 1 exhausted`。
    #
    # ⚠️ 走**配置**而不是环境变量：与 `camera_required` / `judge_control_port`
    # 同因 —— `--detach` 下节点由长命 daemon spawn，daemon 环境里没有
    # `TRACK2*`。裁判侧按 worker 渲染本项。
    #
    # 语义：节点用它设 `CUDA_VISIBLE_DEVICES`，让 `sapien_cuda:0` 落在该卡。
    # 空 = 不改变原行为；**`0` 是合法的显式卡号（物理卡 0）**。
    # ⚠️ 别把 0 当 falsy 处理 —— YAML 里的 `render_gpu: 0` 是 int 0，
    #    写成 `... or ""` 会让 w0 静默不生效（见 Node._build_adapter）。
    render_gpu: str = "0"

    # ── 裁判侧 Oracle 的 GT 规划（G2）
    #
    # 默认 **False**：普通评测**不规划**，选手的公开输入里不会出现 `oracle_plan`。
    # 只有 `release_entry.py --oracle-plan auto`（或显式渲染本项）时才开。
    #
    # 开了之后，`_reset_blocking` 会在**它自己的 env** 上跑一次 GT 规划
    # （规划只读 env，不写），把 payload 经公开输入的 `oracle_plan` 字段下发给
    # 沙箱里的 oracle skill。选手包拿不到它（白名单逐次构造）。
    oracle_planning: bool = False

    # ── provider 注册
    #
    # ⚠️ **这个值会被轮换（2026-10-18，丢包自锁修复 ②-A1）**。
    #
    # 它不再是「一个进程一个值」—— provider 会在**每次 `reset`（换 episode）**
    # 换一个新的 instance id，并重新 `endpoint.register`。原因：
    #
    # gateway 的并发围栏（`occupies_concurrency`）**只有一条释放路径** ——
    # `mark_instance_ambiguous`，触发条件是「注册的 instance 与 invocation 钉住的
    # instance 不一致」（`tool_directory.py` 的 `register_with_change`：换 id 会把
    # 旧的注册放进 `removed`，`_finish_unclaimed_for_removed_locked` 随即调
    # `mark_instance_ambiguous` 释放围栏）。
    #
    # 而一次丢包会让 `max_concurrency = 1` 的槽位**永久占死**（详见
    # `plan/Track2_丢包自锁修复方案.md`）—— 因为我们的 provider 整轮常驻、
    # 每 5 s 续租，那个「不一致」永远不成立。换 id 就是让 gateway 眼里的
    # provider「死一次」，把围栏要回来；物理侧完全连续，场景不重建。
    #
    # 实现上**保持 `frozen=True`**：轮换走 `rotate_instance_id()`（`dataclasses.replace`），
    # 计数器放在 `node.py` 里，不改本类的不可变语义。
    endpoint_instance_id: str = ""
    lease_renew_interval_s: float = 5.0

    # ── Judge-only 私有控制口（B1）
    #
    # **0 = 关闭**（CPU 回归不开监听）。>0 时 runtime 节点在 loopback 上开一个
    # 只给裁判用的 HTTP 口（换 episode / 取 judge-only 快照）。
    #
    # ⚠️ 为什么是**配置项**而不是环境变量：`dora start --detach` 之后节点由
    #    **长命的 dora daemon** spawn，daemon 的环境里没有评测时设的那些变量
    #    （实测 `/proc/<daemon>/environ` 里 0 个 `TRACK2*`）——
    #    走环境变量会**静默地**不开控制口。`runtime.yaml` 由裁判逐次渲染，
    #    一定到得了节点。
    #
    # 它**不是 Tool**：不注册 ToolSpec、不出现在 `GET /tools`、不进选手 context。
    judge_control_port: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ConfigError("case_id must be a non-empty string")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ConfigError("seed must be a non-negative integer")
        if isinstance(self.judge_control_port, bool) \
                or not isinstance(self.judge_control_port, int) \
                or self.judge_control_port < 0:
            raise ConfigError("judge_control_port must be a non-negative integer")
        for name in ("max_steps", "max_concurrency"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"{name} must be a positive integer")
        for name in ("max_sim_time_s", "action_timeout_s", "stop_confirm_timeout_s"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ConfigError(f"{name} must be positive")
        if not str(self.casebank):
            raise ConfigError("casebank path is required")


_KEYS = {
    "case_id", "seed", "casebank", "max_steps", "max_sim_time_s",
    "max_concurrency", "action_timeout_s", "stop_confirm_timeout_s",
    "camera_required", "text_instruction", "endpoint_instance_id",
    "lease_renew_interval_s", "judge_control_port", "oracle_planning",
    "render_gpu",
}


def load_config(config_path: str | Path | None = None) -> RuntimeConfig:
    """读 YAML 配置。路径可由 `--config` 或 `TRACK2_RUNTIME_CONFIG` 给。"""
    raw_path = config_path or os.environ.get("TRACK2_RUNTIME_CONFIG")
    if not raw_path:
        raise ConfigError("no config path given (--config or TRACK2_RUNTIME_CONFIG)")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config must be a YAML mapping")

    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")

    # ── case_id / seed：**环境变量优先**
    #
    # 它们是**每个 case 的评测输入**，不该固化进 Bundle（否则换个 case 就得重打包）。
    # 评测机通过 `skill.yaml` 的 `profiles.<p>.environment` 或 start.sh 注入：
    #   TRACK2_CASE_ID / TRACK2_SEED
    # YAML 里只留一个**调试用**的默认值。
    if os.environ.get("TRACK2_CASE_ID", "").strip():
        data["case_id"] = os.environ["TRACK2_CASE_ID"].strip()
    if os.environ.get("TRACK2_SEED", "").strip():
        try:
            data["seed"] = int(os.environ["TRACK2_SEED"])
        except ValueError:
            raise ConfigError("TRACK2_SEED must be an integer") from None

    for required in ("case_id", "casebank"):
        if required not in data:
            raise ConfigError(f"missing required config key: {required}")

    casebank = Path(str(data["casebank"])).expanduser()
    if not casebank.is_absolute():
        # ⚠️ 相对路径**不能**按配置文件所在目录解析 —— PAOS 的 installer 会把
        # profile 文件软链到 `.../environments/<skill>/<profile>/<digest>/launch/profiles/...`
        # 下，从那里往上推是错的（实测过：算出 `.../sim/<digest>/track2_final/...`）。
        #
        # 改为按 `TRACK2_PHYSICS_ROOT` 解析 —— 它是**机器相关的绝对路径**，
        # 由 `skill.yaml` 的 `profiles.<name>.environment` 注入，
        # 与 Bundle 被搬到哪里无关。没给就退回配置文件目录（手工跑的场景）。
        physics_root = os.environ.get("TRACK2_PHYSICS_ROOT")
        base = Path(physics_root).expanduser() if physics_root else path.parent
        casebank = (base / casebank).resolve()

    kwargs = {k: v for k, v in data.items() if k in _KEYS}
    kwargs["casebank"] = casebank
    if not kwargs.get("endpoint_instance_id"):
        # 每个进程一个实例 ID —— gateway 靠它做 provider 路由。
        # `.0` 是 epoch 后缀，换 episode 时由 `rotate_instance_id()` 递增。
        kwargs["endpoint_instance_id"] = f"{_INSTANCE_BASE}-{os.getpid()}.0"
    return RuntimeConfig(**kwargs)


#: instance id 的基名。轮换时是 `<base>-<pid>.<epoch>`。
_INSTANCE_BASE = "track2-runtime"


def _instance_base(instance_id: str) -> str:
    """从当前 instance id 里取基名（去掉尾部的 `.<digits>` 后缀）。

    用**正则**而不是 `split('.')` —— 基名里可能合法地含点
    （id 规则只要求非空字符串，见 `forge_tool/handler.py` 的 `_validate_identifier`）。
    """
    import re
    return re.sub(r"\.\d+$", "", instance_id) or f"{_INSTANCE_BASE}-{os.getpid()}"


def rotate_instance_id(config: RuntimeConfig, epoch: int) -> RuntimeConfig:
    """换一个新的 `endpoint_instance_id`（丢包自锁修复 ②-A1）。

    ## 为什么需要它

    gateway 的并发围栏只有 `mark_instance_ambiguous` 一条释放路径，而它的触发
    条件是「注册的 instance ≠ invocation 钉住的 instance」。我们整轮常驻并
    **持续续租**，那个不一致永远不成立 → 一次丢包就把 `max_concurrency = 1`
    的槽位占到 run 结束（实测：全量 73 例里 17 例受影响）。换 id 是让 gateway
    眼里的 provider「死一次」。

    ## 为什么保持 `frozen=True`

    `RuntimeConfig` 是不可变的（多处共享同一实例），直接改字段会污染别人。
    轮换用 `dataclasses.replace` 造**新**实例，`node.py` 持有它并同步给
    handler 与所有 register 路径。

    ⚠️ 旧 id 的注册会在 gateway 侧进 `removed` → 触发
    `mark_instance_ambiguous` → **该实例所有未终态 invocation 被标 ambiguous**。
    这正是我们要的（围栏释放），但要确保轮换发生在**没有在飞的动作**的时刻
    —— `node.py` 只在 `reset` 时轮换，而 reset 的语义本来就是「上一轮结束、
    下一轮开始」（边界决策：循环归 Judge，逐 episode 下发）。
    """
    import dataclasses

    return dataclasses.replace(
        config,
        endpoint_instance_id=f"{_instance_base(config.endpoint_instance_id)}.{int(epoch)}")
