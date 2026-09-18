# Agent CLI 与 MCP

`npu-top` 将 Agent 查询统一交给本机 `npu-top serve` 进程。默认 `cache` 模式立即读取内存快照，不建立 SSH；显式 `live` 模式由采集器发起一次集中探查并等待新快照。Agent 无需自行拼接 SSH 命令，也不会接触远程密码或监控密钥。

## 只观测，不分配

npu-top 是**观测面**，不是设备分配权威。每个响应描述的都是采集器在 `observed_at` 那一刻看到的主机状态，随时可能过期。哪些 NPU 可以被使用，由宿主机侧的 NPU 协调器队列决定；npu-top 不知道也不表达任何预约、租约或授权。

因此所有 `/api/agent/*` 响应、CLI JSON 和 MCP `structuredContent` 都带有同一个 `observation` 信封：

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

`capacity` / `find_npu_capacity` 的信封 `kind` 为 `observed_availability`，其 `observed_at` 是所依赖的最旧快照时间；每个候选还带各自的 `observed_at`。`idle_npu_count`、`busy` 这类字段天然容易被当成"可分配"，请把它们理解为"上次观测到空闲/繁忙"，然后到协调器申请设备。HTTP 响应额外携带 `X-MindIE-Top-Contract: observation-only` 头，`/api/health` 返回 `contract: observation-only`。

## CLI

```bash
npu-top servers
npu-top npu 192.0.2.21
npu-top npu 192.0.2.21 --live
npu-top npu 192.0.2.21 --ultra-compact
npu-top --json npu 192.0.2.21 --processes
npu-top status 192.0.2.21
npu-top status 192.0.2.21 --cache
npu-top mounts 192.0.2.21 --live
npu-top capacity --min-idle 4 --max-age 180 --tag A3
```

默认输出只保留状态、缓存年龄、忙闲卡数、AICore、HBM、进程数和归属：

```text
192.0.2.21 online age=4s npu=8 busy=2 util=24.5% hbm=96.0G/512.0G
0 busy util=91% hbm=41.2G/64.0G proc=1 owner=x01234567,xyz
1 idle util=0% hbm=5.9G/64.0G proc=0 owner=-
```

需要机器可读结果时使用 `--json`。`--processes` 加入精简进程记录；`--process-details` 进一步加入 pwd 和启动命令。`--max-age 180` 可约束缓存新鲜度，超限时保持输出并以退出码 `3` 标记陈旧。`status` 默认实时采样；使用 `status HOST --cache` 可直接读取缓存。实时请求由服务内采集队列执行，调用端不会直接 SSH。

`status` 汇总 NPU、CPU、内存、磁盘、Docker、占用进程/容器及可能的工号或姓名缩写；`mounts` 返回挂载源、文件系统、容量，并标出可能存放模型权重的挂载点；`capacity` 从新鲜缓存中筛选观测到满足空闲 NPU 数量和标签的机器，低优先级服务器排在最后。

默认 API 是 `http://127.0.0.1:8788`，可通过 `--url` 或 `MINDIE_TOP_URL` 修改为其他回环端口。为避免误将无认证接口暴露到网络，非回环 URL 默认拒绝。

## MCP

MCP server 使用标准输入输出传输，后端仍通过同一回环缓存 API 取数：

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

服务提供五个只读工具，每个工具的描述都声明了 observation-only 契约：

- `npu_status(host, mode?, include_processes?, process_details?, max_age_seconds?)`
- `server_status(host, mode?, process_details?, timeout_seconds?)`
- `list_mounts(host, mode?, timeout_seconds?)`
- `find_npu_capacity(min_idle_npus?, max_age_seconds?, tags?, include_disabled?)`
- `list_npu_servers()`

查询工具的文本结果与 CLI 一样精简，同时提供 `structuredContent`。`mode=cache` 适合快速初筛，`mode=live` 适合在向协调器申请设备之前做最后一次观测确认。MCP server 兼容当前无握手请求和常见的旧版 `initialize` 客户端；stdout 只写逐行 JSON-RPC 消息，诊断只写 stderr。

## 只读 HTTP API

```text
GET /api/agent/servers
GET /api/agent/npu?host=192.0.2.21&mode=cache
GET /api/agent/npu?host=192.0.2.21&mode=live&processes=1&details=1
GET /api/agent/server?host=192.0.2.21&mode=cache
GET /api/agent/capacity?min_idle_npus=4&max_age_seconds=180&tags=A3
```

`host` 精确匹配服务器 IP、显示名称、远端 hostname 或内部 id。响应中的 `source=cache` 表示数据最终来自采集器快照；`collected_at`/`age_seconds` 与 `observation.observed_at`/`observation.age_seconds` 含义相同，前者保留是为了兼容旧调用方。实时模式只是要求采集器先生成新快照，不会把 SSH 能力暴露给调用端。同一地址存在多个 SSH 端点时返回冲突错误，避免 Agent 猜测目标。

文中的 `192.0.2.x` 为 RFC 5737 文档保留地址，仅作示例。
