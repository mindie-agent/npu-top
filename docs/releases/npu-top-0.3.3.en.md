# npu-top 0.3.3

[中文](npu-top-0.3.3.md)

## Diagnostics 2.0 Entry-Point Observability & Source Attribution

- **Internal Fault Classification & Source-Root Attribution (`npu_top/observability.py`)**:
  - Upgrades `mindie-diagnostics` to formal release `v0.5.11` (`171623584aa812677df907518dcb55b06493241b`) and `remote-dev` to formal release `v0.11.10` (`3556df571450dab481a25e0157167f1c3727dc6c`).
  - Binds `npu_top` `source_root` and records confirmed internal exceptions with structured `reason_code` / `contract_name` across HTTP, CLI, MCP, and collector entry wrappers while excluding `ClientError`, `RemoteDevError`, and caller errors.
