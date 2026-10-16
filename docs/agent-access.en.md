# Agent CLI & MCP Access

[中文](./agent-access.md) · **English** · [Back to Documentation Index](./README.en.md)

`npu-top` routes all Agent queries through the local `npu-top serve` process. The default `cache` mode reads in-memory snapshots immediately without opening SSH connections; the explicit `live` mode asks the collector to perform a fresh probe and waits for the updated snapshot. Agents never construct raw SSH commands or touch remote passwords or monitoring keys.

## Observation-Only Contract

`npu-top` is an **observability surface**, not a device-allocation authority. Every response describes the host state observed at `observed_at` and may become stale at any moment. Which NPUs a workload may use is decided by the host-side NPU coordinator queue; `npu-top` neither tracks nor grants reservations or leases.

Every `/api/agent/*` response, CLI JSON payload, and MCP `structuredContent` therefore includes the same `observation` envelope:

```json
"observation": {
  "kind": "observed_state",
  "observed_at": 1756450000,
  "observed_at_iso": "2026-08-29T06:46:40Z",
  "age_seconds": 4,
  "allocation_authority": false,
  "notice": "Observed host state at observed_at. Not an allocation or reservation source; ..."
}
```

For `capacity` / `find_npu_capacity`, `kind` is `observed_availability` and `observed_at` is the oldest snapshot relied upon by the candidate list (each candidate also carries its own `observed_at`). Treat `idle_npu_count` and `busy` as "last observed idle/busy" and request actual device leases through the coordinator. HTTP responses also carry `X-MindIE-Top-Contract: observation-only` and `/api/health` returns `contract: observation-only`.

## CLI Usage

```bash
npu-top servers
npu-top npu 192.0.2.21
npu-top npu 192.0.2.21 --live
npu-top npu 192.0.2.21 --ultra-compact
npu-top --json npu 192.0.2.21 --processes
npu-top status 192.0.2.21
npu-top status 192.0.2.21 --cache
npu-top mounts 192.0.2.21 --live
npu-top capacity --min-idle 4 --max-age 180 --tag A3
```

Default human-readable output stays concise—reporting connectivity, cache age, busy/total cards, AICore utilization, HBM, process count, and owner:

```text
192.0.2.21 online age=4s npu=8 busy=2 util=24.5% hbm=96.0G/512.0G
0 busy util=91% hbm=41.2G/64.0G proc=1 owner=x01234567,xyz
1 idle util=0% hbm=5.9G/64.0G proc=0 owner=-
```

- Pass `--json` for structured output.
- Pass `--processes` for compact process records, or `--process-details` to include working directory and command line.
- Pass `--max-age 180` to bound acceptable cache staleness (exits with code `3` when exceeded while still printing the snapshot).
- `status` defaults to a live probe; pass `status HOST --cache` to read from cache.
- The default API endpoint is `http://127.0.0.1:8788` (configurable via `--url` or `MINDIE_TOP_URL`; non-loopback URLs are rejected by default).

## MCP Tools

The MCP server runs over stdio and queries the same loopback API:

```json
{
  "mcpServers": {
    "npu-top": {
      "command": "npu-top",
      "args": ["mcp"],
      "env": { "MINDIE_TOP_URL": "http://127.0.0.1:8788" }
    }
  }
}
```

Five read-only tools are exposed, each declaring the observation-only contract:

- `npu_status(host, mode?, include_processes?, process_details?, max_age_seconds?)`
- `server_status(host, mode?, process_details?, timeout_seconds?)`
- `list_mounts(host, mode?, timeout_seconds?)`
- `find_npu_capacity(min_idle_npus?, max_age_seconds?, tags?, include_disabled?)`
- `list_npu_servers()`

Use `mode=cache` for fast candidate screening and `mode=live` for a final pre-request check before asking the coordinator for devices.

## Read-Only Loopback HTTP API

```text
GET /api/agent/servers
GET /api/agent/npu?host=192.0.2.21&mode=cache
GET /api/agent/npu?host=192.0.2.21&mode=live&processes=1&details=1
GET /api/agent/server?host=192.0.2.21&mode=cache
GET /api/agent/capacity?min_idle_npus=4&max_age_seconds=180&tags=A3
```

`host` matches the server IP, display name, remote hostname, or internal ID. If multiple SSH endpoints share the same address, the server returns an explicit ambiguity error rather than guessing. (`192.0.2.x` addresses above are RFC 5737 documentation examples.)
