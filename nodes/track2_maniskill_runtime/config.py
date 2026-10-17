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

__all__ = ["RuntimeConfig", "load_config", "ConfigError"]


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
    max_steps: int = 2400
    max_sim_time_s: float = 120.0

    # ── Action 治理（Q9 / Q12 / Q14）
    # Q9：关节控制天然串行
    max_concurrency: int = 1
    # Q12：每个 Action 必须有执行超时
    # Q14：秒数待 73 case 实测 —— 实测前的保守占位值
    action_timeout_s: float = 30.0
    action_timeout_source: str = "PROVISIONAL_PENDING_MEASUREMENT"
    # Q13：cancel 之后最多等这么久「backend 确认真实停止」
    stop_confirm_timeout_s: float = 5.0

    # ── 观测
    # 给选手的自然语言指令（模式 B 的公开输入之一）。
    # 它不含任何答案 —— 「移动哪个物体」本来就在题面里。
    text_instruction: str = "Move the target object to the position shown in the reference image."

    # 相机是否必需。**第 5 步起默认 True** —— 模式 B（Q4）下原始图文是
    # 选手唯一的感知来源，没有相机这条链路就不成立。
    # 保留开关是为了诊断场景（`camera_required=false` 时不建相机，跑得更快）。
    camera_required: bool = True

    # ── provider 注册
    endpoint_instance_id: str = ""
    lease_renew_interval_s: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ConfigError("case_id must be a non-empty string")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ConfigError("seed must be a non-negative integer")
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
    "lease_renew_interval_s",
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
        # 每个进程一个实例 ID —— gateway 靠它做 provider 路由
        kwargs["endpoint_instance_id"] = f"track2-runtime-{os.getpid()}"
    return RuntimeConfig(**kwargs)
