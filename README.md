<h1 align="center">MindIE Agent NPU Top (<code>npu-top</code>)</h1>

<p align="center">
  <strong>面向本机单用户的昇腾 NPU 无代理监控台——支持 Web 实时看板、90 天有界历史热力图及只读 Agent CLI / MCP 观测接口</strong>
</p>

<p align="center">
  <strong>中文</strong> · <a href="./README.en.md">English</a> · <a href="./docs/README.md">文档中心</a> · <a href="https://github.com/mindie-agent/mindie-agent">MindIE Agent 主仓库</a>
</p>

---

## 项目简介

`npu-top` 是 [MindIE Agent](https://github.com/mindie-agent/mindie-agent) 的本机单用户昇腾（Ascend）NPU 监控与可观测性组件。它通过宿主机 SSH 无代理（Agentless）采集多台服务器的 NPU 利用率、HBM 显存、CPU、系统内存、磁盘、挂载点与 Docker 容器状态，提供本地回环 Web 看板、SQLite 有界历史趋势/热力图，以及面向 Coding Agent 的统一只读 CLI 与 MCP 接口。

> [!IMPORTANT]
> **`npu-top` 只观测，不分配（Observation-Only）。**
> 它不是设备分配权威：哪些 NPU 可以被任务占用，由宿主机侧的 NPU 协调器队列（[`mindie-agent/coordinator`](https://github.com/mindie-agent/coordinator)）决定。`npu-top` 的所有 Agent 接口均附带带观测时间戳的 `observation` 信封（`allocation_authority: false`），明确声明不得直接据此做设备抢占或分配决策。详见 [Agent CLI 与 MCP 接口说明](./docs/agent-access.md) 与 [架构说明](./docs/architecture.zh.md)。

---

## 核心特性

- **无代理宿主机 SSH 采集**：在裸机宿主机上运行 `npu-smi`，跨容器透视设备占用；内置解析器完整覆盖单 Die（910B 类）、双 Die（A3 / 910C 类）与 Ascend 950（A5）设备表、进程表及 `-t usages` 输出。
- **自适应多级采样调度**：Web 页面打开时支持 1s / 5s / 10s / 30s 实时刷新，页面关闭后自动回落至 120s 巡检；高频交互快照保留在内存，SQLite 历史库默认最短每 30s 落盘一次。
- **有界历史存储与热力图**：保存 15 分钟粒度的主机/设备聚合摘要，支持 1 小时至 90 天趋势报表与按 2 小时时段排列的 CPU、内存、NPU、HBM 及逐卡 AICore 热力图；通过 `NFM_SQLITE_MAX_MB`（默认 1024 MB）严格限制 SQLite 库体积。
- **安全密钥引导与零明文落盘**：批量添加服务器时，密码候选仅在单次请求内存中通过 `stdin` 传给引导命令以安装专用 Ed25519 公钥，绝不写入数据库、配置文件、命令行参数或日志。
- **面向 Agent 的统一 CLI 与 MCP**：提供 5 个只读 MCP 工具与对应 CLI 子命令，支持从内存缓存（`cache`）快速筛选候选机器或触发采集器实时探查（`live`），将 SSH 细节完全封装在常驻采集器内。

---

## 快速开始

### 环境要求

- **运行时**：Python `>= 3.11`、系统 OpenSSH 客户端（`ssh` 与 `ssh-keygen`）。后端仅使用 Python 标准库与固定版本的 `mindie-diagnostics`，无需安装 `torch` / `torch_npu`。
- **源码构建前端**：仅从源码或 Git 直接构建 Wheel 时需要 Node.js `>= 22.13`。

### 1. 推荐方式：使用 GitHub Release Wheel 启动（无需本机 Node.js）

Release 资产中的 Wheel 已内置编译好的前端静态资源：

```bash
gh release download v0.1.6 -R mindie-agent/npu-top -p '*.whl'
uvx --from ./npu_top-0.1.6-py3-none-any.whl npu-top serve
```

也可以直接指向 Release 资产 URL：

```bash
uvx --from "https://github.com/mindie-agent/npu-top/releases/download/v0.1.6/npu_top-0.1.6-py3-none-any.whl" npu-top serve
```

启动后在浏览器访问 `http://127.0.0.1:8788`。`npu-top serve` 单进程同时提供 HTTP API 与静态前端，默认仅绑定本地回环地址 `127.0.0.1`。

### 2. 开发者方式：从 Git 或源码构建（需 Node.js 22.13+）

```bash
# 直接通过 uvx 从 Git 构建并启动
uvx --from git+https://github.com/mindie-agent/npu-top@main npu-top serve

# 或克隆仓库后本地构建 Wheel
git clone https://github.com/mindie-agent/npu-top.git
cd npu-top
./scripts/build_wheel.sh
```

### 3. 配置 MCP Server

在客户端 `.mcp.json` 中注册：

```json
{
  "mcpServers": {
    "npu-top": {
      "command": "uvx",
      "args": [
        "--from",
        "https://github.com/mindie-agent/npu-top/releases/download/v0.1.6/npu_top-0.1.6-py3-none-any.whl",
        "npu-top",
        "mcp"
      ],
      "env": {
        "MINDIE_TOP_URL": "http://127.0.0.1:8788"
      }
    }
  }
}
```

### 4. CLI 常用命令

```bash
npu-top serve                    # 启动本地 Web 监控台与后台采集服务（默认 127.0.0.1:8788）
npu-top mcp                      # 启动 stdio MCP Server
npu-top servers                  # 列出所有受监控服务器概览
npu-top npu 192.0.2.21           # 查看指定主机 NPU 忙闲、AICore、HBM 与占用进程（缓存）
npu-top npu 192.0.2.21 --live    # 触发实时采样并查看 NPU 状态
npu-top status 192.0.2.21        # 查看主机完整状态（NPU、CPU、内存、磁盘、Docker）
npu-top mounts 192.0.2.21        # 查看挂载点与模型权重盘容量
npu-top capacity --min-idle 4    # 筛选观测到至少有 4 张空闲 NPU 的候选主机
```

---

## 配置参考

所有外部输入均通过环境变量显式配置，完整模板见 [`.env.example`](./.env.example)：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NFM_STATE_DIR` | `./data` | 运行时私有状态目录（存放 SQLite、专用密钥、`known_hosts` 与 SSH 控制套接字） |
| `NFM_INVENTORY_FILES` | 未设置 | JSON 主机清单文件路径（多路径用系统路径分隔符连接），作为活动主机导入 |
| `NFM_HOST_POOL_FILES` | 未设置 | 纯文本主机池文件路径（每行取首字段）；仅出现在主机池的机器自动标记 `低优先级` |
| `NFM_BOOTSTRAP_COMMAND` | 未设置 | 可选外部密钥引导命令模板（占位符 `{host} {port} {user} {public_key_file} {python}`，密码经 `stdin` 传入） |
| `NFM_HBM_BUSY_THRESHOLD_MB` | `8192` | HBM 判忙兜底阈值（MB），避开 A3 设备约 6 GB 的驱动常驻占用 |
| `NFM_SQLITE_MAX_MB` | `1024` | SQLite 主数据库最大容量上限（MB，最小 16） |
| `NFM_RETENTION_DAYS` | `90` | 历史摘要数据保留天数 |
| `MINDIE_LOG_LEVEL` | `INFO` | 结构化诊断日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| `MINDIE_DIAGNOSTICS_ROOT` | 平台用户状态目录 | 本地结构化诊断日志目录 |

---

## 有界历史存储与可观测性

- **有界历史数据库**：历史表存储 15 分钟粒度的主机与设备摘要，通过 SQLite `max_page_count` 将主库限制在 `NFM_SQLITE_MAX_MB`（默认 1024 MB）以内，并在容量达到 85% 时自动按天淘汰最旧摘要。升级旧版数据库前请先停止服务并运行迁移脚本：
  ```bash
  python3 scripts/migrate-history.py data/monitor.sqlite3 --max-mb 1024
  ```
- **结构化诊断与健康度**：采集线程异常时 `/api/health` 显式返回 HTTP `503` 与 `degraded` 状态，绝不伪造成功采样。支持通过 `npu-top diagnostics bundle --root PATH --output support.json` 离线导出严格脱敏的诊断包。

---

## 文档导航

| 文档 | 中文 | English |
| --- | --- | --- |
| 文档中心索引 | [docs/README.md](./docs/README.md) | [docs/README.en.md](./docs/README.en.md) |
| 系统架构与采集设计 | [docs/architecture.zh.md](./docs/architecture.zh.md) | [docs/architecture.md](./docs/architecture.md) |
| Agent CLI 与 MCP 接口规范 | [docs/agent-access.md](./docs/agent-access.md) | [docs/agent-access.en.md](./docs/agent-access.en.md) |
| Ascend 950 (A5) 探测解析规范 | [docs/a5-probing.zh.md](./docs/a5-probing.zh.md) | [docs/a5-probing.md](./docs/a5-probing.md) |
| 内置 Agent Skill 说明 | [.agents/skills/npu-top/SKILL.md](./.agents/skills/npu-top/SKILL.md) | [.agents/skills/npu-top/SKILL.md](./.agents/skills/npu-top/SKILL.md) |

---

## 仓库目录结构

```text
npu-top/
├── npu_top/         # Python 后端包（HTTP API、自适应采集调度、npu-smi 解析、SSH、CLI、MCP）
├── app/             # 前端源码（Vite 静态单页应用）
├── scripts/         # Wheel 构建脚本（build_wheel.sh）与历史库迁移脚本（migrate-history.py）
├── docs/            # 中英双语架构、Agent 接口与 A5 解析文档
├── .agents/skills/  # 随仓库分发的 Agent Skill
└── tests/           # 单元测试套件與 npu-smi 输出回归固件
```

---

## 开发与测试

```bash
python3 -m pip install .
python3 -m unittest discover -s tests -v
npm ci && npm run lint && npm run build
```

---

## 致谢与许可证

产品交互设计参考了 [RackTop](https://github.com/Tongzh-SEU/RackTop) 的多服务器资源总览、空闲算力发现与历史热力图思路；全部实现代码均为独立编写，并针对昇腾设备与无代理宿主机探测进行了专门设计。

本项目基于 [MIT License](./LICENSE) 开源。
