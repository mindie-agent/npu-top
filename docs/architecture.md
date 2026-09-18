# Architecture

```text
Browser (127.0.0.1:8788)
  ├─ static dashboard + history + server onboarding
  └─ same-origin /api/*
        ↓ loopback only, single process
npu-top serve (HTTP API + AdaptiveScheduler)
  ├─ viewer leases: fastest of 1s / 5s / 10s / 30s
  ├─ idle fallback: 120s
  ├─ history write floor: 30s
  ├─ infrastructure cadence: 60s
  └─ bounded parallel host probes
        ↓ project Ed25519 + OpenSSH multiplexing
Bare-metal NPU hosts
  ├─ npu-smi info + usages
  ├─ /proc/stat, /proc/loadavg, /proc/meminfo
  ├─ df + findmnt
  └─ docker ps + docker info
```

## Host access and inventory

`DeviceAdapter` (`npu_top/device_adapter.py`) is the composition root for everything that touches a host. It is assembled from injected parts and holds no knowledge of where the monitor is installed or which project manages the fleet:

- `SshAccess` owns the monitor's dedicated Ed25519 key, its isolated `known_hosts`, and the OpenSSH options used for probes.
- `npu_smi` parses `npu-smi info` and `npu-smi info -t usages` output. It is bundled and unit-tested here; no external parser is imported at runtime.
- Inventory sources are explicit files supplied through configuration: `NFM_INVENTORY_FILES` (JSON `{"machines": [...]}` documents whose host endpoint fields are read) and `NFM_HOST_POOL_FILES` (plain host lists, first field only). Nothing is discovered by walking parent directories or Git common directories.
- `ExternalKeyBootstrap` runs the optional `NFM_BOOTSTRAP_COMMAND` template to install the monitor public key with a one-time password passed on stdin. Without it, password onboarding is unavailable and only key-based access works.

At startup, the configured sources are merged in order. Active inventory records provide aliases and hardware tags. Addresses found only in a host pool are still inserted and probed, but receive the derived `低优先级` tag. Adjacent authentication columns in pool files never enter application state. User-defined tags are stored in the existing `servers.tags_json` column and the derived priority tag is reconciled on each service start.

Inventory says which hosts exist. It does not say which devices may be used; see "Observation-only contract" below.

## Observation-only contract

npu-top observes. It is not a device-allocation authority and must not be used to decide which NPUs a workload may take. Three places describe the same devices in a typical deployment: this monitor, the host-side NPU coordinator queue, and the machine inventory. Only the coordinator queue grants devices; the inventory says which hosts exist; the monitor reports what a host looked like when it was last probed.

The contract is made explicit in the interface rather than left to documentation:

- Every `/api/agent/*` payload carries an `observation` envelope (`agent_view.observation_envelope`) with `kind`, `observed_at`, `observed_at_iso`, `age_seconds`, `allocation_authority: false` and a human-readable `notice`. `compact_server` rows and capacity candidates carry their own `observed_at`.
- `/api/agent/capacity` uses `kind: observed_availability`; its `observed_at` is the oldest snapshot the list relied on, and its notice states that idle counts are not reservations.
- All `/api/` responses send `X-MindIE-Top-Contract: observation-only`; `/api/health` reports `contract: observation-only`.
- Every MCP tool description and the CLI help end with the same statement; `tests/test_agent_cli_mcp.py` fails if a tool drops it.

Fields that are inherently allocation-shaped and therefore deserve extra care in consumers: `busy` / `busy_npu_count` / `idle_npu_count` (derived from processes, AICore and an HBM threshold at probe time), the `capacity` endpoint and `find_npu_capacity` tool (a ranked shortlist), and the derived `低优先级` tag (inventory membership, not a policy grant). The monitor does not integrate with the coordinator and does not know about leases; it keeps these fields because they are useful observations, and labels them so they cannot be mistaken for grants.

## Collection tiers

Fast collection reads CPU counters, load, memory and NPU state. CPU percentage is calculated from consecutive `/proc/stat` counters, so the probe does not sleep remotely. Infrastructure collection is separately cached because Docker and mount inspection are more expensive.

An NPU is busy when the host process table reports an owner, AICore utilization exceeds 1%, or HBM exceeds the configurable fallback threshold. The default is 8192 MB because the observed A3 fleet carries roughly 6 GB of idle driver HBM per device; the fallback therefore catches opaque cross-container occupancy without classifying driver overhead as a workload.

Each cycle is non-overlapping: a new cycle is scheduled only after the previous one finishes. Host probes run in a bounded thread pool, and OpenSSH `ControlPersist` reuses authenticated connections. Its control socket uses a short path under the state directory so a deeply nested checkout cannot exceed the Unix-domain socket path limit. If a 1-second cycle cannot finish within one second, the system naturally runs at the achievable rate instead of creating a backlog.

On native Windows, the OpenSSH client does not use Unix-domain control sockets, so `ControlMaster`, `ControlPersist`, and `ControlPath` are omitted while all other host-key, identity, timeout, and keepalive controls remain active. The generated private key receives an explicit current-user-only Windows ACL.

## Persistence

SQLite runs in WAL mode. The primary history index is `(server_id, collected_at DESC)`, matching server-range reports, with a second time index for retention cleanup and fleet reports. Detailed snapshots are stored as JSON alongside summary columns used by aggregate queries. The heatmap endpoint groups summary columns and `payload_json.devices` with SQLite JSON functions into timezone-aligned two-hour buckets, so the UI can render both server-level and per-NPU activity without loading raw samples into the browser.

Passwords are deliberately absent from every schema. The API accepts them only for the duration of a batch request and passes one candidate at a time to the configured bootstrap command through stdin.
