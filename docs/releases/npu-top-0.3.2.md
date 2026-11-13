# npu-top 0.3.2

[English](npu-top-0.3.2.en.md)

## 遥测覆盖率诚实性、本地状态边界与 DCMI 超时预算对齐

本版本围绕工程设计原则专项审计（[`mindie-agent#332`](https://github.com/mindie-agent/mindie-agent/issues/332)）收敛遥测覆盖率计算、本地 SQLite 注册异常分类与 DCMI 远端探测超时算术，并接入受保护主分支发布门禁：

- **损坏或非法 `cards_json` 不再伪造全卡覆盖率（[#34](https://github.com/mindie-agent/npu-top/pull/34)）**：
  - `telemetry_window` 严格校验 `cards_json` 解析结果与整数板卡 ID 列表，解析失败（`cards_json_unparseable`）或非整数列表（`cards_json_invalid_filter`）的采集运行直接从计划采样覆盖率中排除，禁止兜底匹配全部板卡；存在不可用运行时将 `coverage` 保持为 `null`（`coverage_basis="cards_json_unparseable"`），并在 `measurement_limitations` 中列明对应 `run_id`。
- **本地注册与未执行连接错误不再误标 `unknown`（[#34](https://github.com/mindie-agent/npu-top/pull/34)）**：
  - `apply_inventory_exception` 显式透传 `TimeoutError`（HTTP 批量导入统一映射为 `503 storage_timeout`），将本地 SQLite 注册异常记为 `registration="failed"`、未产生不确定远端副作用的 `connect()` 异常记为 `auth.state="not_ready"`，将 `"unknown"` 严格保留给 `submission_state == "uncertain"` 的不可逆远端写操作。
- **DCMI 探测外层 SSH 超时满足算术不变量（[#34](https://github.com/mindie-agent/npu-top/pull/34)）**：
  - `probe` 的可选 DCMI 探测统一采用 `inner (5s) + kill_after (1s) + cleanup_grace (3s) = 9s` 外层 SSH 超时，当用户配置的 SSH 超时低于 `9s` 时直接返回 `ConfiguredTimeoutTooSmall`。
- **受保护发布流水线加固（[#35](https://github.com/mindie-agent/npu-top/pull/35)、[#36](https://github.com/mindie-agent/npu-top/pull/36)）**：
  - 接入受保护 `main` 分支发布策略校验、旧客户端真实升级验收与 `MINDIE_RELEASE_DEPLOY_KEY` 显式传递。

本版本的实际测试与基准结果以随包 `benchmark-results.json`、`benchmark-results.md` 及对应 CI 记录为准。
