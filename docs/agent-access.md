# Agent CLI 与 MCP 查卡工具详解

**中文** · [English](./agent-access.en.md) · [返回文档中心](./README.md)

为了让 AI 编程助手（Claude Code、Codex、Kimi Code、Cursor 等）也能随时查服务器状态，`npu-top` 把所有查询统一收口到了本地运行的 `npu-top serve` 服务上：

- **默认读内存缓存（`cache` 模式）**：毫秒级直接返回采集器最近一次拍下的内存快照，不发起新的 SSH 连接；
- **按需实时刷新（`live` 模式）**：当你准备跑大任务、想再确认一眼最新状态时，加上 `--live`（或 `mode="live"`），由后台采集器统一去做一次实时探测并返回最新结果；
- **零密码暴露**：AI Agent 全程只跟本地 `127.0.0.1:8788` 通信，不需要自己拼复杂的 `ssh` 命令，也绝不会接触远端服务器密码或监控私钥。

---

## 1. 读懂返回结果里的 `observation` 信封

`npu-top` 是**只读路况监控**，不是分卡调度器。因此无论是调 `/api/agent/*`、用 CLI `--json` 还是调 MCP 工具，返回结果里都会带上同一个 `observation` 信封：

```json
"observation": {
  "kind": "observed_state",
  "observed_at": 1756450000,
  "observed_at_iso": "2026-08-29T06:46:40Z",
  "age_seconds": 4,
  "allocation_authority": false,
  "notice": "Observed host state at observed_at. Not an allocation or reservation source; ..."
}
```

- `age_seconds: 4` 表示这是 4 秒前采集到的状态；
- `allocation_authority: false` 提醒调用方：看到 `idle_npu_count: 4` 只代表“4 秒前看它有 4 张卡闲着”，不代表这 4 张卡已经给你留好了。选好候选机器后，请通过 `mindie-coordinator` 正式排队申请设备租约。

---

## 2. 终端 CLI 常用命令速查

```bash
# 列出所有受监控的服务器概览
npu-top servers

# 查看某台机器每张 NPU 卡的忙闲、算力、显存和占用人（默认读缓存，秒回）
npu-top npu 192.0.2.21

# 强制采集器立刻去远端探测一次最新 NPU 状态
npu-top npu 192.0.2.21 --live

# 极简输出模式（适合塞进紧凑提示词）
npu-top npu 192.0.2.21 --ultra-compact

# 输出结构化 JSON，并附上占用进程的 PID 与工作目录详情
npu-top --json npu 192.0.2.21 --processes
npu-top --json npu 192.0.2.21 --process-details

# 查看整机详细状态（NPU + CPU + 内存 + 磁盘 + Docker 容器，默认 live 实时探测）
npu-top status 192.0.2.21
npu-top status 192.0.2.21 --cache

# 查看挂载盘容量，找地方存模型权重
npu-top mounts 192.0.2.21 --live

# 筛选最近 180 秒内观测到至少有 4 张空闲卡、且带有 A3 标签的候选服务器
npu-top capacity --min-idle 4 --max-age 180 --tag A3
```

默认文本输出专门针对人类阅读和模型 Token 消耗做过精简，只保留最核心的信息：

```text
192.0.2.21 online age=4s npu=8 busy=2 util=24.5% hbm=96.0G/512.0G
0 busy util=91% hbm=41.2G/64.0G proc=1 owner=x01234567,xyz
1 idle util=0% hbm=5.9G/64.0G proc=0 owner=-
```

**实用小技巧**：
- 想检查缓存是不是太老？加上 `--max-age 180`。如果快照已经超过 180 秒，命令依然会打印数据，但会以退出码 `3` 提醒你数据已陈旧。
- 默认连接 `http://127.0.0.1:8788`，可通过 `--url` 或环境变量 `MINDIE_TOP_URL` 改成其他本地回环端口（为保护安全，默认拒绝非回环的外部网络地址）。

---

## 3. 给 AI 助手挂载 MCP 工具

在客户端配置文件（如 `.mcp.json`）中添加：

```json
{
  "mcpServers": {
    "npu-top": {
      "command": "npu-top",
      "args": ["mcp"],
      "env": { "MINDIE_TOP_URL": "http://127.0.0.1:8788" }
    }
  }
}
```

挂载后，AI 助手会获得 5 个只读工具：

| MCP 工具名 | 参数 | 适用场景 |
| --- | --- | --- |
| `list_npu_servers()` | 无 | 一眼看完当前监控了哪些服务器、各有多少张空卡 |
| `find_npu_capacity(...)` | `min_idle_npus?`, `max_age_seconds?`, `tags?`, `include_disabled?` | 按所需空闲卡数和硬件标签（如 `A3`）快速挑出候选服务器（低优先级机器自动排最后） |
| `npu_status(...)` | `host`, `mode?`, `include_processes?`, `process_details?`, `max_age_seconds?` | 查看指定服务器上每张卡的利用率、HBM 显存和是谁占着卡 |
| `server_status(...)` | `host`, `mode?`, `process_details?`, `timeout_seconds?` | 查看指定服务器的 CPU、内存、磁盘、Docker 容器和占用人详情 |
| `list_mounts(...)` | `host`, `mode?`, `timeout_seconds?` | 查看指定服务器的所有磁盘挂载点容量，并找出适合放模型权重的目录 |

> **推荐配合方式**：先用 `mode="cache"` 从内存里快速初筛出候选机器，选定目标后再用 `mode="live"` 做最后一次实时确认，然后交给 `mindie-coordinator` 申请用卡。

---

## 4. 本地只读 HTTP 接口一览

如果你在写自己的本地脚本，也可以直接请求回环 HTTP 接口：

```text
GET /api/agent/servers
GET /api/agent/npu?host=192.0.2.21&mode=cache
GET /api/agent/npu?host=192.0.2.21&mode=live&processes=1&details=1
GET /api/agent/server?host=192.0.2.21&mode=cache
GET /api/agent/capacity?min_idle_npus=4&max_age_seconds=180&tags=A3
```

其中 `host` 可以传服务器 IP、显示别名、远端 `hostname` 或内部 ID（如果同一个 IP 配了多个不同端口的 SSH 端点，接口会明确报错提示歧义，不会瞎猜）。*注：文档中的 `192.0.2.x` 为 RFC 5737 示例地址。*
