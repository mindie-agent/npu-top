# npu-top 0.3.5：专属状态目录收敛、90d 流式低内存遥测窗口与共享探测预算治理

[English](npu-top-0.3.5.en.md)

`npu-top v0.3.5` 完成状态服务洁净架构收敛并升级锁定 `mindie-diagnostics v0.5.16` 与 `remote-dev v0.11.13`：

- **受管默认状态目录与空环境变量回退安全**：统一默认状态存储路径与 `Path.home()` 空环境变量回退逻辑，配置 SQLite 单次打开 WAL + `busy_timeout=5000`，彻底消除只读查询路径在库文件不存在时意外创建空数据库的问题。
- **90 天历史热力图流式窗口（`<= 70 MiB`）与共享探测截止时间**：将长周期利用率窗口聚合重构为 SQLite 游标流式分桶（内存峰值 `< 70 MiB`），并为多节点 SSH/DCMI 采集引入统一共享 `Deadline` 预算与优雅降级回执。
- **底座正式版升级与 `state-service` 门禁对齐**：同步 `pyproject.toml`、`package.json` 与 `package-lock.json` 版本至 `0.3.5`，升级 `mindie-diagnostics` 至 `v0.5.16`、`remote-dev` 至 `v0.11.13`，并启用 `--profile state-service` PR 门禁。
