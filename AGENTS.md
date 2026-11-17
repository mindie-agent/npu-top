# NPU Top (`npu-top`)

Local Ascend NPU fleet monitor, loopback HTTP console, compact CLI, and read-only MCP observation server (`device-tool` governance profile).

## 1. Domain Boundary & Core Operating Rules

- **Observation-Only Contract**: Every HTTP response (`X-MindIE-Top-Contract: observation-only`), CLI output, and MCP payload (`observation.allocation_authority = False`) reports observed host state at `observed_at`. `npu-top` never allocates or reserves NPU devices; physical NPU/port leases belong exclusively to `mindie-coordinator` (`HostQueue`).
- **Zero Arbitrary CWD Pollution**: Unconfigured installs default to an owned MindIE/XDG state directory (`MINDIE_DATA_HOME/npu-top` -> `XDG_DATA_HOME/mindie-agent/npu-top` -> `LOCALAPPDATA/mindie-agent/npu-top` -> `~/.local/share/mindie-agent/npu-top`). `./data` is adopted in place ONLY when a read-only SQLite check (`?mode=ro`) confirms an existing recognized `npu-top` database (`monitor.sqlite3` / `npu_top.sqlite3`)—never polluting arbitrary working directories or moving unrelated `./data` folders.
- **Zero Pre-Call Session-Binding Gates (`P2.7`)**: All `npu-top` MCP tools (`npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `list_npu_servers`) are deterministic single-user observation tools and work directly on standard MCP `tools/call` without session nonces or hook state.
- **State & Receipt Honesty**:
  - Storage failures in `AdaptiveScheduler` preserve completed in-memory probe snapshots (`status="online"`, `recording.state="failed"`, `receipt_unconfirmed=True`, `receipt_warning=...`) without advancing `_latest_persisted`.
  - SSH credentials and key installation belong to `remote-dev` (`~/.mindie/ssh/credentials.json`); `npu-top` stores only `auth_ref`.
- **Bounded Memory (`<= 512 MiB`) & Shared Monotonic Deadlines**:
  - `Database.telemetry_window` + `telemetry_window.build_window` stream raw SQLite rows via `cursor.fetchmany(2048)` into single-pass compact accumulators (`<= 512 MiB` peak RSS even on 90-day windows), never `.fetchall()` materializing full windows.
  - `HostProbe.collect` enforces one shared monotonic deadline across base+infra, frequency (`-t common`), DCMI, and `/proc` passes (`outer_timeout >= sum(inner_budgets) + grace`). Optional sub-probes whose minimum budget exceeds remaining deadline report `state="skipped", reason="collection_deadline_exhausted"` while preserving base observations.

## 2. Module Map (`npu_top/`, all modules `< 800` lines)

- `cli.py` & `client.py`: CLI entry point (`serve`, `mcp`, `diagnostics`, `servers`, `npu`, `status`, `mounts`, `capacity`, `telemetry-window`), `NpuTopClient` (`VawsTopClient` compatibility alias), and compact text formatters.
- `mcp.py`: Stdio JSON-RPC 2.0 MCP server exposing read-only fleet observation tools.
- `serve.py` & `api.py`: Daemon lifecycle, background inventory import, and loopback `ThreadingHTTPServer` routes.
- `scheduler.py`: `AdaptiveScheduler` managing viewer-lease polling intervals (`1, 5, 10, 30s` active vs `idle_interval`) and bounded `collect_and_wait` live probes.
- `probe.py` & `probe_parsers.py`: Shared-deadline SSH `HostProbe` orchestrator and pure `/proc`/`df`/`findmnt`/Docker text parsers.
- `npu_smi.py` & `npu_single_id.py`: Parsers for Ascend A2 (910B single-die), A3 (910C dual-die with `Phy-ID 0..15`), 310P, and A5 (Ascend950 single-ID) `npu-smi` outputs.
- `dcmi_telemetry.py`, `dcmi_metrics.py`, `dcmi_store.py`, `telemetry_extract.py`, `telemetry_window.py`: Remote stdlib DCMI getter, raw sample schema, and streaming task-window quantile/coverage aggregator.
- `db.py`, `settings.py`, `agent_view.py`, `ssh_access.py`, `device_adapter.py`, `inventory.py`, `inventory_bridge.py`: WAL SQLite store (`monitor.sqlite3`), `MINDIE_TOP_*` / legacy `NFM_*` settings, observation projections, and `remote-dev` / `coordinator` bridges.

## 3. Verification & Pre-PR Gate (`device-tool`)

Run fast local tests (Tier 0 Golden Captures & Tier 1 Local HTTP/SQLite/Scheduler):

```bash
python3 -m pytest -q -m "not live_npu"
npm test
python3 .github/scripts/check_lf_eol.py
```

Run standing live A2/A3 Ascend NPU probe tests (Tier 2, read-only SSH probes against A2 `root@173.125.1.2` and A3 `root@192.168.13.165` / `.154` — **never connect to `.153`**):

```bash
python3 -m pytest -q -m live_npu
```

Complete `.github/pull_request_template.md` (`DEV-ZERO-CTX`, `DEV-READONLY-PATH`, `DEV-TOPOLOGY`, `DEV-BOUNDED-MEM`) and verify via `python3 .github/scripts/check_pr_checklist.py --profile device-tool`.
