# npu-top 0.3.1

[中文](npu-top-0.3.1.md)

## Multi-Host Plugin Documentation Alignment, LF Hygiene Gate & Shared SSH Dependency Upgrade

- **Dependency Baseline Upgrade**: Advance the pinned `remote-dev` dependency to formal release `0.11.3` (`ba62b643af4bc8265c8206a32a1fe21733d9d119`) while preserving `mindie-diagnostics` `0.5.4` (`5ae643e19223f2181ecdbaa3edaf464137c4805e`) and shared user-private `~/.mindie/ssh/credentials.json` / `id_ed25519` authentication.
- **Multi-Host Plugin & 6 Read-Only MCP Tool Alignment**: Align the 6 read-only MCP tools in `README.md` and `README.en.md`, document Google Antigravity (`mindie-agent-agy`) host plugin support, and integrate unified MindIE Agent brand assets (`assets/brand/`).
- **CI & Format Hygiene Gate**: Add `.editorconfig`, `.gitattributes`, and `.github/scripts/check_lf_eol.py`, enforcing Unix LF (`\n`) line endings and Principle 18 Pre-PR checklist verification in `.github/workflows/pr-checklist.yml`.
