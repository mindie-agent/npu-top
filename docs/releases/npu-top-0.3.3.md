# npu-top 0.3.3

[English](npu-top-0.3.3.en.md)

## Diagnostics 2.0 入口可观测性与源码归因升级

- **内部异常精准分流与源码根目录归因（`npu_top/observability.py`）**：
  - 升级 `mindie-diagnostics` 至正式版 `v0.5.11`（`171623584aa812677df907518dcb55b06493241b`）、`remote-dev` 至正式版 `v0.11.10`（`3556df571450dab481a25e0157167f1c3727dc6c`）。
  - 在 HTTP、CLI、MCP 与采集器入口包装器中绑定 `npu_top` 源码根目录并上报结构化内部异常（`reason_code`、`contract_name`），严格排除 `ClientError`、`RemoteDevError` 与调用方参数错误，防止重复记账与敏感现场外泄。
