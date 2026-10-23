---
name: track2-official-runtime
description: Track 2 官方 ManiSkill Tool runtime —— 读取环境、控制机器人。
metadata: {"PhyAgentOS":{"always":true,"requires":{"runtime":["track2-official-runtime"]}}}
---


# Track2 Official Runtime

版本 0.3.5。这是 Track2 唯一官方 Tool 面，选手不得修改。

| Tool ID | 类型 | 用途 |
|---|---|---|
| `track2.env.observe` | query | 机器人状态、公开任务输入、固定与腕部 RGB |
| `track2.robot.joint_trajectory` | action | 8–25 步关节轨迹 |
| `track2.robot.gripper_control` | action | 保持臂关节目标，只改变夹爪 |

调用前读取实时 schema、readiness 和机器人坐标系。ToolSpec 以
`track2_judge/toolapi/schema.py` 为准，不臆造字段、限位或默认值。

## 观测

`robot.joint_position` 为 8 维：6 个臂关节和 2 个夹爪手指。
夹爪开度为 `joint_position[6] - joint_position[7]`。
本体还提供关节速度、限位、TCP 位置及四元数。

任务输入包含文本、参考图、谓词和约束的语义字段。
固定 RGB 为 `images.camera`，腕部 RGB 为 `images.wrist_camera`，
各带状态字段，默认 256×256 PNG，包含尺寸、base64 和 sha256。
每次 observe 采集当前物理状态，不推进控制步；相机失败时报错且不复用旧帧。
`include_rgb=False` 同时省略两路图像。
不提供目标位姿、区域坐标、容差、相机标定、深度或分割。

## 动作

轨迹入参为 `targets` 列表，每项具有 6 维 `joint_position`（弧度）和
`gripper_opening_m`（米，0–0.1）。每块 8–25 项，整块先校验再执行。
每步臂关节变化不超过 0.05 rad，夹爪开度变化不超过 0.006 m。
首步相对上一次目标校验，超限时整块拒绝。

动作并发数为 1，超时 5 秒。Action 被接纳不等于完成；应等待终态并读取结果。
取消请求被接纳也不证明物理已停。停止状态未知时先重新观察，不盲目重复动作。
`control_steps` 和 `sim_time_s` 是累计值；`executed_steps` 是本块实际执行步数。

## 预算和边界

每 episode 计算预算 300 秒，包含推理、IK、插值和规划。
工具服务时间不计入计算预算，但包含在 `run()` 的 600 秒总墙钟内。
另有 400 控制步、20 秒仿真、200 次工具调用上限；observe 也占调用次数。
规划不推进物理，但会消耗计算预算。

只通过官方 ToolClient 或 PAOS 工具桥接操作机器人；不得自行连接裁判控制口，
读取私有 casebank、目标真值或修改仿真状态。判分仅依据裁判记录的实际物理终态。
