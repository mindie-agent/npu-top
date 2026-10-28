# NPU Top (`npu-top`)

Local Ascend NPU fleet monitor, loopback HTTP console, compact CLI, and read-only MCP observation server.

## 1. Domain Boundary & Core Principles

- **Observation-Only Contract**: Every HTTP response (`X-MindIE-Top-Contract: observation-only`), CLI output, and MCP payload (`observation.allocation_authority = False`) reports observed host state at `observed_at`. `npu-top` never allocates or reserves NPU devices; device leases belong exclusively to `mindie-coordinator`.
- **Loopback-Only Surface**: `AppServer` refuses non-loopback bind addresses (`127.0.0.1`, `::1`, `localhost`), and `VawsTopClient` bypasses environment proxies (`ProxyHandler({})`) while refusing remote URLs unless `MINDIE_TOP_ALLOW_REMOTE=1`.
- **State Honesty & Idempotent Bootstrap**:
  - Storage failures in `AdaptiveScheduler` never erase completed probe snapshots or advance `_latest_persisted`.
  - SSH credentials and key installation belong to remote-dev. Its shared local secret file defaults to `~/.mindie/ssh/credentials.json`; do not introduce a monitor-owned store or OS keychain prompts. Store only `auth_ref`; an unresolved reference reports not-ready. Periodic probes disable remote key repair.

## 2. Module Map (`npu_top/`)

- `cli.py` & `client.py`: CLI entry point (`serve`, `mcp`, `diagnostics`, `servers`, `npu`, `status`, `mounts`, `capacity`), loopback HTTP client, and compact text formatters.
- `mcp.py`: Stdio JSON-RPC 2.0 MCP server exposing read-only fleet observation tools (`npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `list_npu_servers`).
- `serve.py` & `api.py`: Daemon lifecycle, background inventory import, and `ThreadingHTTPServer` REST/static routes.
- `scheduler.py`: `AdaptiveScheduler` managing viewer-lease polling intervals (`1, 5, 10, 30s` active vs `idle_interval`) and bounded `collect_and_wait` live probes.
- `probe.py`: `HostProbe` executing single-roundtrip SSH shell probes (`/proc`, `npu-smi`, `df`, `findmnt`, `docker`, process details) and parsing host/container/owner metadata.
- `npu_smi.py` & `npu_single_id.py`: Bundled parsers for Ascend A2 (910B single-die), A3 (910C dual-die with `Phy-ID`), 310P, and A5 (Ascend950 single-ID) `npu-smi info` and `-t usages` outputs.
- `ssh_access.py`, `device_adapter.py`, `inventory.py`: remote-dev endpoint/authentication adapter and explicit `MachineInventoryFile` / `HostPoolFile` loaders.
- `db.py`: Thread-local WAL SQLite store (`monitor.sqlite3`) for server registry/auth references and 15-minute host/device metric rollups with hard capacity pruning.
- `agent_view.py`: Pure projection functions producing `observation`-enveloped payloads and ranking idle capacity candidates.
- `settings.py`, `observability.py`, `static_files.py`: `NFM_*` environment validation, `mindie_diagnostics` instrumentation, and frontend asset resolution.

## 3. Timeout Arithmetic & Concurrency Invariants

- **Outer >= Sum(Inner) + Grace**:
  - Remote `FAST_SCRIPT` runs `timeout 8 npu-smi info` and `timeout 8 npu-smi info -t usages`; default `NFM_SSH_TIMEOUT_SECONDS=20` (`>= 8 + 8 + 4s`).
  - When `include_infrastructure=True` (`timeout 10 docker stats`), `HostProbe.collect` expands outer timeout to `max(ssh_timeout + 12, 32)`.
  - `VawsTopClient` expands live HTTP request timeout to `timeout + 2s`.
- **SQLite Concurrency (`db.py`)**:
  - Configures `journal_mode=WAL`, `busy_timeout=5000`, and `max_page_count` once per thread-local connection; bounds concurrent history reads via `BoundedSemaphore(2)` and a `3s` SQLite progress-handler deadline.

## 4. Verification & Testing Pyramid

Run fast local tests (Tier 0 Golden Captures & Tier 1 Local HTTP/SQLite/Scheduler):

```bash
pytest -q -m "not live_npu"
```

Run standing live A2/A3 Ascend NPU probe tests (Tier 2, read-only SSH probes against A2 `root@173.125.1.2` and A3 `root@192.168.13.165` / `.154` — **never connect to `.153`**):

```bash
pytest -q -m live_npu
```
