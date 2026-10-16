# Agent CLI & MCP Tools Guide

[中文](./agent-access.md) · **English** · [Back to Documentation Index](./README.en.md)

To let AI coding agents (Claude Code, Codex, Kimi Code, Cursor, etc.) inspect cluster state safely, `npu-top` routes all agent queries through your local `npu-top serve` process:

- **Instant `cache` Mode (Default)**: Returns the latest in-memory snapshot in milliseconds without opening a new SSH connection;
- **On-Demand `live` Mode**: Asks the collector to run a fresh SSH probe and returns the updated snapshot;
- **Zero Credential Exposure**: Agents talk only to `127.0.0.1:8788` — they never build raw SSH commands or touch remote passwords or monitoring keys.

---

## 1. Understanding the `observation` Envelope

Because `npu-top` is a **read-only traffic monitor** rather than a resource scheduler, every `/api/agent/*` response, CLI `--json` output, and MCP `structuredContent` payload includes an `observation` envelope:

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

- `age_seconds: 4` tells you the snapshot is 4 seconds old;
- `allocation_authority: false` reminds callers that seeing `idle_npu_count: 4` means "4 cards looked idle 4 seconds ago," not that those cards are reserved for you. Always request actual device leases through `mindie-coordinator`.

---

## 2. Quick CLI Cheat Sheet

```bash
# List all monitored servers
npu-top servers

# View per-card busy/idle state, AICore, HBM, and process owners from cache
npu-top npu 192.0.2.21

# Trigger an immediate live SSH probe for fresh NPU state
npu-top npu 192.0.2.21 --live

# Ultra-compact output for tight context windows
npu-top npu 192.0.2.21 --ultra-compact

# Structured JSON output with process PIDs, working directories, and command lines
npu-top --json npu 192.0.2.21 --processes
npu-top --json npu 192.0.2.21 --process-details

# Full server status (NPU + CPU + RAM + disks + Docker containers; defaults to live probe)
npu-top status 192.0.2.21
npu-top status 192.0.2.21 --cache

# Inspect disk mounts and candidate model-weight folders
npu-top mounts 192.0.2.21 --live

# Find A3 servers observed within the last 180s with at least 4 idle NPUs
npu-top capacity --min-idle 4 --max-age 180 --tag A3
```

Default text output is concise and token-efficient:

```text
192.0.2.21 online age=4s npu=8 busy=2 util=24.5% hbm=96.0G/512.0G
0 busy util=91% hbm=41.2G/64.0G proc=1 owner=x01234567,xyz
1 idle util=0% hbm=5.9G/64.0G proc=0 owner=-
```

**Helpful Flags**:
- Pass `--max-age 180` to enforce cache freshness; if the snapshot is older than 180 seconds, `npu-top` still prints the snapshot but exits with code `3`.
- The default API URL is `http://127.0.0.1:8788` (configurable via `--url` or `MINDIE_TOP_URL`; non-loopback URLs are rejected by default for safety).

---

## 3. MCP Tools for AI Coding Agents

Register `npu-top` in `.mcp.json`:

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

This exposes 5 read-only MCP tools:

| MCP Tool | Arguments | When to Use It |
| --- | --- | --- |
| `list_npu_servers()` | *(none)* | Get a quick overview of all monitored servers and their idle/busy card counts |
| `find_npu_capacity(...)` | `min_idle_npus?`, `max_age_seconds?`, `tags?`, `include_disabled?` | Shortlist candidate servers with enough idle NPUs and matching tags (low-priority hosts are ranked last) |
| `npu_status(...)` | `host`, `mode?`, `include_processes?`, `process_details?`, `max_age_seconds?` | Inspect per-card AICore utilization, HBM usage, and owning processes on a host |
| `server_status(...)` | `host`, `mode?`, `process_details?`, `timeout_seconds?` | Inspect full host health: NPUs, CPU, RAM, disks, Docker containers, and process owners |
| `list_mounts(...)` | `host`, `mode?`, `timeout_seconds?` | Check filesystem mounts and free capacity to find where model weights live |

> **Recommended Workflow**: Use `mode="cache"` for fast initial screening, run `mode="live"` on your chosen candidate for a final pre-flight check, and then submit your workload through `mindie-coordinator`.

---

## 4. Loopback HTTP Endpoints

Custom local scripts can also query the read-only loopback endpoints directly:

```text
GET /api/agent/servers
GET /api/agent/npu?host=192.0.2.21&mode=cache
GET /api/agent/npu?host=192.0.2.21&mode=live&processes=1&details=1
GET /api/agent/server?host=192.0.2.21&mode=cache
GET /api/agent/capacity?min_idle_npus=4&max_age_seconds=180&tags=A3
```

`host` matches the server IP, display name, remote `hostname`, or internal ID (if multiple SSH endpoints share the same IP, an explicit ambiguity error is returned instead of guessing). *Note: `192.0.2.x` addresses above are RFC 5737 documentation examples.*
