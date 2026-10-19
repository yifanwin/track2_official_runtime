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
├── start.sh                          启动前准备 + **注入选手包**（T3-b）
├── profiles/sim/
│   ├── dataflow.yaml                 dora 拓扑：gateway + runtime + player
│   ├── gateway.yaml                  ★ 官方 gateway 配置（**生成物**，见下）
│   ├── runtime.example.yaml          节点配置模板（可直接用）
│   ├── runtime.yaml                  ⚠️ 裁判私有，**被 .gitignore 忽略**
│   ├── player/                       ★ 选手包解到这里（每次 start 重建；已忽略）
│   └── player_work/                  选手沙箱工作区（已忽略）
└── nodes/
    ├── run_runtime.sh                启动包装（绕开 dora 的 venv 解析问题）
    ├── run_player.sh                 同上，给 player 节点
    ├── track2_maniskill_runtime/     ★ provider 节点
    │   ├── __main__.py              `python -m` 入口
    │   ├── node.py                  dora 主循环 + endpoint.register
    │   ├── endpoints.py             官方 ToolEndpoint 实现
    │   └── config.py                节点配置
    └── track2_player_node/           ★ 选手侧节点（第 4 步）
        ├── __main__.py
        └── node.py                  沙箱起停 + Tool 调用打 gateway
```

## 三个节点

```text
官方 gateway 二进制 ──── /tools + /invocations ────▶ PAOS
        ↕ dora（env_out/env_in、robot_out/robot_in）
track2-maniskill-runtime ──▶ ManiSkill/SAPIEN 里的 Piper

player ──HTTP──▶ gateway          （把选手沙箱里的 Tool 调用转成 HTTP）
   └─ 沙箱（选手 entrypoint.py）
```

**player 为什么不直连物理**：它只跟 gateway 说话；物理由 runtime 节点提供。
**player 为什么不走 dora 通道**：dora 通道是 provider↔gateway 的专用路径
（注册与执行请求），不是给「工具调用方」用的。

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
| `track2.robot.joint_trajectory` | action | 一段关节轨迹（`targets`，8–25 步）—— **唯一的关节移动途径** |
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

## 选手包注入（第 4 步）

```bash
TRACK2_PLAYER_PACKAGE=/path/to/player.zip \
  paos skill start track2-official-runtime --profile sim
```

* 解到 `profiles/<profile>/player/`，**原子落盘**（校验失败不留半个包）
* 校验不过 → `start.sh` 非零退出 → **PAOS 中止 start**
* **不给包时清掉上一轮残留** —— 避免静默复用旧选手包
* 选手 `manifest.json` 的 `tool_profile` 必须是**列表**且 ⊆ 官方三 Tool
  （旧字符串形态明确拒绝，Q16）

**两个沙箱档**（由选手 `requires_gpu` 决定）：

| 档 | 内容 | 谁用 |
|---|---|---|
| `stdlib`（默认） | 纯标准库、无 GPU、`RLIMIT_AS` 256 MiB | 不带模型的选手 |
| `official` | 官方 ML runtime（torch + CUDA）、只给 GPU 0、不设内存上限 | 带模型的选手 |

**「允许」不是「要求」**：不带模型的方案走 stdlib 档，一样完整评测。

## 依赖

节点需要**标定环境**（`../toolenv/.venv`）：Python 3.12 + 官方 `gateway` 二进制 +
官方 `forge_tool` SDK + ManiSkill/SAPIEN + CUDA torch。
见 `../toolenv/requirements.txt` 与 `52_tool_surface_decision.md`。

`start.sh` 做前置检查 —— 缺包直接失败，不让它到运行期才炸。

## 已知的坑（都踩过）

完整的台账在 `../work/reports/54_pitfalls.md`（按 PAOS 打包 / dora / forge_tool / 环境 分类）。
**最容易再踩的六条**：

| 坑 | 现象 | 解法 |
|---|---|---|
| **同版本号 → install 空转** | 改了代码但行为没变，也不报错 | bump `skill.yaml` 的 `version`；用 `work/scripts/pack_bundle.sh` |
| **`path:` 不展开任意环境变量** | daemon 找不到解释器 | 用 `nodes/run_runtime.sh` 包一层 |
| **daemon 解析 venv 符号链接** | `No module named 'pyarrow'` | 同上（venv 识别交给 python 自己） |
| **dora 会写 `out/`** | 运行产物被打进 Bundle | 打包/收尾时 `rm -rf profiles/sim/out` |
| **provider 的 `context.tool_id` 不是 Tool ID** | `unknown tool 'track2.env'` | `endpoints.py: _TOOL_IDS` 显式映射 |
| **handler 只在终态事件时推进 phase** | phase 永远停在 `accepted` | 执行体跑完发 `ToolEvent(executor_completed/...)` |
| **官方 gateway 的 phase 词表与本地不同** | 客户端永远轮询到超时 | `toolapi/client.py` 的 `is_terminal_phase()` 两套都认 |
| **官方 Query 响应多套一层** | `observe()["robot"]` → `KeyError` | `query_payload()` 归一化 |
| **官方不转发 `ToolControlResponse.details`** | Q13 三态到不了客户端 | 超时证据改用「phase 已终结」 |
| **seccomp 的 `clone3` 规则顺序** | `cuda.is_available()==False`（像没插显卡） | `clone3` 只能有一条 `ENOSYS` 规则，不能被 `EPERM` 遮蔽 |
| **`--pid` 后不挂新 `/proc`** | 同上，`cuInit=304` | `unshare` 加 `--mount-proc` |

## 路径策略（三条规则，各有理由）

改动这几个文件时最容易搞混：

| 项 | 怎么定 | 为什么 |
|---|---|---|
| `TRACK2_PYTHON` / `TRACK2_PHYSICS_ROOT` | `skill.yaml` 的 `environment`，**绝对路径** | 它们在 Bundle **外**，install 时不变 |
| `nodes/` 的路径 | `run_runtime.sh` 用 `$0` **自己算** | install 后 Bundle 被搬走，`nodes/` 跟着走 |
| `casebank` | YAML 写**相对路径**，相对 `TRACK2_PHYSICS_ROOT` | installer 会把 profile 文件软链到别处，**不能**按配置文件目录推 |

## `case_id` / `seed` 走环境变量

它们是**每个 case 的评测输入**，不该固化进 Bundle（否则换个 case 就得重打包）。

```bash
export TRACK2_CASE_ID=016_synthetic_images__scene_image_2
export TRACK2_SEED=0
```

`runtime.yaml` 里留的值只是**调试默认值**。
评测机通过 `skill.yaml` 的 `profiles.<p>.environment` 或启动前 export 注入。

## 状态

**第 3–4 步已完成**（2026-10-16）。

| 项 | 状态 |
|---|---|
| 标定环境（3.12：tool 面 + 物理侧一个环境，CUDA 可用） | ✅ |
| `gateway.yaml`（官方 gateway 实测加载；`GET /tools` 返回我们的三个 spec） | ✅ |
| provider 节点（离线自检 23 项全过；端到端经官方 gateway 通） | ✅ |
| **经官方 gateway 的完整 invoke** | ✅ 见下 |
| 21 项回归在 3.12 上复核 | ✅ 与 3.10 **零差异** |
| `paos skill start` 起整条链 | ✅ 三条 Tool 全 ready |
| **选手包注入 + 沙箱起停（第 4 步）** | ✅ 两条档都跑通（stdlib / official+CUDA） |

### 端到端实测（`work/scripts/e2e_step3.sh --action`）

> ⚠️ **下面是 2026-10-16（第 3 步）的原始记录，当时 Tool 面还是单步 `joint_control`。**
> **2026-10-18 起该 Tool 已删除**，唯一的关节控制途径是 `joint_trajectory`（`targets`，8–25 步）。
> 记录本身保留原样（历史证据），**当前接口以 `SKILL.md` 与 `schema.py` 为准**。

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
