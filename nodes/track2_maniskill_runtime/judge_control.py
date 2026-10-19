#!/usr/bin/env python3
"""**Judge-only 私有控制通道**（B1）—— 常驻 flow 里换 episode。

## 它解决什么

「循环归 Judge，flow 常驻」这条边界（`requirement/Track2_边界决策.md` §2）要求：
一个 PAOS flow 在**不重启**的情况下连续跑多个 episode。但现在的结构里：

* `runtime` 节点进 dora 主循环（常驻）—— 但**没有外部输入口**
* `player` 节点跑完一次 `run_episode()` 就走到结束
* dora **没有外部 publish**（`dora topic` 只有 list/echo/hz/info，实测）

所以需要一条**新的 Judge → Runtime 通道**。

## 为什么走私有 HTTP（而不是 dora / 文件）

| 方案 | 评价 |
|---|---|
| **私有 HTTP 控制口（本模块）** | ✅ 最简单；**安全性有实测支撑**（见下） |
| dora 新增节点 + 外部 stdin | 要改 dataflow 拓扑，且 dora 不提供外部 publish |
| 共享文件 + 轮询 | 零依赖但最土，且要处理竞态 |

**安全性支撑**：选手沙箱 `allow_network=False`（`official_runtime.py:451`）
→ `rpc.py:45` 加 `--net` → **选手侧没有任何网络**。选手的工具调用走
JSON-lines 桥（`SandboxBridge`，在宿主进程里）。所以宿主上再开一个端口，
**选手够不着**。

## 边界（必须守住）

* **不是 Tool** —— 不注册 ToolSpec、不出现在 `GET /tools`、不进选手 context
* **只绑 loopback**（`127.0.0.1`）—— 不监听外部地址
* **不做认证** —— 本机评测前提（同机、同用户）；多机部署时要加 token
  （已在代码里留了 `TRACK2_JUDGE_CONTROL_TOKEN` 的钩子）
* 端口由 `TRACK2_JUDGE_CONTROL_PORT` 指定；**不给就整个功能关闭**
  （回归环境不开这个口，避免多一个监听）

## 协议

```http
POST /episode/reset      {"case_id": "...", "seed": 0}
  → {"ok": true, "case_id": ..., "seed": ..., "seq": 1, "attempt": 1}

GET  /episode/snapshot
  → {"episode": {...}, "layout": {...}, "physical": {...}, ...}

GET  /health
  → {"ok": true, "episode": {...}}
```

★ 2026-10-18：`POST /episode/next_attempt` **已删除** —— 取消 first/final 后
不再有「失败后追加执行」。每次 `reset` 就是一段连续的、可自由闭环的执行。

`snapshot` 的 `physical` 就是 **judge-only 私有真值** ——
**绝不进 gateway、绝不进选手 context**。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["JudgeControlServer", "start_from_env"]

_MAX_BODY = 8 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "track2-judge-control/1"

    # 静默：默认实现会往 stderr 打每一行请求，会污染 dora 日志
    def log_message(self, fmt, *args):                          # noqa: A003
        pass

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                           # noqa: N802
        server: JudgeControlServer = self.server.judge_control     # type: ignore[attr-defined]
        if not server.authorized(self.headers.get("X-Track2-Judge-Token")):
            return self._reply(403, {"error": "unauthorized"})
        if self.path == "/health":
            return self._reply(200, {"ok": True, "episode": server.episode()})
        if self.path == "/episode/snapshot":
            try:
                return self._reply(200, server.snapshot())
            except Exception as exc:                            # noqa: BLE001
                return self._reply(500, {"error": type(exc).__name__,
                                         "message": str(exc)[:300]})
        if self.path == "/episode/timings":
            # Q14：动作耗时分布。**只读**，不含 GT / 位姿 / 目标信息。
            try:
                return self._reply(200, server.timings())
            except Exception as exc:                            # noqa: BLE001
                return self._reply(500, {"error": type(exc).__name__,
                                         "message": str(exc)[:300]})
        return self._reply(404, {"error": "unknown path"})

    def do_POST(self):                                          # noqa: N802
        server: JudgeControlServer = self.server.judge_control     # type: ignore[attr-defined]
        if not server.authorized(self.headers.get("X-Track2-Judge-Token")):
            return self._reply(403, {"error": "unauthorized"})
        # ★ 2026-10-19：`/episode/rotate_fence` —— 无 body，立即换 instance id
        #   释放被占死的并发围栏（见 `JudgeControlServer.rotate_fence`）。
        if self.path == "/episode/rotate_fence":
            try:
                return self._reply(200, server.rotate_fence())
            except Exception as exc:                            # noqa: BLE001
                return self._reply(500, {"error": type(exc).__name__,
                                         "message": str(exc)[:300]})
        if self.path != "/episode/reset":
            return self._reply(404, {"error": "unknown path"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            return self._reply(400, {"error": "bad body length"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._reply(400, {"error": "bad json", "message": str(exc)[:200]})
        try:
            result = server.reset(case_id=payload["case_id"],
                                  seed=payload["seed"])
        except KeyError as exc:
            return self._reply(400, {"error": f"missing field: {exc}"})
        except Exception as exc:                                # noqa: BLE001
            return self._reply(500, {"error": type(exc).__name__,
                                     "message": str(exc)[:300]})
        return self._reply(200, result)


class JudgeControlServer:
    """把 `Track2SimulationEndpoints` 的 B1/B2 能力挂到一个**私有 loopback 口**上。

    `endpoints` 必须是已经 `open()` 过的实例（场景已建）。
    """

    def __init__(self, endpoints, *, port: int, host: str = "127.0.0.1",
                 token: str | None = None):
        self._endpoints = endpoints
        self._token = token or None
        self._httpd = ThreadingHTTPServer((host, int(port)), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.judge_control = self                    # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="track2-judge-control", daemon=True)

    # ── Handler 回调

    def authorized(self, presented: str | None) -> bool:
        if self._token is None:
            return True                    # 本机评测：不开 token（见模块说明）
        return presented is not None and hmac_compare(presented, self._token)

    def episode(self) -> dict:
        return dict(getattr(self._endpoints, "_episode", {}) or {})

    def reset(self, *, case_id: str, seed: int) -> dict:
        return self._endpoints.reset_episode(case_id, int(seed))

    def snapshot(self) -> dict:
        return self._endpoints.judge_snapshot()

    def timings(self) -> dict:
        """Q14：本 flow 至今所有 Action 的执行耗时分布。"""
        return self._endpoints.judge_timings()

    def rotate_fence(self) -> dict:
        """★ 2026-10-19：**立即换 provider 的 instance id**，释放被占死的并发围栏。

        为什么需要它：gateway 的 `max_concurrency = 1` 围栏在 action deadline
        到期而 provider 未发终态时会被**有意保持**（`action_invocations.py:389`
        "Keep the concurrency fence"），此后每个 Action 都被拒
        `operation concurrency 1 is exhausted`。唯一能释放它的路径是
        `mark_instance_ambiguous` —— 而它只在「注册的 instance id 与 invocation
        钉住的不一致」时触发（`tool_directory.py:227-233`）。

        A1 只在 **episode 边界**换 id ⇒ 一个 episode 内占死无法自愈。本端点把
        换 id 变成**可被 player 即时调用**的动作，用于 episode 中途自救。

        与 A1 共用同一个钩子（`on_episode_started` → `node._rotate_instance_id`），
        所以「三处同时换」（config / handler / register 请求体）的逻辑不必重写。

        ⚠️ **幂等且安全**：换 id 只让 gateway 眼里的 provider「死一次」，
        provider 自身与物理场景**不受影响**。
        """
        hook = getattr(self._endpoints, "on_episode_started", None)
        if hook is None:
            return {"rotated": False, "reason": "no hook"}
        try:
            hook()
        except Exception as exc:                                # noqa: BLE001
            return {"rotated": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {"rotated": True}

    # ── 生命周期

    def start(self) -> "JudgeControlServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:                                       # noqa: BLE001
            pass
        try:
            self._httpd.server_close()
        except Exception:                                       # noqa: BLE001
            pass


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode("utf8"), b.encode("utf8"))


def start_from_env(endpoints, *, port_override: int = 0):
    """按环境变量 / 显式端口起控制口。**不给端口就返回 None**（功能关闭）。

    | 来源 | 说明 |
    |---|---|
    | `port_override`（来自 `runtime.yaml`） | **首选**。见下 |
    | `TRACK2_JUDGE_CONTROL_PORT` | 端口。**不设 = 关闭**（回归环境不开监听） |
    | `TRACK2_JUDGE_CONTROL_HOST` | 默认 `127.0.0.1`（只绑 loopback） |
    | `TRACK2_JUDGE_CONTROL_TOKEN` | 可选；多机部署时用 |

    ⚠️ **为什么还要一个显式端口**：`dora start --detach` 之后节点是由
    **dora daemon** spawn 的，而 daemon 是长命进程 —— 它的环境里**没有**
    `TRACK2_JUDGE_CONTROL_PORT`（实测：`/proc/<daemon>/environ` 里 0 个
    `TRACK2*`）。所以环境变量那条路在 detach 形态下**根本不生效**，
    节点侧会静默地不开控制口（表现是 judge 一直等不到 `/health`）。

    `runtime.yaml` 是**节点自己的**配置文件（`--config` 传进去的），
    且它由裁判侧逐次渲染 —— 走这条路的端口一定到得了。
    """
    port = int(port_override or 0) or int(os.environ.get("TRACK2_JUDGE_CONTROL_PORT") or 0)
    if not port:
        return None
    server = JudgeControlServer(
        endpoints, port=port,
        host=os.environ.get("TRACK2_JUDGE_CONTROL_HOST", "127.0.0.1"),
        token=os.environ.get("TRACK2_JUDGE_CONTROL_TOKEN"))
    server.start()
    print(f"[track2-runtime] judge-only 控制口就绪 127.0.0.1:{port}",
          file=__import__("sys").stderr, flush=True)
    return server
