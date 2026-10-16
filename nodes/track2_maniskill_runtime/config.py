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
    # 相机是否必需。模式 B（Q4）下目标是原始图文，但当前案例的正式入口
    # `camera_status` 仍是 `NOT CONFIGURED`（见 §5.3 的改动清单），
    # 所以这里默认 False —— 第 5 步启用相机后改 true。
    camera_required: bool = False

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
    "camera_required", "endpoint_instance_id", "lease_renew_interval_s",
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

    for required in ("case_id", "casebank"):
        if required not in data:
            raise ConfigError(f"missing required config key: {required}")

    casebank = Path(str(data["casebank"])).expanduser()
    if not casebank.is_absolute():
        # 相对路径按**配置文件所在目录**解析，与 dataflow 的惯例一致
        casebank = (path.parent / casebank).resolve()

    kwargs = {k: v for k, v in data.items() if k in _KEYS}
    kwargs["casebank"] = casebank
    if not kwargs.get("endpoint_instance_id"):
        # 每个进程一个实例 ID —— gateway 靠它做 provider 路由
        kwargs["endpoint_instance_id"] = f"track2-runtime-{os.getpid()}"
    return RuntimeConfig(**kwargs)
