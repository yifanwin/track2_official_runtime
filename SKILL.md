---
name: track2-official-runtime
description: Track 2 官方 ManiSkill Tool runtime —— 读取环境、控制机器人。
metadata: {"PhyAgentOS":{"always":true,"requires":{"runtime":["track2-official-runtime"]}}}
---

# Track 2 Official Runtime

这是 Track 2 的**唯一 Tool 面**。环境怎么提供、反馈什么、机器人怎么控制，
全部经这里的三个 Tool。**选手不允许修改这个 Bundle。**

## 三个 Tool（只用这些稳定的 Tool ID）

| Tool ID | 语义 | 用途 |
|---|---|---|
| `track2.env.observe` | **query** | 读当前观测：机器人本体状态 + 任务输入 + 相机帧 |
| `track2.robot.joint_trajectory` | **action** | 下发**一段**关节轨迹（`targets`，**8–25** 步）—— **唯一的关节移动途径** |
| `track2.robot.gripper_control` | **action** | 只动夹爪，关节保持在上一次目标 |

调用前先用 `forge_tool_context` 读它们的**实时** schema、readiness 与机器人坐标系；
**不要臆造**字段、单位、限位或默认值。

> ⚠️ **旧的单步 `track2.robot.joint_control` 已彻底删除**（2026-10-18，不给兼容期）。
> 调它（或 `step` / `piper_joint_control`）会 `AttributeError` / 准入失败。
> 现在**唯一**能移动关节的就是 `joint_trajectory`，入参是 `targets`（**8–25 步**）。

## 观测里有什么

`track2.env.observe` 返回：

- `robot.joint_position` —— 8 维（6 个臂关节 + 2 个夹爪手指的位置）
- `robot.joint_velocity` / `joint_limits_rad` / `tcp_position_m` / `tcp_quaternion_wxyz`
- `control_steps`、`sim_time_s` —— 本次 episode 已推进的控制步与仿真时间
- `task_inputs` —— 任务指令（`text_instruction`）、参考图（`reference_image`）等
- `images.camera` —— **当前帧**（PNG，base64 + sha256）。它在**每次 `observe` 时更新**，
  不是只给一次；判「这一帧是不是新的」用 `sha256` 自比。
  `images.camera_status` 是**运行事实**（拍到没拍到），不是目标答案；
  拍失败时 `camera` **不会**给上一帧（避免你把过期画面当新的）。

**夹爪开度**从关节位置推：`gripper_opening_m = joint_position[6] - joint_position[7]`（**差，不是和**）。

> ⚠️ **读错一个符号就整 case 归零**：初始两指是 `[+0.04, -0.04]`，真实开度 `0.04-(-0.04)=0.08 m`；
> 写成 `+` 会得到 `0.00`，第一次下发即与物理差 `0.08 m` > 单步限幅 `0.006 m` →
> `target slew exceeded` → **Gate BLOCKED、全盘 0 分**。

## 怎么控制机器人

`track2.robot.joint_trajectory` 收**一段**轨迹（动作块），**不是单步**：

```jsonc
{
  "targets": [                         // 必须是 8–25 项
    { "joint_position": [6 个臂关节弧度], "gripper_opening_m": 0.05 },
    ...                                // 每项 = 一个控制步（0.05 s 仿真）
  ]
}
```

- **整段先校验、整段执行** —— 任一步不合规，**整条被拒**，物理**一步都不推进**。
- **步间限幅**：相邻两步的臂关节变化 ≤ **0.05 rad**、夹爪开度变化 ≤ **0.006 m**。
  超限会拒（`target slew exceeded`）。
- **片长 8–25**：少于 8 步会被 schema 拒（堵死「单步退化」）；超过 25 也要拆。
- **片内是盲走** —— 一次调用期间不重拍图；要闭环就**多调几次** `observe` + `joint_trajectory`。
- **规划是你的活**：IK、插值、waypoint、抓取顺序全在**你的进程里**算，不占预算、不碰物理；
  算完再拆成 8–25 步一片下发。

**动作是串行的**（`max_concurrency = 1`）。一次只能有一个动作在跑，不要并发提交。

## Action 生命周期（重要）

`joint_trajectory` / `gripper_control` 是 **Action**，不是同步函数：

```text
POST /tools/track2.robot.joint_trajectory:invoke
  → 202 + invocation_id + attempt_id        ← 这只是「已接纳」
  → 轮询 GET /invocations/{invocation_id}   → 看 phase
  → GET /invocations/{invocation_id}/result → 拿结果
```

**接纳不等于完成。** 用 `forge_tool_start_action` / `forge_tool_action_status` /
`forge_tool_action_result` 这三个桥接工具，**不要**自己拼 Gateway 的 HTTP 请求。

### 取消的语义（务必看清）

`forge_tool_cancel_action` 返回 `cancel_status=accepted`
**只说明取消请求被受理，不证明物理已经停下**。

- `execution_status` 才是事实：`stopped_confirmed` = 已确认停止；`unknown` = **无法确认**。
- 超时不代表动作没做，也不代表已经停了。
- **`unknown` 时不要盲目重试** —— 重新提交可能让物理动作重复执行。

## 机器人坐标系

- `base_frame`: `arm_base`；`tool_frame`: `tcp`
- 用 `forge_tool_context` 读实时 `robot_frame_profile`，不要假设朝向。

## 你必须遵守的边界

- 只用上面三个 Tool ID。**未声明的 Tool 一律被拒**（含 `tabletop.*`、`motion.*`
  与任何裁判侧接口）。
- **不要**试图读裁判私有数据（casebank / 私有 GT / 评分器）；沙箱会拦住，
  且被拦的行为会体现在证据里。
- 不要自己构造 Gateway 的 HTTP 请求或调用 Shell —— 走 PAOS 的桥接工具。

## 一个最小流程

```text
1. forge_tool_context('track2.env.observe')          # 读 schema 与 readiness
2. forge_tool_query('track2.env.observe', {})         # 拿初始观测
3. 自己规划：从当前关节位置算出一段 8–25 步的轨迹
4. forge_tool_start_action('track2.robot.joint_trajectory', {
       'targets': [ {'joint_position': [...6 个数...], 'gripper_opening_m': 0.05}, ... ] })
5. 轮询 forge_tool_action_status 直到 phase 终结
6. forge_tool_action_result 读结果
7. 重复 2–6（**再观察再执行**）直到任务完成
```
