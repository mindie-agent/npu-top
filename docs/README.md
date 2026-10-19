# `npu-top` 文档中心

**中文** · [English](./README.en.md) · [返回项目主页](../README.md)

这里汇总了 `npu-top` 的内部采集原理、AI 编程助手查卡接口（CLI 与 MCP），以及昇腾各代芯片（含 Ascend 950 A5）的输出解析说明。

## 文档导航

| 文档主题 | 中文 | English | 你能在这里查到什么 |
| --- | --- | --- | --- |
| 系统架构、多级采样与判忙规则 | [architecture.zh.md](./architecture.zh.md) | [architecture.md](./architecture.md) | 怎么通过 SSH 免安装采数据？怎么判断一张 NPU 卡到底忙不忙？为什么说它“只管看不管分”？ |
| Agent CLI 与 MCP 查卡工具详解 | [agent-access.md](./agent-access.md) | [agent-access.en.md](./agent-access.en.md) | AI 助手怎么用 5 个 MCP 工具或 CLI 命令秒查空闲机器、看进程归属和找大盘挂载点？ |
| Ascend 950 (A5) 探测与解析说明 | [a5-probing.zh.md](./a5-probing.zh.md) | [a5-probing.md](./a5-probing.md) | 新一代 Ascend 950 (A5) 的四列 `npu-smi info` 表格是怎么解析和做回归测试的？ |
