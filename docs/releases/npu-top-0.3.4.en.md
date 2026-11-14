# npu-top 0.3.4

[中文](npu-top-0.3.4.md)

## Windows CP936 / Long-Path Hardening, WSL2 Wheel-Build Isolation & Shared SSH Upgrade

- **Native `npm` Resolution & Windowless Wheel Build Hook (`hatch_build.py`)**: Prefers `npm.cmd` with `CREATE_NO_WINDOW` and `stdin=DEVNULL` on Windows; skips WSL2 DrvFS (`drvfs`/`9p`) mounts and full-offset `e_lfanew` PE executables on POSIX so `/mnt/c` Windows `npm` shims never hijack wheel builds, while propagating `TimeoutError` across every `OSError` boundary.
- **UTF-8 CLI/MCP Stdio & Extended-Length Windows SQLite Paths (`npu_top/cli.py`, `npu_top/mcp.py`, `npu_top/db.py`)**: Reconfigures non-UTF-8 `TextIOWrapper` stdio streams to UTF-8 on `CP936` / `CP1252` hosts without breaking `StringIO` test doubles or `pythonw.exe` `None` streams, and adds `\\?\` / `\\?\UNC\` extended path prefixes for Windows SQLite paths `>= 248` characters.
- **Formal Runtime Dependency Upgrades (`pyproject.toml`, `.github/workflows/ci.yml`)**: Upgrades `mindie-diagnostics` to `v0.5.12` (`3d82313bb4b826bf52d5ec1dd64ef6476a070b4b`) and `remote-dev` to `v0.11.11` (`4f362d50ee54f169493c7e91b3dca82a5f94c7e7`), and isolates `USERPROFILE` alongside `HOME` on Windows CI.
