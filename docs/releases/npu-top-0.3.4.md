# npu-top 0.3.4

[English](npu-top-0.3.4.en.md)

## Windows CP936/长路径兼容、WSL2 前端构建防护与共享 SSH 底座升级

- **Wheel 构建钩子原生 `npm` 解析与无弹窗执行（`hatch_build.py`）**：Windows 下优先选用 `npm.cmd` 并附加 `CREATE_NO_WINDOW` 与 `stdin=DEVNULL`；POSIX/WSL2 下通过严格符号链接解析、`e_lfanew` 全偏移 PE 头检测与 `/proc/self/mountinfo` DrvFS 识别跳过 `/mnt/c` Windows `npm` 影子可执行文件，并在 `OSError` 边界前透传 `TimeoutError`。
- **CLI / MCP 标准流 UTF-8 自适应与 Windows 超长 SQLite 路径（`npu_top/cli.py`, `npu_top/mcp.py`, `npu_top/db.py`）**：在 `CP936` / `CP1252` 等非 UTF-8 控制台或重定向管道上自动将非空标准流重配置为 UTF-8，并为 `>= 248` 字符的 Windows 数据库路径自动补齐 `\\?\` / `\\?\UNC\` 扩展前缀。
- **底座组件正式版升级（`pyproject.toml`, `.github/workflows/ci.yml`）**：升级 `mindie-diagnostics` 至 `v0.5.12`（`3d82313bb4b826bf52d5ec1dd64ef6476a070b4b`）、`remote-dev` 至 `v0.11.11`（`4f362d50ee54f169493c7e91b3dca82a5f94c7e7`），并在 Windows CI 中隔离 `USERPROFILE`。
