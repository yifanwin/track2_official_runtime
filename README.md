# track2-official-runtime —— Track 2 官方 Tool Runtime Bundle

Track 2 的**唯一 Tool 面**（Q16 单一新轨）。环境怎么提供、反馈什么、机器人怎么控制，
全部经这里的三个 Tool。**选手不允许修改这个 Bundle。**

> 架构决策与实测依据：`../work/reports/52_tool_surface_decision.md`

---

## 目录

```text
track2_official_runtime/
├── skill.yaml                        manifest v2（PAOS 读）
├── SKILL.md                          PAOS 硬约束：必须叫这个名字
├── start.sh                          启动前准备（注入标定环境路径 + 前置检查）
├── profiles/sim/
│   ├── dataflow.yaml                 dora 拓扑：gateway + runtime
│   ├── gateway.yaml                  ★ 官方 gateway 配置（**生成物**，见下）
│   ├── runtime.example.yaml          节点配置模板（可直接用）
│   └── runtime.yaml                  ⚠️ 裁判私有，**被 .gitignore 忽略**
└── nodes/
    ├── run_runtime.sh                启动包装（绕开 dora 的 venv 解析问题）
    └── track2_maniskill_runtime/     ★ provider 节点
        ├── __main__.py              `python -m` 入口
        ├── node.py                  dora 主循环 + endpoint.register
        ├── endpoints.py             官方 ToolEndpoint 实现
        └── config.py                节点配置
```

## 两个节点

```text
官方 gateway 二进制 ──── /tools + /invocations ────▶ PAOS
        ↕ dora（env_out/env_in、robot_out/robot_in）
track2-maniskill-runtime ──▶ ManiSkill/SAPIEN 里的 Piper
```

| 层 | 谁提供 |
|---|---|
| ToolSpec 声明 | **我们**（`../track2_final/track2_judge/toolapi/schema.py` —— 真源） |
| `/tools` + `/invocations` 路由 | **官方 gateway 二进制** |
| ToolEndpoint 协议 | **官方 `forge_tool` SDK** |
| 物理执行 | **我们**（`nodes/track2_maniskill_runtime/`） |

## 三个 Tool

| Tool ID | 语义 | 用途 |
|---|---|---|
| `track2.env.observe` | query | 读观测（本体状态 + task_inputs + 相机帧） |
| `track2.robot.joint_control` | action | 一步关节目标（6 维）+ 夹爪开度 |
| `track2.robot.gripper_control` | action | 只动夹爪 |

### Action 治理（Q9 / Q12 / Q13 / Q14）

| # | 落实位置 |
|---|---|
| **Q9** `max_concurrency = 1` | **两处**都要：`gateway.yaml` 的 ToolSpec 侧 + `endpoint.register` 的 `ToolOperationDescriptor` |
| **Q12** 执行超时 | `runtime.yaml: action_timeout_s`（当前 `30.0`，标 `PROVISIONAL_PENDING_MEASUREMENT`，Q14 待实测） |
| **Q13** cancel 不谎称已停 | `endpoints.py: cancel()` —— 官方 wire **没有** `execution_status` 概念，我们放在 `ToolControlResponse.details` 里带着走 |
| **Q14** 超时秒数 | 待 73 case 实测 |

**Q13 的三态语义**（这是最容易丢的东西）：

| 字段 | 含义 |
|---|---|
| `ToolControlResponse.status` | `accepted` —— **只说明请求被受理** |
| `details.execution_status` | `stopped_confirmed` / `unknown` —— **物理上停没停** |
| `details.cancel_effective` | 取消**是否真的阻止了物理效果** |

## ⚠️ `gateway.yaml` 是生成物 —— 不要手改

它必须与 `schema.py` **逐字节一致**，否则 PAOS 绑定时报
`ToolSpec changed after AgentTask binding`。生成与校验：

```bash
S=../work/scripts/gen_gateway_yaml.py

# 生成
../toolenv/.venv/bin/python $S

# 校验（交付前跑）
../toolenv/.venv/bin/python $S --check
```

改 ToolSpec 的流程：**改 `schema.py` → 重新生成 → 两者一起提交**。

## 本地跑一条链路

最省事的做法是让 PAOS 托管（第 3.6 步的验收内容）：

```bash
paos skill install track2-official-runtime --local
paos skill start   track2-official-runtime --profile sim
paos skill status  track2-official-runtime   # 三条 Tool 应全 ready
```

手工调试（不经 PAOS）：

```bash
cd wenyifan/track2_official_runtime/profiles/sim
cp runtime.example.yaml runtime.yaml     # 含 case_id/seed（私有）

export PATH="$HOME/.dora/bin:$PATH"
dora run dataflow.yaml                   # 在 profile 目录里跑，相对路径才对
```

## 依赖

节点需要**标定环境**（`../toolenv/.venv`）：Python 3.12 + 官方 `gateway` 二进制 +
官方 `forge_tool` SDK + ManiSkill/SAPIEN + CUDA torch。
见 `../toolenv/requirements.txt` 与 `52_tool_surface_decision.md`。

`start.sh` 做前置检查 —— 缺包直接失败，不让它到运行期才炸。

## 已知的坑（都踩过）

| 坑 | 现象 | 解法 |
|---|---|---|
| **dora 解析 venv 符号链接** | `No module named 'pyarrow'` | 用 `nodes/run_runtime.sh` 包一层 |
| **`./run.sh` 不能直接当 `path:`** | daemon 把它当 py 文件喂给 python | `path:` 必须指向**可执行文件**（脚本或解释器绝对路径） |
| **provider 的 `context.tool_id` 不是 Tool ID** | schema 校验报 `unknown tool 'track2.env'` | 边界上显式映射，见 `endpoints.py: _TOOL_IDS` |
| **`image_stale_after_sec` 不能是 null** | 官方 config 解析拒绝 | 省略该键（`require_images: false` 时它不参与判定） |
| **端口 19110 被占** | `address already in use` | 上一次 gateway 没退干净；`pkill -f "gateway --config"` |

## 状态

**第 3 步已完成**（2026-10-16）。

| 项 | 状态 |
|---|---|
| 标定环境（3.12：tool 面 + 物理侧一个环境，CUDA 可用） | ✅ |
| `gateway.yaml`（官方 gateway 实测加载；`GET /tools` 返回我们的三个 spec） | ✅ |
| provider 节点（离线自检 23 项全过；端到端经官方 gateway 通） | ✅ |
| **经官方 gateway 的完整 invoke** | ✅ 见下 |
| 21 项回归在 3.12 上复核 | ✅ 与 3.10 **零差异** |
| `paos skill start` 起整条链 | ⏳ 第 3.9 步 |

### 端到端实测（`work/scripts/e2e_step3.sh --action`）

```
[3] gateway 已监听，revision=2          ← provider 的 endpoint.register 被接受
revision 2
  track2.env.observe
  track2.robot.gripper_control
  track2.robot.joint_control

POST /tools/track2.env/observe:invoke -> 200
  control_steps: 0 / camera_status: "NOT CONFIGURED" / joint_limits_rad 真实值
POST /tools/track2.robot.joint_control:invoke -> 202
  轮询 -> phase: completed
  result: control_steps: 1 / sim_time_s: 0.05      <- 物理**真的推进了一步**
```

### 复现

```bash
bash work/scripts/e2e_step3.sh              # Query
bash work/scripts/e2e_step3.sh --action     # Query + Action
```

脚本自己负责清场、起 flow、探活、抓两侧日志、收尾 ——
**不要并发跑**（它有单实例锁，会拒绝）。
