# `npu-top` 文档中心

**中文** · [English](./README.en.md) · [返回项目主页](../README.md)

本目录包含 `npu-top` 的系统架构、只观测不分配契约、Agent CLI / MCP 接口以及昇腾设备解析规范。

## 文档列表

| 文档 | 中文 | English | 内容概述 |
| --- | --- | --- | --- |
| 系统架构与采集设计 | [architecture.zh.md](./architecture.zh.md) | [architecture.md](./architecture.md) | 宿主机接入与清单合并、Observation-only 契约、多级采样周期与 SQLite 持久化 |
| Agent CLI 与 MCP 接口规范 | [agent-access.md](./agent-access.md) | [agent-access.en.md](./agent-access.en.md) | `observation` 信封格式、CLI 子命令、5 个只读 MCP 工具与回环 HTTP API |
| Ascend 950 (A5) 探测解析规范 | [a5-probing.zh.md](./a5-probing.zh.md) | [a5-probing.md](./a5-probing.md) | Ascend 950 (A5) 四列 `npu-smi info` 表格解析与回归测试固件说明 |
