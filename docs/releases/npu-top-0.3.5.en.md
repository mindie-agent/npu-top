# npu-top 0.3.5: Owned Default State Directory, `< 70 MiB` Streaming 90-Day Telemetry Windows, and Shared Probe Deadlines

[中文](npu-top-0.3.5.md)

`npu-top v0.3.5` delivers clean-architecture state-service improvements and advances its pins to `mindie-diagnostics v0.5.16` and `remote-dev v0.11.13`:

- **Owned Default State Root & Read-Only SQLite Absence Safety**: Normalizes owned state directory resolution with safe `Path.home()` fallback under stripped environments, configures SQLite WAL + `busy_timeout=5000` once in `open_database`, and ensures read-only queries never materialize empty database files when absent.
- **`< 70 MiB` Streaming 90-Day Telemetry Aggregation & Shared Probe Deadlines**: Streams 90-day utilization history through bounded cursor buckets (`< 70 MiB` peak RSS) and bounds multi-host SSH/DCMI polling under shared `Deadline` budgets.
- **Dependency Pins & `state-service` Governance**: Bumps `pyproject.toml`, `package.json`, and `package-lock.json` to `0.3.5`, pins `mindie-diagnostics v0.5.16` and `remote-dev v0.11.13`, and enforces `--profile state-service` PR governance.
