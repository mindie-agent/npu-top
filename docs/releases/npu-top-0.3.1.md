# npu-top 0.3.1

[English](npu-top-0.3.1.en.md)

## 多端宿主插件兼容对齐、LF 门禁与共享 SSH 依赖升级

- **依赖基线升级**：将 `remote-dev` 固定依赖升级至正式版本 `0.11.3`（`ba62b643af4bc8265c8206a32a1fe21733d9d119`），保持 `mindie-diagnostics` `0.5.4`（`5ae643e19223f2181ecdbaa3edaf464137c4805e`），完整复用共享用户私有 `~/.mindie/ssh/credentials.json` 与 `id_ed25519` 凭据存储。
- **多端宿主文档与 6 项只读 MCP 工具契约对齐**：在 `README.md` 与 `README.en.md` 中对齐 6 项只读 MCP 工具说明，新增 Google Antigravity（`mindie-agent-agy`）宿主插件支持说明，并更新统一家族品牌视觉资产（`assets/brand/`）。
- **工程门禁强化**：新增 `.editorconfig`、`.gitattributes` 与 `.github/scripts/check_lf_eol.py`，并在 CI 流水线（`pr-checklist.yml`）中强制校验 Unix LF（`\n`）换行符与 PR 自查清单完整性。
