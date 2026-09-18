---
name: npu-top
description: Query observed NPU fleet state through the local npu-top CLI/MCP, including NPU/HBM, CPU, memory, Docker containers, process ownership, and mounts. Use when an agent needs to inspect what a monitored Ascend host currently looks like. Do not use to decide device allocation, launch workloads, reserve NPUs, or manage remote containers; device assignment comes from the host-side NPU coordinator queue.
---

# npu-top

Use the running local monitor as the single *observation* interface for fleet status.

## Boundaries

- npu-top is observation-only. Every result is host state at `observation.observed_at`; `allocation_authority` is always `false`. It is not the source of truth for which NPUs may be used. Obtain devices from the host-side NPU coordinator queue and treat npu-top as a way to sanity-check what that queue told you, never as a substitute.
- Prefer npu-top over ad hoc SSH for fleet status. Its service owns remote probing, credentials, connection reuse, and snapshot collection.
- Fleet discovery, `capacity`, `npu`, and `mounts` default to cache and never trigger SSH.
- CLI `status` defaults to live: it requests one centralized probe and waits for a new snapshot. Add `--cache` when a cached full-server view is sufficient.
- The web/API service remains on `127.0.0.1` and has no login. Do not expose it through a public listener, reverse proxy, or port forward.
- Do not print or inspect `data/keys`, passwords, or `known_hosts` contents.

## Inspection flow

1. Shortlist hosts that were recently observed with idle NPUs:

   ```bash
   npu-top capacity --min-idle 4 --max-age 180 --tag A3
   ```

   `idle_npu_count` is an observation that can change before anything starts. It is not a reservation.

2. Inspect a candidate from cache:

   ```bash
   npu-top status 192.0.2.21 --cache
   ```

   The compact result includes NPU/HBM, CPU, memory, disk pressure, Docker count, grouped NPU processes, containers, extracted employee IDs or initials, and likely model-weight mounts.

3. Inspect storage when model placement matters:

   ```bash
   npu-top mounts 192.0.2.21
   ```

   Default text hides pseudo and container-overlay filesystems. JSON/MCP structured results retain the full mount list. `weight_candidate` is a heuristic; it does not recursively scan the remote filesystem.

4. Immediately before asking the coordinator for devices on a host, refresh only that host so the observation is current:

   ```bash
   npu-top status 192.0.2.21 --timeout 30
   ```

Do not follow a successful live result with a duplicate raw SSH occupancy query. Do not turn a fresh observation into an assignment yourself; hand the host to the coordinator workflow.

## CLI routing

```bash
npu-top servers
npu-top npu 192.0.2.21
npu-top npu 192.0.2.21 --ultra-compact
npu-top --json npu 192.0.2.21 --processes
npu-top --json npu 192.0.2.21 --process-details
```

- Add top-level `--json` for stable machine-readable output; it includes the `observation` envelope.
- `status HOST` is live by default. Use `status HOST --cache` to avoid a new probe; `--live` remains an explicit alias for the default.
- `--processes` includes compact PID, container, NPU memory, and ownership records.
- `--process-details` additionally includes pwd, command, executable, and user. Keep those details out of the default response unless they help answer the request.
- `npu --max-age N` prints the snapshot but exits `3` when it is missing or stale; it does not refresh implicitly.
- A lookup is an exact match on IP, display name, remote hostname, or server id. Ambiguous endpoints fail instead of guessing.

## MCP routing

```text
npu-top mcp
```

Set `MINDIE_TOP_URL=http://127.0.0.1:8788` only when the default is unsuitable. Route tool calls as follows:

- `find_npu_capacity`: shortlist hosts by observed idle NPUs, snapshot age, and tags; low-priority hosts sort last.
- `npu_status`: compact per-device utilization/HBM; supports `mode=cache|live`.
- `server_status`: full observed view with system, storage, Docker, processes, containers, and owners.
- `list_mounts`: mounted storage and likely weight locations.
- `list_npu_servers`: compact discovery/status list.

Tool text is concise; prefer `structuredContent` for downstream reasoning and read its `observation` block. `mode=live` is still read-only to the caller but causes the monitor collector to contact the selected host.

## Service operation

Start the local single-process console with `npu-top serve` (default loopback). Preserve the ignored state directory (`data/` or `NFM_STATE_DIR`) across upgrades. Host sources and password bootstrap are configured explicitly through environment variables (see `.env.example`); the service never searches the filesystem for another project.

Read [references/acceptance.md](references/acceptance.md) when starting the console, changing the Agent interface, or diagnosing health. User-facing CLI/API examples are in [docs/agent-access.md](../../../docs/agent-access.md).

## Result

Report the query mode (`cache` or requested `live`), `observed_at`/age, decisive occupancy facts, and any relevant container, owner, or mount. State explicitly that the numbers are observations and that device assignment still has to come from the coordinator. Keep the default response compact and mention staleness or failed live collection explicitly.
