"""Loopback HTTP client and compact text formatters for `npu-top` CLI and MCP.

[Role] Queries `/api/agent/*` endpoints bypassing environment proxies (`ProxyHandler({})`) and formats compact human/agent summaries.
[Invariants] Refuses non-loopback URLs unless `MINDIE_TOP_ALLOW_REMOTE=1` is set.
[Timeouts & Locks] Default `3.0s` HTTP timeout for cached reads; expands to `timeout + 2s` (`>= inner_timeout + grace`) for `mode="live"`.
[Side-Effect Policy] Loopback HTTP GET requests only.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, build_opener


DEFAULT_URL = "http://127.0.0.1:8788"


class ClientError(RuntimeError):
    pass


class VawsTopClient:
    def __init__(self, base_url: str | None = None, timeout: float = 3.0) -> None:
        self.base_url = (base_url or os.environ.get("MINDIE_TOP_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        parsed = urlparse(self.base_url)
        if (
            (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path not in ("", "/"))
            and os.environ.get("MINDIE_TOP_ALLOW_REMOTE") != "1"
        ):
            raise ClientError("non-loopback API requires MINDIE_TOP_ALLOW_REMOTE=1")
        self.opener = build_opener(ProxyHandler({}))

    def _get(
        self, path: str, query: dict[str, str] | None = None, request_timeout: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlencode(query)
        try:
            with self.opener.open(url, timeout=request_timeout or self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error")
            except (json.JSONDecodeError, AttributeError):
                message = None
            raise ClientError(message or f"npu-top API returned HTTP {exc.code}") from exc
        except (TimeoutError, OSError, URLError, json.JSONDecodeError) as exc:
            raise ClientError(f"npu-top unavailable at {self.base_url}: {exc}") from exc

    def servers(self) -> dict[str, Any]:
        return self._get("/api/agent/servers")

    def npu(
        self, host: str, include_processes: bool = False, detailed_processes: bool = False,
        mode: str = "cache", timeout: int = 30,
    ) -> dict[str, Any]:
        query = {"host": host, "mode": mode, "timeout": str(timeout)}
        if include_processes:
            query["processes"] = "1"
        if detailed_processes:
            query.update({"processes": "1", "details": "1"})
        return self._get("/api/agent/npu", query, timeout + 2 if mode == "live" else None)

    def server(
        self, host: str, mode: str = "cache", include_processes: bool = True,
        detailed_processes: bool = False, timeout: int = 30,
    ) -> dict[str, Any]:
        return self._get("/api/agent/server", {
            "host": host, "mode": mode, "timeout": str(timeout),
            "processes": "1" if include_processes else "0",
            "details": "1" if detailed_processes else "0",
        }, timeout + 2 if mode == "live" else None)

    def capacity(
        self, min_idle_npus: int = 1, max_age_seconds: int = 300,
        tags: list[str] | None = None, include_disabled: bool = False,
        *, min_weight_mount_avail_gb: float = 0.0, prefer_same_package: bool = True,
        exclude_throttled: bool = True,
    ) -> dict[str, Any]:
        return self._get("/api/agent/capacity", {
            "min_idle_npus": str(min_idle_npus), "max_age_seconds": str(max_age_seconds),
            "tags": ",".join(tags or []), "include_disabled": "1" if include_disabled else "0",
            "min_weight_mount_avail_gb": f"{float(min_weight_mount_avail_gb):g}",
            "prefer_same_package": "1" if prefer_same_package else "0",
            "exclude_throttled": "1" if exclude_throttled else "0",
        })

    def telemetry_window(
        self, host: str, devices: list[int], start_ts: int | float | str,
        end_ts: int | float | str,
    ) -> dict[str, Any]:
        return self._get("/api/agent/telemetry-window", {
            "host": host,
            "devices": ",".join(str(device) for device in devices),
            "start_ts": str(start_ts),
            "end_ts": str(end_ts),
        })


def _number(value: Any, suffix: str = "%") -> str:
    return "-" if value is None else f"{float(value):g}{suffix}"


def _gib(value_mb: Any) -> str:
    return f"{float(value_mb or 0) / 1024:.1f}G"


def format_npu(payload: dict[str, Any], ultra_compact: bool = False) -> str:
    server = payload["server"]
    summary = payload["summary"]
    age = "-" if payload.get("age_seconds") is None else f"{payload['age_seconds']}s"
    headline = (
        f"{server['host']} {server['status']} age={age} "
        f"npu={summary['npu_count']} busy={summary['busy_npu_count']} "
        f"util={_number(summary.get('aicore_percent'))} "
        f"hbm={_gib(summary.get('hbm_used_mb'))}/{_gib(summary.get('hbm_total_mb'))}"
    )
    if payload.get("error"):
        headline += f" error={payload['error']}"
    if payload.get("recording", {}).get("state") == "failed":
        headline += " history_recording=failed"
    if payload.get("process_details", {}).get("state") == "unavailable":
        headline += " process_details=unavailable"
    if ultra_compact or not payload.get("devices"):
        return headline
    lines = [headline]
    for device in payload["devices"]:
        owner = ",".join(device.get("owners") or device.get("containers") or []) or "-"
        state = "busy" if device.get("busy") else "idle"
        lines.append(
            f"{device.get('id')} {state} util={_number(device.get('aicore_percent'))} "
            f"hbm={_gib(device.get('hbm_used_mb'))}/{_gib(device.get('hbm_total_mb'))} "
            f"proc={device.get('process_count', 0)} owner={owner}"
        )
    return "\n".join(lines)


def format_servers(payload: dict[str, Any]) -> str:
    rows = []
    runtime = payload.get("runtime", {})
    if runtime.get("inventory", {}).get("state") == "failed":
        rows.append("inventory_import=failed; configured fleet may be incomplete")
    if runtime.get("storage_failures") or runtime.get("maintenance_error"):
        rows.append("history_recording=failed; current observations do not establish saved history")
    if runtime.get("collector_status") in {"failed", "stopped"}:
        rows.append(f"collector={runtime['collector_status']}")
    for server in payload.get("servers") or []:
        age = "-" if server.get("age_seconds") is None else f"{server['age_seconds']}s"
        rows.append(
            f"{server['host']} {server['status']} age={age} "
            f"npu={server['npu_count']} busy={server['busy_npu_count']} name={server['name']}"
        )
    return "\n".join(rows) or "no servers"


def format_server(payload: dict[str, Any]) -> str:
    lines = [format_npu(payload, ultra_compact=True)]
    system = payload.get("system") or {}
    lines.append(
        f"system cpu={_number(system.get('cpu_percent'))} mem={_number(system.get('memory_percent'))} "
        f"disk={_number(system.get('disk_max_percent'))} docker={system.get('docker_running', '-')}"
    )
    processes = [process for device in payload.get("devices") or [] for process in device.get("processes") or []]
    groups: dict[tuple[str, tuple[str, ...], str], dict[str, Any]] = {}
    for process in processes:
        key = (
            str(process.get("container") or "-"),
            tuple(process.get("owners") or []),
            str(process.get("name") or "-"),
        )
        group = groups.setdefault(key, {"pids": [], "npu_memory_mb": 0})
        if process.get("pid") is not None:
            group["pids"].append(str(process["pid"]))
        group["npu_memory_mb"] += int(process.get("npu_memory_mb") or 0)
    for (container, owners, name), group in groups.items():
        pids = ",".join(group["pids"][:4]) + (",..." if len(group["pids"]) > 4 else "")
        lines.append(
            f"proc x{len(group['pids'])} npu_mem={_gib(group['npu_memory_mb'])} "
            f"container={container} owner={','.join(owners) or '-'} name={name} pids={pids or '-'}"
        )
    candidates = [mount for mount in (payload.get("storage") or {}).get("mounts") or [] if mount.get("weight_candidate")]
    if candidates:
        lines.append("mounts " + " ".join(str(mount.get("target")) for mount in candidates))
    return "\n".join(lines)


def format_mounts(payload: dict[str, Any]) -> str:
    rows = []
    hidden_types = {
        "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts", "devtmpfs",
        "efivarfs", "fusectl", "hugetlbfs", "mqueue", "nsfs", "overlay", "proc", "rpc_pipefs",
        "securityfs", "selinuxfs", "sysfs", "tmpfs", "tracefs",
    }
    for mount in (payload.get("storage") or {}).get("mounts") or []:
        target = str(mount.get("target") or "")
        if (
            not target
            or str(mount.get("fstype") or "").casefold() in hidden_types
            or target.startswith("/var/lib/docker/")
            or target in ("/boot", "/efi")
            or target.startswith(("/boot/", "/efi/"))
        ):
            continue
        capacity = ""
        if mount.get("total_bytes") is not None:
            capacity = f" used={_number(mount.get('used_percent'))} free={float(mount.get('available_bytes') or 0) / (1024 ** 3):.1f}G"
        candidate = " weight-candidate" if mount.get("weight_candidate") else ""
        rows.append(f"{mount['target']} source={mount.get('source', '-')} type={mount.get('fstype', '-')}{capacity}{candidate}")
    return "\n".join(rows) or "no mounts in cache"


def format_capacity(payload: dict[str, Any]) -> str:
    rows = []
    for server in payload.get("candidates") or []:
        pairs = server.get("IdlePackagePairs") or []
        thermal = server.get("ThermalHealth") or "unknown"
        mount_gb = server.get("MaxWeightMountAvailGb")
        rows.append(
            f"{server['host']} idle={server['idle_npu_count']}/{server['npu_count']} "
            f"age={server['age_seconds']}s cpu={_number(server.get('cpu_percent'))} "
            f"mem={_number(server.get('memory_percent'))} tags={','.join(server.get('tags') or []) or '-'} "
            f"thermal={thermal} pairs={pairs or '-'} "
            f"weight_mount={'?' if mount_gb is None else f'{float(mount_gb):.1f}G'}"
        )
    return "\n".join(rows) or "no matching capacity"


def format_telemetry_window(payload: dict[str, Any]) -> str:
    window = payload.get("window") or {}
    throttling = payload.get("throttling_detected")
    throttle_text = "unknown" if throttling is None else ("detected" if throttling else "none-observed")
    interference = payload.get("resource_interference_detected")
    interference_text = "unknown" if interference is None else ("detected" if interference else "none-observed")
    sources = ",".join(window.get("timestamp_sources") or []) or "-"
    lines = [
        f"{payload['server']['host']} telemetry {window.get('start_ts')}..{window.get('end_ts')} "
        f"coverage={_number(payload.get('coverage_ratio'), '')} "
        f"interval={_number(window.get('sample_interval_seconds'), 's')} "
        f"sources={sources} throttling={throttle_text} "
        f"resource_interference={interference_text} raw_samples={window.get('raw_sample_count', 0)}"
    ]
    for device in payload.get("devices") or []:
        device_throttle = device.get("throttling_detected")
        device_throttle_text = "null" if device_throttle is None else str(device_throttle).lower()
        device_interference = device.get("resource_interference_detected")
        device_interference_text = "null" if device_interference is None else str(device_interference).lower()
        lines.append(
            f"{device.get('npu_id')} n={device.get('sample_count', 0)} "
            f"cov={_number(device.get('coverage_ratio'), '')} "
            f"aicore_p50={_number(device.get('aicore_p50'), '%')} "
            f"aicore_p95={_number(device.get('aicore_p95'), '%')} "
            f"hbm_peak={_number(device.get('hbm_peak_mb'), 'M')} "
            f"temp_max={_number(device.get('temp_max_c'), 'C')} "
            f"power_max={_number(device.get('power_max_w'), 'W')} "
            f"freq_min={_number(device.get('freq_mhz_min'), 'MHz')} "
            f"throttling={device_throttle_text} interference={device_interference_text}"
        )
    for limitation in payload.get("measurement_limitations") or []:
        lines.append(f"limitation: {limitation}")
    return "\n".join(lines)
