"""Command-line entry point for `npu-top` (`serve`, `mcp`, `diagnostics`, `inventory`, `servers`, `npu`, `status`, `mounts`, `capacity`).

[Role] Parses CLI flags and dispatches to local HTTP server, stdio MCP server, diagnostics exporter, machine-inventory bridge, or `VawsTopClient`.
[Invariants] Observation-only; exits `3` when `--max-age` is exceeded and `2` when the loopback monitor or inventory bridge fails. `inventory sync` writes only the explicitly selected machines.json.
[Timeouts & Locks] Passes `--timeout` (`default 30s`) to `VawsTopClient` for live probes and cached inventory reads.
[Side-Effect Policy] Loopback HTTP queries, explicit machine-inventory/DB writes on `inventory sync`, or server launch on `serve`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .observability import observed, capture_failure
from .client import (
    ClientError, VawsTopClient, format_capacity, format_mounts, format_npu, format_server, format_servers,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="npu-top",
        description="Local Ascend NPU fleet monitor and compact observation CLI",
        epilog=(
            "Observation only: every result describes host state at its observed_at timestamp. "
            "It is not an allocation or reservation source; obtain devices from the host-side "
            "NPU coordinator queue."
        ),
    )
    result.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    result.add_argument("--url", help="npu-top loopback API URL (default: http://127.0.0.1:8788)")
    result.add_argument("--json", action="store_true", help="emit compact JSON")
    sub = result.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start HTTP API and static console on loopback")
    serve.add_argument("--bind", default=None, help="listen address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="listen port (default: 8788)")
    sub.add_parser("mcp", help="stdio MCP server")
    diagnostics = sub.add_parser("diagnostics", help="local diagnostic export and configured reporting")
    diagnostics.add_argument("arguments", nargs=argparse.REMAINDER)
    inventory = sub.add_parser("inventory", help="bridge coordinator machine inventory and npu-top observations")
    inventory_commands = inventory.add_subparsers(dest="inventory_command", required=True)
    inventory_sync = inventory_commands.add_parser(
        "sync", help="read coordinator machines or write observed hardware/tags/weight mounts back",
    )
    inventory_sync.add_argument(
        "--from-coordinator", action="store_true",
        help="register machines.json hosts in npu-top's local server registry",
    )
    inventory_sync.add_argument(
        "--to-coordinator", action="store_true",
        help="merge cached npu-top hardware/tags/weight mounts into machines.json atomically",
    )
    inventory_sync.add_argument(
        "--machines", type=Path,
        help=(
            "explicit machines.json path (default: MINDIE_COORDINATOR_STATE_DIR or first "
            "MINDIE_TOP_INVENTORY_FILES/NFM_INVENTORY_FILES entry)"
        ),
    )
    inventory_sync.add_argument("--timeout", type=int, default=30)
    sub.add_parser("servers", help="list monitored servers")
    npu = sub.add_parser("npu", help="show cached NPU status by IP, hostname, name, or server id")
    npu.add_argument("host")
    npu.add_argument("--live", action="store_true", help="ask the collector for a fresh remote snapshot")
    npu.add_argument("--timeout", type=int, default=30)
    npu.add_argument("--processes", action="store_true", help="include compact process records in JSON")
    npu.add_argument("--process-details", action="store_true", help="include pwd and command in JSON")
    npu.add_argument("--max-age", type=int, help="exit 3 when the cached snapshot is older than this many seconds")
    npu.add_argument("--ultra-compact", action="store_true", help="emit one summary line")
    status = sub.add_parser("status", help="show NPU, CPU, memory, Docker, processes, and storage")
    status.add_argument("host")
    status_mode = status.add_mutually_exclusive_group()
    status_mode.add_argument("--live", dest="mode", action="store_const", const="live", help="request a fresh probe (default)")
    status_mode.add_argument("--cache", dest="mode", action="store_const", const="cache", help="return the current cached snapshot")
    status.set_defaults(mode="live")
    status.add_argument("--timeout", type=int, default=30)
    status.add_argument("--process-details", action="store_true")
    mounts = sub.add_parser("mounts", help="show mount points and likely model-weight storage")
    mounts.add_argument("host")
    mounts.add_argument("--live", action="store_true")
    mounts.add_argument("--timeout", type=int, default=30)
    capacity = sub.add_parser("capacity", help="list servers whose observed idle-NPU count matches (not a reservation)")
    capacity.add_argument("--min-idle", type=int, default=1)
    capacity.add_argument("--max-age", type=int, default=300)
    capacity.add_argument("--tag", action="append", default=[])
    capacity.add_argument("--include-disabled", action="store_true")
    capacity.add_argument("--min-weight-mount-avail-gb", type=float, default=0.0)
    capacity.add_argument("--no-prefer-same-package", action="store_true")
    capacity.add_argument("--include-throttled", action="store_true")
    return result


def _machines_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    coordinator_dir = os.environ.get("MINDIE_COORDINATOR_STATE_DIR", "").strip()
    if coordinator_dir:
        return Path(coordinator_dir).expanduser() / "machines.json"
    from .settings import Settings
    settings = Settings.load()
    if settings.inventory_files:
        return settings.inventory_files[0]
    raise ValueError(
        "inventory sync requires --machines PATH or an existing "
        "MINDIE_COORDINATOR_STATE_DIR / MINDIE_TOP_INVENTORY_FILES / NFM_INVENTORY_FILES configuration"
    )


def _inventory_sync(args: argparse.Namespace) -> dict:
    if not args.from_coordinator and not args.to_coordinator:
        raise ValueError("inventory sync requires --from-coordinator and/or --to-coordinator")
    from .inventory_bridge import import_from_coordinator, sync_to_coordinator
    path = _machines_path(args.machines)
    result: dict = {"machines_file": str(path)}
    if args.from_coordinator:
        from .db import Database
        from .settings import Settings
        settings = Settings.load()
        settings.prepare()
        db = Database(settings.state_dir / "monitor.sqlite3", settings.sqlite_max_mb * 1024 * 1024)
        db.initialize()
        try:
            result["from_coordinator"] = import_from_coordinator(path, db)
        finally:
            db.close()
    if args.to_coordinator:
        client = VawsTopClient(args.url)
        servers = (client.servers().get("servers") or [])
        snapshots = {
            server["id"]: client.server(
                server["host"], mode="cache", include_processes=False,
                detailed_processes=False, timeout=args.timeout,
            )
            for server in servers
        }
        result["to_coordinator"] = sync_to_coordinator(path, servers, snapshots)
    return result


def _format_inventory(result: dict) -> str:
    rows = [f"machines={result['machines_file']}"]
    source = result.get("from_coordinator")
    if source:
        rows.append(
            f"from-coordinator machines={source['machines']} registered={source['registered']} "
            f"updated={source['updated']} unchanged={source['unchanged']}"
        )
    target = result.get("to_coordinator")
    if target:
        rows.append(
            f"to-coordinator observations={target['observations']} matched={target['matched']} "
            f"added={target['added']} hardware={target['hardware']} tags={target['tags']} "
            f"mounts={target['mounts']}"
        )
    return "\n".join(rows)


@observed("top.cli")
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["diagnostics"]:
        from mindie_diagnostics.cli import main as diagnostics_main
        return diagnostics_main(argv[1:])
    args = parser().parse_args(argv)
    if args.command == "diagnostics":
        from mindie_diagnostics.cli import main as diagnostics_main
        return diagnostics_main(args.arguments)
    if args.command == "serve":
        # Explicit CLI flags must beat ambient primary and legacy environment
        # settings. `Settings.load()` gives MINDIE_TOP_* precedence, so setting
        # both names keeps the historical NFM_* contract visible as well.
        if args.bind:
            os.environ["MINDIE_TOP_BIND"] = args.bind
            os.environ["NFM_BIND"] = args.bind
        if args.port is not None:
            os.environ["MINDIE_TOP_PORT"] = str(args.port)
            os.environ["NFM_PORT"] = str(args.port)
        from .static_files import require_static
        from .serve import main as serve_main
        require_static()
        serve_main()
        return 0
    if args.command == "mcp":
        from .mcp import main as mcp_main
        return mcp_main()
    if args.command == "inventory":
        try:
            payload = _inventory_sync(args)
        except (ClientError, TimeoutError, OSError, ValueError) as exc:
            capture_failure(exc, "inventory_sync_failed")
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else _format_inventory(payload))
        return 0
    try:
        client = VawsTopClient(args.url)
        if args.command == "servers":
            payload = client.servers()
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_servers(payload)
        elif args.command == "npu":
            payload = client.npu(
                args.host, args.processes or args.process_details, args.process_details,
                "live" if args.live else "cache", args.timeout,
            )
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_npu(payload, args.ultra_compact)
            age = payload.get("age_seconds")
            if args.max_age is not None and (age is None or age > args.max_age):
                print(output)
                print(f"stale snapshot: age={age} max={args.max_age}", file=sys.stderr)
                return 3
        elif args.command in ("status", "mounts"):
            payload = client.server(
                args.host, args.mode if args.command == "status" else ("live" if args.live else "cache"), True,
                getattr(args, "process_details", False), args.timeout,
            )
            if args.json:
                output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            else:
                output = format_server(payload) if args.command == "status" else format_mounts(payload)
        else:
            payload = client.capacity(
                args.min_idle, args.max_age, args.tag, args.include_disabled,
                min_weight_mount_avail_gb=args.min_weight_mount_avail_gb,
                prefer_same_package=not args.no_prefer_same_package,
                exclude_throttled=not args.include_throttled,
            )
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_capacity(payload)
        print(output)
        return 0
    except ClientError as exc:
        capture_failure(exc, "monitor_unavailable")
        print(str(exc), file=sys.stderr)
        return 2
