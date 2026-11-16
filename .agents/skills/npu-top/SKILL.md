---
name: npu-top
description: Query observed NPU fleet state through the local npu-top CLI/MCP, including NPU/HBM, CPU, memory, Docker containers, process ownership, and mounts. Use when an agent needs to inspect what a monitored Ascend host currently looks like. Do not use to decide device allocation, launch workloads, reserve NPUs, or manage remote containers; device assignment comes from the host-side NPU coordinator queue.
---

# npu-top

Use the running local monitor as a fast, read-only *observation* interface for Ascend fleet status.

## Boundaries

- `npu-top` is observation-only. Every result reflects host state at `observation.observed_at` with `allocation_authority = false`. Obtain physical device leases from `mindie-coordinator` (`HostQueue`); never treat an `npu-top` snapshot as a device reservation.
- Fleet discovery, `capacity`, `npu`, and `mounts` default to cached snapshots and trigger zero SSH connections.
- CLI `status` defaults to `live` (requesting one fresh probe); pass `--cache` when a recent cached view suffices.
- Because `mindie-coordinator` already reconciles live `npu-smi` occupancy when acquiring a lease, do **not** run a redundant live `npu-top status` refresh solely as a ceremony before calling `coordinator`.
- The web/API service binds strictly to loopback (`127.0.0.1` / `::1` / `localhost`) without authentication; never expose it publicly or inspect SSH secret files.

## Typical Inspection Flow

1. Shortlist hosts recently observed with idle NPUs:
   ```bash
   npu-top capacity --min-idle 4 --max-age 180 --tag A3
   ```
2. Inspect candidate host details or storage mounts from cache:
   ```bash
   npu-top status 192.0.2.21 --cache
   npu-top mounts 192.0.2.21
   ```
3. Request an actual NPU/port lease directly through `mindie-coordinator` (`lease --action acquire` or `mindie.run`) when ready to execute on devices.

## CLI & MCP Quick Reference

```bash
npu-top servers
npu-top npu 192.0.2.21 --ultra-compact
npu-top --json npu 192.0.2.21 --processes
```

- MCP (`npu-top mcp`, default `MINDIE_TOP_URL=http://127.0.0.1:8788`): exposes `find_npu_capacity`, `npu_status` (`mode=cache|live`), `server_status`, `list_mounts`, and `list_npu_servers`.
- Start the local console with `npu-top serve`. See [docs/agent-access.md](../../../docs/agent-access.md) for CLI/API details.
