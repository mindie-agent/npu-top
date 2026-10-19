"""Compact, agent-facing views over collector snapshots.

[Role] Projects raw collector snapshots into agent-oriented `servers`, `npu_status`, `server_status`, and `capacity_candidates` payloads.
[Invariants] Every payload carries an `observation` envelope (`allocation_authority=False`); never grants or reserves NPU devices. Capacity package pairs use observed per-die `phy_id` values; temperature alone never proves throttling.
[Timeouts & Locks] Pure in-memory projection; zero I/O or locks.
[Side-Effect Policy] Side-effect free.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any


OBSERVATION_KIND = "observed_state"
OBSERVATION_NOTICE = (
    "Observed host state at observed_at. Not an allocation or reservation source; "
    "device assignment is decided by the host-side NPU coordinator queue."
)


class AgentQueryError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _iso(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def observation_envelope(observed_at: int | float | None, now: int | None = None) -> dict[str, Any]:
    """Describe when the returned data was observed and what it may be used for."""
    current_time = int(time.time()) if now is None else now
    return {
        "kind": OBSERVATION_KIND,
        "observed_at": int(observed_at) if observed_at is not None else None,
        "observed_at_iso": _iso(observed_at),
        "age_seconds": max(0, current_time - int(observed_at)) if observed_at is not None else None,
        "allocation_authority": False,
        "notice": OBSERVATION_NOTICE,
    }


def _percent(used: int | float | None, total: int | float | None) -> float | None:
    if not total:
        return None
    return round(float(used or 0) * 100 / float(total), 1)


_DIE_BUSY_HBM_MB = 8192
_THROTTLE_HEALTH = {"throttled", "throttling", "thermal_throttle", "power_throttle"}
_HEALTHY_HEALTH = {"ok", "healthy", "normal", "nominal"}


def weight_mounts(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Merge mount metadata with disk capacity and mark likely model-weight storage."""
    snapshot = snapshot or {}
    disks = snapshot.get("disks") or (snapshot.get("storage") or {}).get("disks") or []
    by_mount = {disk.get("mount"): disk for disk in disks if isinstance(disk, dict)}
    rows = []
    for mount in snapshot.get("mounts") or (snapshot.get("storage") or {}).get("mounts") or []:
        if not isinstance(mount, dict):
            continue
        target = mount.get("target")
        target_text = str(target or "").casefold()
        fstype = str(mount.get("fstype") or "").casefold()
        disk = by_mount.get(target) or {}
        rows.append({
            "target": target,
            "source": mount.get("source"),
            "fstype": mount.get("fstype"),
            "options": mount.get("options"),
            "total_bytes": disk.get("total_bytes", mount.get("total_bytes")),
            "available_bytes": disk.get("available_bytes", mount.get("available_bytes")),
            "used_percent": disk.get("used_percent", mount.get("used_percent")),
            "weight_candidate": bool(
                mount.get("weight_candidate")
                or any(token in target_text for token in ("model", "weight", "/data", "/mnt", "shared"))
                or fstype in ("nfs", "nfs4", "cifs", "ceph", "lustre")
            ),
        })
    return rows


def _max_weight_mount_avail_gb(snapshot: dict[str, Any] | None) -> float | None:
    available = [
        float(row["available_bytes"]) / (1024 ** 3)
        for row in weight_mounts(snapshot)
        if row.get("weight_candidate") and isinstance(row.get("available_bytes"), (int, float))
    ]
    return round(max(available), 3) if available else None


_DCMI_THROTTLE_KEYS = ("thermal_limit", "power_limit", "svfd")
_DIRECT_THROTTLE_KEYS = ("throttled", "throttling_detected", "thermal_throttled")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _dcmi_thermal_evidence(record: Any) -> str | None:
    """Return explicit DCMI throttle evidence, or `unknown` for missing/failed reads."""
    if not isinstance(record, dict):
        return None
    if record.get("state") != "observed":
        return "unknown"
    limits = [record.get(key) for key in _DCMI_THROTTLE_KEYS]
    if any(value is True for value in limits):
        return "throttled"
    if all(value is False for value in limits) and not record.get("unknown_bits"):
        return "healthy"
    return "unknown"


def _device_thermal_evidence(device: dict[str, Any]) -> str:
    for key in _DIRECT_THROTTLE_KEYS:
        if device.get(key) is True:
            return "throttled"
    if str(device.get("health") or "").casefold() in _THROTTLE_HEALTH:
        return "throttled"
    dcmi = _dcmi_thermal_evidence(device.get("dcmi_freq_reduc"))
    if dcmi is not None:
        return dcmi
    if device.get("frequency_exit_code") not in (None, 0):
        return "unknown"
    if any(device.get(key) is False for key in _DIRECT_THROTTLE_KEYS):
        return "healthy"
    current = _finite_number(device.get("freq_mhz"))
    rated = _finite_number(device.get("freq_max_mhz"))
    if current is not None and rated is not None and rated > 0:
        # Equality with the rated maximum is explicit no-reduction evidence.
        # A lower measured value has no threshold/flag here, so it stays unknown.
        return "healthy" if current >= rated else "unknown"
    return "unknown"


def _thermal_health(snapshot: dict[str, Any] | None) -> str:
    snapshot = snapshot or {}
    declared = str(snapshot.get("thermal_health") or "").casefold()
    if (
        declared in _THROTTLE_HEALTH
        or any(snapshot.get(key) is True for key in _DIRECT_THROTTLE_KEYS)
    ):
        return "throttled"
    devices = [device for device in snapshot.get("devices") or [] if isinstance(device, dict)]
    evidence = [_device_thermal_evidence(device) for device in devices]
    if "throttled" in evidence:
        return "throttled"
    snapshot_dcmi = _dcmi_thermal_evidence(snapshot.get("dcmi"))
    if snapshot_dcmi == "throttled":
        return "throttled"
    if declared == "unknown":
        return "unknown"
    snapshot_healthy = (
        declared in _HEALTHY_HEALTH
        or any(snapshot.get(key) is False for key in _DIRECT_THROTTLE_KEYS)
    )
    if devices:
        if "unknown" in evidence:
            dcmi_unknown = any(
                _dcmi_thermal_evidence(device.get("dcmi_freq_reduc")) == "unknown"
                for device in devices
            )
            if dcmi_unknown or snapshot_dcmi == "unknown":
                return "unknown"
            return "healthy" if snapshot_healthy else "unknown"
        return "healthy" if evidence and all(item == "healthy" for item in evidence) else "unknown"
    if snapshot_healthy or snapshot_dcmi == "healthy":
        return "healthy"
    return "unknown"


def _idle_package_pairs(devices: list[dict[str, Any]]) -> list[list[int]]:
    pairs: list[list[int]] = []
    seen: set[tuple[int, int]] = set()
    for device in devices:
        if not isinstance(device, dict):
            continue
        chips = [chip for chip in device.get("chips") or [] if isinstance(chip, dict)]
        if len(chips) < 2:
            continue
        physical = [chip.get("phy_id") for chip in chips]
        chip_ids = [chip.get("chip_id") for chip in chips]
        if (
            any(not isinstance(value, int) or isinstance(value, bool) for value in physical)
            or any(not isinstance(value, int) or isinstance(value, bool) for value in chip_ids)
        ):
            # ``chip_id``/``npu_id`` are not physical package IDs. Without an
            # observed Phy-ID a same-package pair is not provable.
            continue
        processes = [process for process in device.get("processes") or [] if isinstance(process, dict)]
        busy_chips = {
            int(process["chip_id"])
            for process in processes
            if isinstance(process.get("chip_id"), int)
        }
        unmapped_process = any(process.get("chip_id") is None for process in processes)
        device_busy = device.get("busy")
        if device_busy is None:
            aicore = device.get("aicore_percent")
            device_busy = (
                bool(processes)
                or (isinstance(aicore, (int, float)) and aicore > 1)
                or int((device.get("hbm") or {}).get("used_mb") or 0) >= _DIE_BUSY_HBM_MB
            )
        if unmapped_process:
            busy_chips.update(chip_ids)
        elif device_busy is True and not busy_chips:
            busy_chips.update(chip_ids)
        for chip in chips:
            if chip.get("busy") is True:
                busy_chips.add(chip.get("chip_id"))
            if isinstance(chip.get("aicore_percent"), (int, float)) and chip["aicore_percent"] > 1:
                busy_chips.add(chip.get("chip_id"))
            if not device_busy and not unmapped_process:
                continue
            if int((chip.get("hbm") or {}).get("used_mb") or 0) >= _DIE_BUSY_HBM_MB:
                busy_chips.add(chip.get("chip_id"))
        idle = [
            int(chip["phy_id"]) for chip in chips
            if chip.get("chip_id") not in busy_chips
        ]
        for index in range(0, len(idle) - 1, 2):
            pair = tuple(sorted(idle[index:index + 2]))
            if len(pair) == 2 and pair not in seen:
                seen.add(pair)
                pairs.append(list(pair))
    return pairs


def _process(process: dict[str, Any], detailed: bool) -> dict[str, Any]:
    container = process.get("container") or {}
    result = {
        "pid": process.get("pid"),
        "name": process.get("name") or process.get("npu_process_name"),
        "npu_memory_mb": process.get("npu_memory_mb"),
        "container": container.get("name") or container.get("short_id"),
        "owners": [label.get("value") for label in process.get("ownership_labels") or [] if label.get("value")],
    }
    if detailed:
        result.update({
            "user": process.get("user"),
            "cwd": process.get("cwd"),
            "command": process.get("command"),
            "executable": process.get("executable"),
            "observed_at": process.get("details_observed_at"),
        })
    return {key: value for key, value in result.items() if value not in (None, [], "")}


def compact_server(server: dict[str, Any], snapshot: dict[str, Any] | None, now: int | None = None) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    status = snapshot.get("status") if snapshot else ("offline" if server.get("last_error") else "pending")
    summary = (snapshot or {}).get("summary") or {}
    collected_at = (snapshot or {}).get("collected_at")
    return {
        "id": server["id"],
        "name": server["name"],
        "host": server["host"],
        "enabled": bool(server.get("enabled")),
        "tags": server.get("tags") or [],
        "status": status,
        "observed_at": int(collected_at) if collected_at else None,
        "age_seconds": max(0, current_time - int(collected_at)) if collected_at else None,
        "npu_count": summary.get("npu_count") or 0,
        "busy_npu_count": summary.get("busy_npu_count") or 0,
    }


def find_server(
    query: str,
    servers: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    value = query.strip().casefold()
    if not value or len(value) > 255:
        raise AgentQueryError("host must be 1-255 characters")
    matches = []
    for server in servers:
        snapshot = snapshots.get(server["id"])
        candidates = {str(server.get(key) or "").casefold() for key in ("id", "name", "host")}
        candidates.add(str((snapshot or {}).get("hostname") or "").casefold())
        if value in candidates:
            matches.append((server, snapshot))
    if not matches:
        raise AgentQueryError(f"server not found: {query}", 404)
    if len(matches) > 1:
        hosts = ", ".join(f"{server['host']}:{server['port']}" for server, _ in matches)
        raise AgentQueryError(f"server is ambiguous: {hosts}", 409)
    return matches[0]


def npu_status(
    server: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    include_processes: bool = False,
    detailed_processes: bool = False,
    now: int | None = None,
) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    compact = compact_server(server, snapshot, current_time)
    summary = (snapshot or {}).get("summary") or {}
    collected_at = (snapshot or {}).get("collected_at")
    devices = []
    for device in (snapshot or {}).get("devices") or []:
        hbm = device.get("hbm") or {}
        processes = device.get("processes") or []
        owners = []
        containers = []
        for process in processes:
            container = process.get("container") or {}
            if container.get("name") and container["name"] not in containers:
                containers.append(container["name"])
            for label in process.get("ownership_labels") or []:
                if label.get("value") and label["value"] not in owners:
                    owners.append(label["value"])
        row = {
            "id": device.get("npu_id"),
            "name": device.get("name"),
            "aicore_percent": device.get("aicore_percent"),
            "hbm_used_mb": hbm.get("used_mb"),
            "hbm_total_mb": hbm.get("total_mb"),
            "hbm_percent": _percent(hbm.get("used_mb"), hbm.get("total_mb")),
            "busy": bool(device.get("busy")),
            "process_count": len(processes),
            "containers": containers,
            "owners": owners,
        }
        if device.get("chips"):
            row["chips"] = [
                {
                    "chip_id": chip.get("chip_id"),
                    "phy_id": chip.get("phy_id"),
                    "aicore_percent": chip.get("aicore_percent"),
                    "hbm_used_mb": (chip.get("hbm") or {}).get("used_mb"),
                    "hbm_total_mb": (chip.get("hbm") or {}).get("total_mb"),
                }
                for chip in device["chips"] if isinstance(chip, dict)
            ]
        if include_processes:
            row["processes"] = [_process(process, detailed_processes) for process in processes]
        devices.append(row)
    return {
        "source": "cache",
        "observation": observation_envelope(collected_at, current_time),
        "server": {
            "id": compact["id"], "name": compact["name"], "host": compact["host"],
            "enabled": compact["enabled"], "tags": compact["tags"], "status": compact["status"],
        },
        "collected_at": collected_at,
        "age_seconds": compact["age_seconds"],
        "summary": {
            "npu_count": summary.get("npu_count") or 0,
            "busy_npu_count": summary.get("busy_npu_count") or 0,
            "idle_npu_count": max(0, (summary.get("npu_count") or 0) - (summary.get("busy_npu_count") or 0)),
            "aicore_percent": summary.get("npu_util_percent"),
            "hbm_used_mb": summary.get("hbm_used_mb") or 0,
            "hbm_total_mb": summary.get("hbm_total_mb") or 0,
        },
        "devices": devices,
        **({"recording": snapshot["recording"]} if snapshot and "recording" in snapshot else {}),
        **({"process_details": (snapshot or {}).get("process_details", {"state": "not_observed"})}
           if include_processes or detailed_processes else {}),
        **({"error": str((snapshot or {}).get("error") or server.get("last_error"))[-240:]} if compact["status"] == "offline" else {}),
    }


def server_status(
    server: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    include_processes: bool = True,
    detailed_processes: bool = False,
    include_infrastructure: bool = True,
    now: int | None = None,
) -> dict[str, Any]:
    result = npu_status(
        server, snapshot, include_processes=include_processes,
        detailed_processes=detailed_processes, now=now,
    )
    summary = (snapshot or {}).get("summary") or {}
    memory_total = summary.get("memory_total_bytes") or 0
    memory_used = summary.get("memory_used_bytes") or 0
    result["system"] = {
        "cpu_percent": summary.get("cpu_percent"),
        "load": [summary.get("load1"), summary.get("load5"), summary.get("load15")],
        "memory_used_bytes": memory_used,
        "memory_total_bytes": memory_total,
        "memory_percent": _percent(memory_used, memory_total),
        "disk_max_percent": summary.get("disk_max_percent"),
        "docker_running": summary.get("docker_running"),
    }
    if include_infrastructure:
        result["storage"] = {
            "disks": (snapshot or {}).get("disks") or [],
            "mounts": weight_mounts(snapshot),
        }
        result["docker"] = (snapshot or {}).get("docker") or {"available": None, "containers": []}
    return result


def capacity_candidates(
    servers: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    *,
    min_idle_npus: int = 1,
    max_age_seconds: int = 300,
    tags: list[str] | None = None,
    include_disabled: bool = False,
    now: int | None = None,
    min_weight_mount_avail_gb: float = 0.0,
    prefer_same_package: bool = True,
    exclude_throttled: bool = True,
) -> dict[str, Any]:
    current_time = int(time.time()) if now is None else now
    required_tags = {tag.casefold() for tag in tags or []}
    if min_weight_mount_avail_gb < 0:
        raise AgentQueryError("min_weight_mount_avail_gb must be non-negative")
    candidates = []
    for server in servers:
        if not include_disabled and not server.get("enabled"):
            continue
        if required_tags and not required_tags.issubset({str(tag).casefold() for tag in server.get("tags") or []}):
            continue
        snapshot = snapshots.get(server["id"])
        compact = compact_server(server, snapshot, current_time)
        summary = (snapshot or {}).get("summary") or {}
        idle = max(0, compact["npu_count"] - compact["busy_npu_count"])
        if compact["status"] != "online" or compact["age_seconds"] is None or compact["age_seconds"] > max_age_seconds:
            continue
        if idle < min_idle_npus:
            continue
        thermal_health = _thermal_health(snapshot)
        if exclude_throttled and thermal_health == "throttled":
            continue
        mount_avail_gb = _max_weight_mount_avail_gb(snapshot)
        if min_weight_mount_avail_gb > 0 and (
            mount_avail_gb is None or mount_avail_gb < min_weight_mount_avail_gb
        ):
            continue
        candidates.append({
            **compact,
            "observed_at": int(snapshot["collected_at"]),
            "idle_npu_count": idle,
            "IdlePackagePairs": _idle_package_pairs((snapshot or {}).get("devices") or []),
            "MaxWeightMountAvailGb": mount_avail_gb,
            "ThermalHealth": thermal_health,
            "aicore_percent": summary.get("npu_util_percent"),
            "hbm_used_mb": summary.get("hbm_used_mb") or 0,
            "hbm_total_mb": summary.get("hbm_total_mb") or 0,
            "cpu_percent": summary.get("cpu_percent"),
            "memory_percent": _percent(summary.get("memory_used_bytes"), summary.get("memory_total_bytes")),
            "disk_max_percent": summary.get("disk_max_percent"),
        })
    candidates.sort(key=lambda item: (
        "低优先级" in item["tags"],
        -len(item["IdlePackagePairs"]) if prefer_same_package else 0,
        -item["idle_npu_count"], item["age_seconds"], item["host"],
    ))
    # Each candidate is an observation with its own age; the envelope for the
    # list as a whole is the oldest snapshot it relied on.
    oldest = min((snapshots[item["id"]]["collected_at"] for item in candidates), default=None)
    return {
        "source": "cache",
        "observation": {
            **observation_envelope(oldest, current_time),
            "kind": "observed_availability",
            "notice": (
                "Idle counts are observed at each candidate's observed_at and can change "
                "before any workload starts. Not a reservation; obtain devices from the "
                "host-side NPU coordinator queue."
            ),
        },
        "requirements": {
            "min_idle_npus": min_idle_npus, "max_age_seconds": max_age_seconds, "tags": tags or [],
            "min_weight_mount_avail_gb": min_weight_mount_avail_gb,
            "prefer_same_package": prefer_same_package, "exclude_throttled": exclude_throttled,
        },
        "candidates": candidates,
    }
