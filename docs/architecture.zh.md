# 系统架构与采集设计

**中文** · [English](./architecture.md) · [返回文档中心](./README.md)

```text
Browser (127.0.0.1:8788)
  ├─ 静态监控看板 + 历史趋势/热力图 + 服务器批量接入
  └─ 同源请求 /api/*
        ↓ 仅限本地回环 (Loopback)，单进程运行
npu-top serve (HTTP API + AdaptiveScheduler)
  ├─ 页面租约刷新率：取所有打开页面的最快档位 (1s / 5s / 10s / 30s)
  ├─ 空闲巡检周期：120s
  ├─ 历史库写入下限：30s
  ├─ 基础设施采集周期：60s
  └─ 有界并发主机探测线程池
        ↓ 项目专用 Ed25519 密钥 + OpenSSH 多路复用
裸机 NPU 宿主机
  ├─ npu-smi info + usages
  ├─ /proc/stat, /proc/loadavg, /proc/meminfo
  ├─ df + findmnt
  └─ docker ps + docker info
```

## 宿主机访问与清单合并

`DeviceAdapter`（`npu_top/device_adapter.py`）是所有宿主机交互的组合根（Composition Root），由显式注入的组件装配而成，不假设监控台自身的安装位置或上层集群管理工具：

- **`SshAccess`**：管理监控台专用的 Ed25519 密钥、独立的 `known_hosts` 文件以及用于探测的 OpenSSH 参数。
- **`npu_smi`**：解析 `npu-smi info` 与 `npu-smi info -t usages` 输出，内置于本包并通过完备单元测试覆盖，运行时不依赖任何外部解析器。
- **显式清单源**：通过环境变量注入 `NFM_INVENTORY_FILES`（JSON `{"machines": [...]}`，仅读取主机端点字段）与 `NFM_HOST_POOL_FILES`（纯文本主机列表，仅读取首列地址），绝不向上遍历父目录或 Git 公共目录猜测清单。
- **`ExternalKeyBootstrap`**：可选运行 `NFM_BOOTSTRAP_COMMAND` 模板，通过 `stdin` 传入一次性密码以安装监控台公钥；未配置时仅支持基于已有密钥的访问。

启动时按顺序合并清单源：活动清单提供别名与硬件标签；仅出现在主机池中的地址会自动附加派生的 `低优先级` 标签。主机池文件中的相邻认证列绝不进入应用状态。

## 只观测，不分配（Observation-Only）契约

`npu-top` 仅负责观测，不是设备分配权威，不得用于决定某个任务可以占用哪些 NPU。在典型部署中，只有宿主机侧的 NPU 协调器队列（Coordinator）拥有设备分配权；机器清单声明存在哪些主机；监控台汇报主机在最近一次探测时刻的观测快照。

这一契约直接体现在接口层：

- 所有 `/api/agent/*` 响应均携带 `observation` 信封（`agent_view.observation_envelope`），包含 `kind`、`observed_at`、`observed_at_iso`、`age_seconds`、`allocation_authority: false` 以及可读的 `notice`。
- `/api/agent/capacity` 使用 `kind: observed_availability`，其 `observed_at` 为所依赖的最旧快照时间，并声明空闲卡数并非设备预留。
- 所有 `/api/` 响应均携带 HTTP 头 `X-MindIE-Top-Contract: observation-only`，`/api/health` 返回 `contract: observation-only`。
- 每个 MCP 工具描述与 CLI 帮助末尾均包含相同声明，由 `tests/test_agent_cli_mcp.py` 强制守护。

## 多级采集策略

- **高频层**：读取 CPU 计数器、负载、内存与 NPU 状态。CPU 利用率通过连续两次 `/proc/stat` 差值计算，远端探测无需 `sleep`。
- **基础设施层**：独立缓存磁盘挂载与 Docker 容器信息（默认 60s 一次），降低宿主机开销。
- **NPU 判忙规则**：当宿主机进程表存在占用进程、AICore 利用率超过 `1%` 或 HBM 超过兜底阈值（默认 `8192 MB`，避开 A3 设备约 6 GB 的驱动常驻占用）时判定为 `busy`。
- **非重叠周期与跨平台 SSH 控制**：上一轮采集完成后才调度下一轮；POSIX 使用短路径 `ControlPersist` 复用连接，原生 Windows 自动省略 Unix 域控制套接字参数并为生成的私钥设置仅当前用户可读的 Windows ACL。

## 持久化与隐私

SQLite 运行于 WAL 模式，主索引为 `(server_id, collected_at DESC)`。热力图接口利用 SQLite JSON 函数将摘要列与 `payload_json.devices` 按时区对齐为 2 小时时间桶。任何表结构与日志中均不存储密码。
