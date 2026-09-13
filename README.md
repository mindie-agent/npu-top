# vaws-top — NPU Fleet Monitor

面向本机单用户的 Ascend NPU 监控台。它通过宿主机 SSH 无代理采集，重点展示 NPU 利用率、HBM、CPU、系统内存、磁盘、挂载点和 Docker 容器，并以 SQLite 保存历史数据。用 `uvx` 在本机拉起，只服务自己，不对外提供托管服务。

**vaws-top 只观测，不分配。** 它不是设备分配权威：哪些 NPU 可以被使用，由宿主机侧的 NPU 协调器队列决定。vaws-top 的所有 Agent 接口都把数据标注为"带观测时间戳的观测状态"，并明确声明不得据此做设备分配决策。详见 [docs/agent-access.md](docs/agent-access.md) 与 [docs/architecture.md](docs/architecture.md)。

## 诊断与故障状态

CLI、MCP、HTTP、后台采集及 inventory 初始化使用零依赖的 `vaws-diagnostics`。
该依赖固定为公开 canonical Git revision，独立 wheel/uvx 安装无需先准备 consumer
或 knowledge；共享包的 release wheel 可用于离线依赖包。
`VAWS_LOG_LEVEL` 控制 DEBUG/INFO/WARNING/ERROR，`VAWS_DIAGNOSTICS_ROOT` 可覆盖平台
用户状态目录。不同进程独立写 JSONL，每段 1 MiB、保留三个轮转备份；可选诊断 worker
按全局容量与时间保留清理旧进程日志。日志写失败保留原业务结果。

采集线程失败后 `/api/health` 返回 503 和 `degraded`，runtime 包含实际线程存活状态
和异常类型；等待新采样的调用立即得到不可用，不伪造成功。失败 probe 的耗时来自
实际工作线程内的计时，不再写零或把排队时间当作探测时间。不会自动重放 bootstrap。

`vaws-top diagnostics bundle --root PATH --output support.json` 仅离线导出严格字段
投影与脱敏结果，不扫描宿主机、不启动服务、不上传原始 inventory、密码或命令。
自动 issue 上报由独立启用的 reporter 负责；普通监控请求不等待网络上报。

## 安装与启动

唯一版本号来自 `pyproject.toml` 的 `0.1.6`。MCP `serverInfo.version` 读取 `importlib.metadata.version("vaws-top")`；`package.json` 的 version 与之相同。

### 推荐：GitHub Release wheel（无需本机 Node）

Release 资产里的 wheel 已打入前端构建产物。私有仓库下载需要已登录的 `gh` 或 `GITHUB_TOKEN`。

```bash
gh release download v0.1.6 -R vllm-ascend-workspace/vaws-top -p '*.whl'
uvx --from ./vaws_top-0.1.6-py3-none-any.whl vaws-top serve
```

或在已具备仓库读权限的环境里直接指向资产 URL：

```bash
uvx --from "https://github.com/vllm-ascend-workspace/vaws-top/releases/download/v0.1.6/vaws_top-0.1.6-py3-none-any.whl" vaws-top serve
```

浏览器访问 `http://127.0.0.1:8788`。`vaws-top serve` 单进程同时提供 HTTP API 与静态前端，默认只绑 loopback。`python -m vaws_top` 与 `vaws-top` 等价。

### 开发者路径：从 git 构建（需要 Node.js 22.13+）

`uvx --from git+…` 会在本机执行 hatch 构建。构建 hook 在缺少前端产物时会跑 `npm ci && npm run build`，没有 Node 会得到一个不含静态资源的 wheel，`vaws-top serve` 会立刻报错而不是 404。

```bash
uvx --from git+https://github.com/vllm-ascend-workspace/vaws-top@main vaws-top serve
```

源码树里也可以显式串起两步：

```bash
git clone https://github.com/vllm-ascend-workspace/vaws-top.git
cd vaws-top
./scripts/build_wheel.sh
```

### MCP

`.mcp.json`：

```json
{
  "mcpServers": {
    "vaws-top": {
      "command": "uvx",
      "args": [
        "--from",
        "https://github.com/vllm-ascend-workspace/vaws-top/releases/download/v0.1.6/vaws_top-0.1.6-py3-none-any.whl",
        "vaws-top",
        "mcp"
      ],
      "env": {
        "VAWS_TOP_URL": "http://127.0.0.1:8788"
      }
    }
  }
}
```

开发者可用 `git+https://github.com/vllm-ascend-workspace/vaws-top@<ref>` 替换 `--from` 的 wheel URL。五个工具见 [docs/agent-access.md](docs/agent-access.md)。

## 主要能力

- 批量添加服务器；先尝试项目专用密钥，再按顺序尝试本次请求中的密码候选。
- 可选地从显式配置的清单文件导入主机（`NFM_INVENTORY_FILES`、`NFM_HOST_POOL_FILES`）；只读取每条记录的主机端点字段。仅出现在主机池、未进入活动清单的设备自动获得"低优先级"标签。
- 密码只通过请求内存和标准输入传给可选的外部密钥引导命令（`NFM_BOOTSTRAP_COMMAND`），不写数据库、文件、命令参数或应用日志。
- 成功认证后自动安装专用 Ed25519 公钥；后续采集使用密钥和复用的 OpenSSH 控制连接。
- Host Key 使用独立 `known_hosts`，首次连接记录指纹，指纹变化时 OpenSSH 会拒绝连接。
- 在裸机宿主机运行 `npu-smi`，可跨容器观察占用；内置并经单元测试的解析器覆盖单 die（910B 类）与双 die（A3/910C 类）设备表、两种进程表布局和 `-t usages` 输出。
- 判忙优先进程与 AICore 利用率，HBM 作为兜底；默认 8 GB 阈值避开 A3 类设备约 6 GB 的驱动常驻占用，可通过 `NFM_HBM_BUSY_THRESHOLD_MB` 调整。
- 页面可选 1、5、10、30 秒实时刷新；多个页面同时打开时采用最快频率。
- 页面关闭或心跳过期后自动回到默认 120 秒巡检；磁盘、挂载和 Docker 默认 60 秒一次。
- 交互态高频结果保留在内存，SQLite 默认最短每 30 秒落一条，避免历史库随 1 秒刷新膨胀。
- 固定服务器侧栏汇总在线状态、NPU 数量、HBM 和 CPU；总览展开物理 die 展示逐 die HBM 占用（A3 为 8 张逻辑卡、16 个 die），AICore 非零时以右上角红色闪光点提示。点击服务器可按逻辑卡分组查看两个 die，以及逐卡 AICore/HBM、温度、功耗、进程、磁盘与 Docker 明细。
- 服务器标签可在管理页新增、编辑并通过侧栏搜索；派生的"低优先级"标签在后续同步中自动维护，低优先级服务器排在普通服务器之后。
- 历史报表覆盖 1 小时到 90 天，包含聚合趋势，以及按日期和 2 小时时段排列的 CPU、内存、NPU、HBM 与逐卡 AICore 热力图；原始数据默认保留 90 天。
- 默认只监听 `127.0.0.1`，不含登录功能，也不应直接暴露到外网。
- 为 Agent 提供统一 CLI/MCP：可按 IP/主机名选择缓存或实时探查，筛选观测到的空闲算力，并查询 NPU、CPU、内存、容器/进程归属及挂载盘；SSH 始终封装在常驻采集器内。结构化 JSON、`observation` 信封和完整参数见 [Agent CLI 与 MCP](docs/agent-access.md)。
- Agent 的完整使用与决策约定随仓库保存在 [`.agents/skills/vaws-top/SKILL.md`](.agents/skills/vaws-top/SKILL.md)。

## 依赖与目录

运行时需要 Python 3.11+、系统 OpenSSH 客户端和 `ssh-keygen`。后端只使用 Python 标准库，不依赖 `torch`/`torch_npu`。构建 wheel 或从 git 安装时才需要 Node.js 22.13+。

```text
vaws_top/       Python 包：HTTP API、采集调度、npu-smi 解析、SSH、CLI、MCP
vaws_top/static/  前端构建产物（gitignore，由 npm run build 写入 wheel）
app/            前端源码（Vite 静态 SPA）
tests/          标准库 unittest，针对安装后的 vaws_top 包
scripts/        build_wheel.sh：npm ci && npm run build && uv build
docs/           架构与 Agent 接口
.agents/skills/ 随仓库分发的 Agent Skill
```

运行时私有状态默认写在当前工作目录的 `data/`：SQLite、专用密钥、`known_hosts` 和 SSH 控制套接字。用 `NFM_STATE_DIR` 改位置。该目录已忽略，不会进入 Git。

## 配置

所有外部输入都通过环境变量显式给出，模板见 [`.env.example`](.env.example)。

| 变量 | 作用 |
|------|------|
| `NFM_INVENTORY_FILES` | JSON 主机清单（`{"machines": [{"alias", "host": {"ip", "port", "user", "machine_type"}}]}`），多个路径用平台路径分隔符连接；其中的主机作为活动主机导入 |
| `NFM_HOST_POOL_FILES` | 纯文本主机池，每行取第一个字段；仅出现在这里的主机带"低优先级"标签 |
| `NFM_BOOTSTRAP_COMMAND` | 可选外部命令模板，用一次性密码安装监控公钥；占位符 `{host} {port} {user} {public_key_file} {python}`，密码经 stdin 传入。未配置时只支持已有密钥登录 |
| `NFM_STATE_DIR` | 私有状态目录，默认当前目录下的 `data/` |
| 其余 `NFM_*` | 监听地址/端口、采集周期、保留天数、HBM 判忙阈值 |

自动导入只读取清单中的宿主机地址、端口、用户和机器类型；不会采用容器端口，也不会创建或修改容器。已有数据库记录优先，不会在每次启动时重命名用户维护的服务器。

## CLI

```bash
vaws-top serve
vaws-top serve --port 9876
vaws-top mcp
vaws-top npu 192.0.2.21
vaws-top servers
vaws-top status 192.0.2.21
vaws-top mounts 192.0.2.21
vaws-top capacity --min-idle 4
```

开发时分别跑前端热更新（Vite 把 `/api` 代理到本机 `vaws-top serve`）：

```bash
vaws-top serve --port 8788
npm ci
npm run dev
```

## 批量格式

界面中每行一台服务器：

```text
名称, 主机, SSH端口, SSH用户, 标签1|标签2
example-a3-01, 192.0.2.21, 22, root, A3|训练
```

密码候选一行一个。服务对每台尚未配置密钥的主机按顺序尝试，成功即停止；请求完成后不保留候选密码。

文中的 `192.0.2.x` 为 RFC 5737 文档保留地址，仅作示例。

## 与 vllm-ascend-workspace 的关系

本仓库从 `vllm-ascend-workspace` 脚手架拆出。脚手架侧如需拉起本机监控台，应安装本仓库的 wheel 或 `uvx` 调用 `vaws-top serve`，不要再部署成常驻多用户服务。本仓库本身不依赖脚手架的任何文件或目录布局。

## 测试与 CI

```bash
python3 -m pip install .
python3 -m unittest discover -s tests -v
npm ci && npm run lint && npm run build
```

GitHub Actions 在 Python 3.11/3.12 上对**已安装的包**跑后端测试，并跑前端 lint/build 以及带静态资源的 wheel 构建。打 `v*` tag 时把 wheel 上传为 GitHub Release 资产。

## 设计来源

产品交互参考了 [RackTop](https://github.com/Tongzh-SEU/RackTop) 的多服务器资源总览、空闲算力发现和历史热力图思路；实现代码为独立编写，并针对 Ascend 设备与无代理宿主机探测进行了适配。

## License

MIT，见 [LICENSE](LICENSE)。
