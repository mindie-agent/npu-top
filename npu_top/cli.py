from __future__ import annotations

import argparse
import json
import os
import sys

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
    return result


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
        if args.bind:
            os.environ["NFM_BIND"] = args.bind
        if args.port is not None:
            os.environ["NFM_PORT"] = str(args.port)
        from .static_files import require_static
        from .serve import main as serve_main
        require_static()
        serve_main()
        return 0
    if args.command == "mcp":
        from .mcp import main as mcp_main
        return mcp_main()
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
            payload = client.capacity(args.min_idle, args.max_age, args.tag, args.include_disabled)
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else format_capacity(payload)
        print(output)
        return 0
    except ClientError as exc:
        capture_failure(exc, "monitor_unavailable")
        print(str(exc), file=sys.stderr)
        return 2
