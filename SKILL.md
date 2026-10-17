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
| `track2.robot.joint_control` | **action** | 下发一步关节目标（6 维）与夹爪开度 |
| `track2.robot.gripper_control` | **action** | 只动夹爪，关节保持在上一次目标 |

调用前先用 `forge_tool_context` 读它们的**实时** schema、readiness 与机器人坐标系；
**不要臆造**字段、单位、限位或默认值。

## 观测里有什么

`track2.env.observe` 返回：

- `robot.joint_position` —— 8 维（6 个臂关节 + 2 个夹爪手指的位置）
- `robot.joint_velocity` / `joint_limits_rad` / `tcp_position_m` / `tcp_quaternion_wxyz`
- `control_steps`、`sim_time_s` —— 本次 episode 已推进的控制步与仿真时间
- `task_inputs` —— 任务指令（`text_instruction` 等）
- `images.camera` —— **当前帧**（PNG，base64 + sha256）。它在**每个控制步都更新**，
  不是只给一次；判「这一帧是不是新的」用 `sha256` 自比。
  `images.camera_status` 是**运行事实**（拍到没拍到），不是目标答案；
  拍失败时 `camera` **不会**给上一帧（避免你把过期画面当新的）。

**夹爪开度**从关节位置推：`gripper_opening_m = joint_position[6] - joint_position[7]`。

## 怎么控制机器人

`track2.robot.joint_control` 是**控制步级**的：

- `joint_position`：6 个臂关节目标（rad）
- `gripper_opening_m`：夹爪总开度（m），范围 `[0, 0.1]`
- **每一步都要自己规划** —— IK、轨迹、抓取顺序全是你的活。runtime 不做 FK/IK/轨迹生成。

**目标不能突跳。** runtime 按 `1 rad/s`（臂）与 `0.12 m/s`（夹爪）的上限校验；
超限的一步会被拒，且**那一步不会推进物理**。

**动作是串行的**（`max_concurrency = 1`）。一次只能有一个动作在跑，不要并发提交。

## Action 生命周期（重要）

`joint_control` / `gripper_control` 是 **Action**，不是同步函数：

```text
POST /tools/track2.robot.joint_control:invoke
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
3. 自己规划：从观测里的关节位置推目标
4. forge_tool_start_action('track2.robot.joint_control', {
       'joint_position': [...6 个数...], 'gripper_opening_m': 0.05 })
5. 轮询 forge_tool_action_status 直到 phase 终结
6. forge_tool_action_result 读结果
7. 重复 3–6 直到任务完成
```
