"""Stdio MCP server exposing `npu-top` observation tools (`npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`).

[Role] Translates JSON-RPC 2.0 MCP tool calls into loopback HTTP requests via `VawsTopClient`.
[Invariants] All tools are read-only (`readOnlyHint=True`) and declare the observation-only contract.
[Timeouts & Locks] Honours per-call `timeout_seconds` (`1..60s`) via `VawsTopClient`.
[Side-Effect Policy] Read-only queries to local `npu-top` HTTP API and diagnostic event recording.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .observability import observed, capture_failure
from .client import (
    ClientError, VawsTopClient, format_capacity, format_mounts, format_npu, format_server,
    format_servers, format_telemetry_window,
)


LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = [MODERN_VERSION, LEGACY_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"]


CONTRACT_NOTE = (
    " Observation only: results describe host state at observed_at and are not an "
    "allocation or reservation source; obtain devices from the host-side NPU coordinator queue."
)

TOOLS = [
    {
        "name": "npu_status",
        "title": "NPU status (observed)",
        "description": "Get observed NPU usage by IP or hostname. Cache mode returns immediately; live mode asks the central collector for one fresh probe." + CONTRACT_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Server IP, hostname, display name, or id"},
                "mode": {"type": "string", "enum": ["cache", "live"], "default": "cache"},
                "include_processes": {"type": "boolean", "default": False},
                "process_details": {"type": "boolean", "default": False, "description": "Include pwd and launch command"},
                "max_age_seconds": {"type": "integer", "minimum": 0},
            },
            "required": ["host"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "server_status",
        "title": "Server status (observed)",
        "description": "Inspect cached or live NPU, CPU, memory, disks, Docker containers, processes, and likely owners of one server." + CONTRACT_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "mode": {"type": "string", "enum": ["cache", "live"], "default": "cache"},
                "process_details": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            },
            "required": ["host"], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "list_mounts",
        "title": "Server mounts (observed)",
        "description": "List mounted filesystems, capacity, and likely model-weight mount points from cache or a live infrastructure probe." + CONTRACT_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "mode": {"type": "string", "enum": ["cache", "live"], "default": "cache"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            },
            "required": ["host"], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "find_npu_capacity",
        "title": "Find observed idle NPUs",
        "description": "Rank fresh cached servers whose observed idle-NPU count and tags match; low-priority hosts sort last. Idle counts are observations that can change at any moment." + CONTRACT_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_idle_npus": {"type": "integer", "minimum": 0, "maximum": 64, "default": 1},
                "max_age_seconds": {"type": "integer", "minimum": 0, "maximum": 86400, "default": 300},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "include_disabled": {"type": "boolean", "default": False},
                "min_weight_mount_avail_gb": {
                    "type": "number", "minimum": 0, "default": 0.0,
                    "description": "Minimum available GB on a weight-candidate mount",
                },
                "prefer_same_package": {
                    "type": "boolean", "default": True,
                    "description": "Prefer A3 dual-die pairs sharing a physical package",
                },
                "exclude_throttled": {
                    "type": "boolean", "default": True,
                    "description": "Exclude hosts whose observed thermal health is throttled",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "npu_telemetry_window",
        "title": "Task-window NPU telemetry (observed)",
        "description": "Export raw per-device aicore p50/p95, HBM peak, temperature/power maxima, frequency minimum, coverage, explicit throttling state, and measurement limitations for a bounded historical window." + CONTRACT_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Server IP, hostname, display name, or id"},
                "devices": {
                    "type": "array", "items": {"type": "integer", "minimum": 0},
                    "minItems": 1, "maxItems": 256,
                },
                "start_ts": {
                    "oneOf": [{"type": "number"}, {"type": "string"}],
                    "description": "Unix seconds; fractional numbers or decimal strings preserve subsecond window bounds",
                },
                "end_ts": {
                    "oneOf": [{"type": "number"}, {"type": "string"}],
                    "description": "Unix seconds; fractional numbers or decimal strings preserve subsecond window bounds",
                },
            },
            "required": ["host", "devices", "start_ts", "end_ts"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_npu_servers",
        "title": "NPU servers (observed)",
        "description": "List monitored servers with cached online and busy-NPU counts." + CONTRACT_NOTE,
        "inputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _result(text: str, payload: Any, modern: bool, is_error: bool = False) -> dict[str, Any]:
    result = {"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": is_error}
    if modern:
        result["resultType"] = "complete"
    return result


def _meta_version(request: dict[str, Any]) -> str | None:
    meta = request.get("params", {}).get("_meta", {}) if isinstance(request.get("params"), dict) else {}
    return meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None


@observed("top.mcp.request", level="DEBUG")
def handle_request(request: dict[str, Any], client: VawsTopClient) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None:
        return None
    modern = bool(_meta_version(request)) or method == "server/discover"
    try:
        if method == "server/discover":
            result = {
                "protocolVersions": SUPPORTED_VERSIONS,
                "serverInfo": {"name": "npu-top", "version": __version__},
                "capabilities": {"tools": {"listChanged": False}},
            }
        elif method == "initialize":
            requested = request.get("params", {}).get("protocolVersion", LEGACY_VERSION)
            version = requested if requested in SUPPORTED_VERSIONS else LEGACY_VERSION
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "npu-top", "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
            if modern:
                result["resultType"] = "complete"
        elif method == "tools/call":
            params = request.get("params") or {}
            arguments = params.get("arguments") or {}
            if params.get("name") == "list_npu_servers":
                payload = client.servers()
                result = _result(format_servers(payload), payload, modern)
            elif params.get("name") == "find_npu_capacity":
                payload = client.capacity(
                    arguments.get("min_idle_npus", 1), arguments.get("max_age_seconds", 300),
                    arguments.get("tags") or [], bool(arguments.get("include_disabled")),
                    min_weight_mount_avail_gb=float(arguments.get("min_weight_mount_avail_gb", 0.0)),
                    prefer_same_package=bool(arguments.get("prefer_same_package", True)),
                    exclude_throttled=bool(arguments.get("exclude_throttled", True)),
                )
                result = _result(format_capacity(payload), payload, modern)
            elif params.get("name") == "npu_telemetry_window":
                host = arguments.get("host")
                if not isinstance(host, str) or not host.strip():
                    raise ClientError("host is required")
                devices = arguments.get("devices")
                if (
                    not isinstance(devices, list) or not devices
                    or any(isinstance(device, bool) or not isinstance(device, int) or device < 0 for device in devices)
                ):
                    raise ClientError("devices must be a non-empty array of non-negative integers")
                start_ts = arguments.get("start_ts")
                end_ts = arguments.get("end_ts")
                if (
                    isinstance(start_ts, bool) or isinstance(end_ts, bool)
                    or not isinstance(start_ts, (int, float, str))
                    or not isinstance(end_ts, (int, float, str))
                    or not str(start_ts).strip() or not str(end_ts).strip()
                ):
                    raise ClientError("start_ts and end_ts must be finite Unix seconds")
                payload = client.telemetry_window(host, devices, start_ts, end_ts)
                result = _result(format_telemetry_window(payload), payload, modern)
            elif params.get("name") in ("server_status", "list_mounts"):
                host = arguments.get("host")
                if not isinstance(host, str) or not host.strip():
                    raise ClientError("host is required")
                mode = arguments.get("mode", "cache")
                if mode not in ("cache", "live"):
                    raise ClientError("mode must be cache or live")
                payload = client.server(
                    host, mode, True, bool(arguments.get("process_details")), arguments.get("timeout_seconds", 30),
                )
                text = format_server(payload) if params.get("name") == "server_status" else format_mounts(payload)
                result = _result(text, payload, modern)
            elif params.get("name") == "npu_status":
                host = arguments.get("host")
                if not isinstance(host, str) or not host.strip():
                    raise ClientError("host is required")
                details = bool(arguments.get("process_details"))
                mode = arguments.get("mode", "cache")
                if mode not in ("cache", "live"):
                    raise ClientError("mode must be cache or live")
                payload = client.npu(host, bool(arguments.get("include_processes")) or details, details, mode)
                max_age = arguments.get("max_age_seconds")
                age = payload.get("age_seconds")
                if max_age is not None and (not isinstance(max_age, int) or max_age < 0):
                    raise ClientError("max_age_seconds must be a non-negative integer")
                if max_age is not None and (age is None or age > max_age):
                    result = _result(f"stale snapshot: age={age} max={max_age}\n{format_npu(payload)}", payload, modern, True)
                else:
                    result = _result(format_npu(payload), payload, modern)
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"}}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except ClientError as exc:
        capture_failure(exc, "monitor_unavailable")
        return {"jsonrpc": "2.0", "id": request_id, "result": _result(str(exc), {"error": str(exc)}, modern, True)}
    except Exception as exc:  # noqa: BLE001
        capture_failure(exc)
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}


@observed("top.mcp")
def main() -> int:
    try:
        client = VawsTopClient()
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("message must be an object")
            response = handle_request(request, client)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0
