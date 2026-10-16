<h1 align="center">MindIE Agent NPU Top (<code>npu-top</code>)</h1>

<p align="center">
  <strong>Local single-user Ascend NPU observability dashboard — featuring agentless SSH collection, a real-time web UI, bounded 90-day history heatmaps, and read-only Agent CLI / MCP interfaces</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> · <strong>English</strong> · <a href="./docs/README.en.md">Documentation</a> · <a href="https://github.com/mindie-agent/mindie-agent">MindIE Agent Main Repo</a>
</p>

---

## Overview

`npu-top` is the local single-user Ascend NPU observability component of [MindIE Agent](https://github.com/mindie-agent/mindie-agent). It collects NPU utilization, HBM usage, CPU, system memory, disk capacity, mount points, and Docker containers across bare-metal hosts via agentless SSH, serving a loopback web dashboard, bounded SQLite history trends and heatmaps, and unified read-only CLI and MCP interfaces for coding agents.

> [!IMPORTANT]
> **`npu-top` is strictly observation-only (`allocation_authority: false`).**
> It is not a device-allocation authority: which NPUs a workload may use is decided exclusively by the host-side NPU coordinator queue ([`mindie-agent/coordinator`](https://github.com/mindie-agent/coordinator)). Every Agent API response carries a timestamped `observation` envelope explicitly stating that observed idle counts are not reservations or grants. See [Agent CLI & MCP Access](./docs/agent-access.en.md) and [Architecture](./docs/architecture.md).

---

## Key Features

- **Agentless Host SSH Probing**: Runs `npu-smi` on bare-metal hosts to observe device occupancy across containers; includes a unit-tested parser covering single-die (910B-class), dual-die (A3 / 910C-class), and Ascend 950 (A5) device tables, process layouts, and `-t usages` output.
- **Adaptive Multi-Tier Scheduling**: Supports 1s / 5s / 10s / 30s live refresh while the web UI is open and falls back to 120s background polling when idle; high-frequency interactive snapshots stay in memory while SQLite history writes are throttled to a 30s floor.
- **Bounded History Storage & Heatmaps**: Stores 15-minute host/device aggregate summaries for 1-hour to 90-day trend reports and 2-hour bucketed CPU, memory, NPU, HBM, and per-card AICore heatmaps, capped by `NFM_SQLITE_MAX_MB` (default 1024 MB).
- **Zero-Plaintext Key Onboarding**: Batch server onboarding passes password candidates in memory via `stdin` to install a dedicated Ed25519 public key—never persisting passwords to databases, files, CLI arguments, or logs.
- **Unified Agent CLI & MCP**: Exposes 5 read-only MCP tools and matching CLI commands supporting instant `cache` screening and on-demand `live` probes while keeping SSH credentials isolated inside the collector process.

---

## Quick Start

### Requirements

- **Runtime**: Python `>= 3.11`, system OpenSSH client (`ssh` and `ssh-keygen`). The backend uses only the Python standard library plus the pinned `mindie-diagnostics` package (no `torch` / `torch_npu` required).
- **Building from Git / Source**: Requires Node.js `>= 22.13` to build the frontend static assets.

### 1. Recommended: Run from GitHub Release Wheel (No Local Node.js Needed)

Release wheels come pre-packaged with the compiled frontend assets:

```bash
gh release download v0.1.6 -R mindie-agent/npu-top -p '*.whl'
uvx --from ./npu_top-0.1.6-py3-none-any.whl npu-top serve
```

Or run directly from the release asset URL:

```bash
uvx --from "https://github.com/mindie-agent/npu-top/releases/download/v0.1.6/npu_top-0.1.6-py3-none-any.whl" npu-top serve
```

Open `http://127.0.0.1:8788` in your browser. `npu-top serve` serves both the HTTP API and static web UI in a single process bound to `127.0.0.1` by default.

### 2. Developer Path: Build from Git or Source (Requires Node.js 22.13+)

```bash
# Build and launch directly from Git via uvx
uvx --from git+https://github.com/mindie-agent/npu-top@main npu-top serve

# Or clone and build the wheel locally
git clone https://github.com/mindie-agent/npu-top.git
cd npu-top
./scripts/build_wheel.sh
```

### 3. Configure the MCP Server

Add `npu-top` to `.mcp.json`:

```json
{
  "mcpServers": {
    "npu-top": {
      "command": "uvx",
      "args": [
        "--from",
        "https://github.com/mindie-agent/npu-top/releases/download/v0.1.6/npu_top-0.1.6-py3-none-any.whl",
        "npu-top",
        "mcp"
      ],
      "env": {
        "MINDIE_TOP_URL": "http://127.0.0.1:8788"
      }
    }
  }
}
```

### 4. Common CLI Commands

```bash
npu-top serve                    # Start local web dashboard & collector (default 127.0.0.1:8788)
npu-top mcp                      # Start the stdio MCP server
npu-top servers                  # List all monitored servers
npu-top npu 192.0.2.21           # Inspect NPU utilization, HBM, and owning processes (cached)
npu-top npu 192.0.2.21 --live    # Trigger a fresh probe and inspect NPU state
npu-top status 192.0.2.21        # Inspect full host status (NPU, CPU, memory, disk, Docker)
npu-top mounts 192.0.2.21        # Inspect mount points and model-weight storage capacity
npu-top capacity --min-idle 4    # Find candidate hosts observed with at least 4 idle NPUs
```

---

## Configuration Reference

All external inputs are configured explicitly via environment variables (see [`.env.example`](./.env.example)):

| Variable | Default | Purpose |
| --- | --- | --- |
| `NFM_STATE_DIR` | `./data` | Local state directory (SQLite DB, dedicated Ed25519 key, `known_hosts`, SSH control sockets) |
| `NFM_INVENTORY_FILES` | unset | Path-separated JSON machine inventory files imported as active hosts |
| `NFM_HOST_POOL_FILES` | unset | Plain-text host pool files (first column); hosts only in pool files receive the `低优先级` (low-priority) tag |
| `NFM_BOOTSTRAP_COMMAND` | unset | Optional external command template to install the monitor public key using a one-time password on `stdin` |
| `NFM_HBM_BUSY_THRESHOLD_MB` | `8192` | Fallback HBM busy threshold in MB (avoids classifying ~6 GB idle A3 driver overhead as busy) |
| `NFM_SQLITE_MAX_MB` | `1024` | Maximum SQLite main database size in MB (minimum 16) |
| `NFM_RETENTION_DAYS` | `90` | History summary retention period in days |
| `MINDIE_LOG_LEVEL` | `INFO` | Structured diagnostic log level (`DEBUG` / `INFO` / `WARNING` / `ERROR`) |
| `MINDIE_DIAGNOSTICS_ROOT` | Platform user state dir | Local structured diagnostic log root |

---

## Bounded History Storage & Observability

- **Bounded SQLite History**: History stores 15-minute host/device summaries capped by SQLite `max_page_count` at `NFM_SQLITE_MAX_MB` (default 1024 MB), automatically pruning the oldest summary days at 85% capacity. When upgrading an older database, stop the monitor first and run:
  ```bash
  python3 scripts/migrate-history.py data/monitor.sqlite3 --max-mb 1024
  ```
- **Health & Diagnostics**: If the collector thread fails, `/api/health` returns HTTP `503` (`degraded`) with thread liveness and exception metadata instead of fabricating success. Export redacted offline diagnostic bundles with `npu-top diagnostics bundle --root PATH --output support.json`.

---

## Documentation

| Topic | 中文 | English |
| --- | --- | --- |
| Documentation Index | [docs/README.md](./docs/README.md) | [docs/README.en.md](./docs/README.en.md) |
| System Architecture & Collection | [docs/architecture.zh.md](./docs/architecture.zh.md) | [docs/architecture.md](./docs/architecture.md) |
| Agent CLI & MCP Reference | [docs/agent-access.md](./docs/agent-access.md) | [docs/agent-access.en.md](./docs/agent-access.en.md) |
| Ascend 950 (A5) Probing & Parsing | [docs/a5-probing.zh.md](./docs/a5-probing.zh.md) | [docs/a5-probing.md](./docs/a5-probing.md) |

---

## Repository Layout

```text
npu-top/
├── npu_top/         # Python package: HTTP API, adaptive scheduler, npu-smi parser, SSH, CLI, MCP
├── app/             # Frontend source (Vite static SPA)
├── scripts/         # Wheel build script (build_wheel.sh) and DB migration script (migrate-history.py)
├── docs/            # Bilingual architecture, Agent access, and A5 probing documentation
├── .agents/skills/  # Bundled Agent Skill definition
└── tests/           # Unit test suite and captured npu-smi regression fixtures
```

---

## Development & Testing

```bash
python3 -m pip install .
python3 -m unittest discover -s tests -v
npm ci && npm run lint && npm run build
```

---

## Acknowledgments & License

The multi-server overview, idle capacity discovery, and historical heatmap interactions were inspired by [RackTop](https://github.com/Tongzh-SEU/RackTop); all implementation code was written independently and tailored for Ascend NPU devices and agentless host probing.

Released under the [MIT License](./LICENSE).
