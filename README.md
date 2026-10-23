# Track2 Official Runtime

日期：2026-10-23。发布版本：**0.3.5**。
当前赛题为 **30 case × 5 seed = 150 episode**。选手计算预算 **300 秒**，
工具服务时间不计入计算预算；`run()` 总墙钟上限 **600 秒**，包含工具服务时间。
控制预算为 **400 步 / 20 秒仿真 / 200 次工具调用**，单个动作超时 **5 秒**。
正式观测为固定与腕部两路 RGB。
选手包根目录 `model/` 的总大小和单文件大小上限均为 8 GiB（解压后字节数）。

Bundle 提供唯一官方工具面：`track2.env.observe`、`track2.robot.joint_trajectory`、
`track2.robot.gripper_control`。选手不能修改此 Bundle。

gateway 处理官方 Tool 协议；runtime 负责观测与物理执行；player 在 official 沙箱内
运行选手代码，经 ToolClient 调用 gateway。所有选手使用同一官方运行环境。

`toolapi/schema.py` 是 ToolSpec 真源，`profiles/sim/gateway.yaml` 为生成物。
改 schema 后重新生成并校验；不要只手改 gateway.yaml。
Action 并发数为 1，超时 5 秒；调用接纳、执行完成和取消确认应分别处理。

发布 `runtime.yaml` 与 `runtime.example.yaml` 一致，控制端口默认 0。
正式入口分配实际控制端口并渲染 GPU 等运行配置。
`skill.yaml` 保存机器相关解释器和物理源码路径；迁机需同步调整。

动作字段、观测和边界见 `SKILL.md`。部署入口为工作区
`report/delivery/current/Track2_部署发布交付说明_20261023.md`。
