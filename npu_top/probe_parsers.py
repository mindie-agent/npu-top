"""Pure text parsers for `/proc`, `df`, `findmnt`, Docker, and process metadata sections.

[Role] Parses delimited remote probe stdout (`__NFM_SECTION__*`) into structured CPU,
memory, disk, mount, Docker container, and `/proc/<pid>` ownership/cgroup dictionaries.
[Invariants] Pure deterministic parsing; malformed lines or partial `/proc` entries
degrade gracefully to `None`/empty fields without raising or fabricating metrics.
[Timeouts & Locks] Pure in-memory string parsing; zero subprocesses, network I/O, or locks.
[Side-Effect Policy] Side-effect free (except `attach_process_details`, which enriches caller-supplied device dicts in place).
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

SECTION = "__NFM_SECTION__"
EMPLOYEE_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{7,9})(?![A-Za-z0-9])")
INITIALS_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,4})(?![A-Za-z0-9])")
INITIALS_STOP_WORDS = {
    "app", "bin", "data", "dev", "etc", "home", "lib", "log", "logs", "mnt",
    "opt", "proc", "root", "run", "src", "sys", "test", "tmp", "usr", "var",
    "vllm", "work",
}
_DISK_WAIT_WCHAN_TOKENS = (
    "lock_page", "folio_wait", "nfs_", "ceph_", "blk_mq", "io_schedule", "wait_on_page",
)


def split_sections(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith(SECTION):
            current = line[len(SECTION):].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def parse_meminfo(text: str) -> dict[str, int | None]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    return {
        "memory_total_bytes": total,
        "memory_used_bytes": total - available if total is not None and available is not None else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free if swap_total is not None and swap_free is not None else None,
    }


def parse_cpu_stat(text: str) -> tuple[int, int] | None:
    tokens = text.split()
    if not tokens or tokens[0] != "cpu":
        return None
    try:
        values = [int(value) for value in tokens[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float | None:
    if not previous or not current:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 1)


def _is_excluded_storage(source: str | None, target: str | None, fstype: str | None = None) -> bool:
    mount = str(target or "").rstrip("/")
    excluded_mounts = ("/boot", "/efi", "/var/lib/docker/overlay", "/var/lib/docker/overlay2")
    return (
        str(source or "").casefold() == "overlay"
        or str(fstype or "").casefold() == "overlay"
        or any(mount == base or mount.startswith(base + "/") for base in excluded_mounts)
    )


def parse_disks(text: str) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        cells = line.split()
        if len(cells) < 6:
            continue
        try:
            total, used, available = int(cells[1]), int(cells[2]), int(cells[3])
            percent = float(cells[4].rstrip("%"))
        except ValueError:
            continue
        mount = " ".join(cells[5:])
        if _is_excluded_storage(cells[0], mount):
            continue
        disks.append({
            "filesystem": cells[0], "total_bytes": total, "used_bytes": used,
            "available_bytes": available, "used_percent": percent, "mount": mount,
        })
    return disks


def parse_mounts(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
        mounts: list[dict[str, Any]] = []

        def append_filesystems(filesystems: list[dict[str, Any]]) -> None:
            for item in filesystems:
                if not _is_excluded_storage(item.get("source"), item.get("target"), item.get("fstype")):
                    mounts.append({
                        "target": item.get("target"), "source": item.get("source"),
                        "fstype": item.get("fstype"), "options": item.get("options"),
                    })
                append_filesystems(item.get("children") or [])

        append_filesystems(payload.get("filesystems", []))
        return mounts
    except (json.JSONDecodeError, AttributeError):
        mounts = []
        for line in text.splitlines():
            cells = line.split()
            if len(cells) >= 4 and not _is_excluded_storage(cells[0], cells[1], cells[2]):
                mounts.append({"source": cells[0], "target": cells[1], "fstype": cells[2], "options": cells[3]})
        return mounts


def parse_docker(text: str, stats_text: str = "", info_text: str = "") -> dict[str, Any]:
    if text.strip() == "unavailable":
        return {"available": False, "containers": [], "running": 0}
    containers = []
    error = None
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            error = line[-500:]
            continue
        containers.append({
            "id": item.get("ID"), "name": item.get("Names"), "image": item.get("Image"),
            "status": item.get("Status"), "state": item.get("State"), "ports": item.get("Ports"),
        })
    stats: dict[str, dict[str, Any]] = {}
    for line in stats_text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(item.get("Name") or item.get("Container") or "")
        if key:
            stats[key] = {
                "cpu_percent": item.get("CPUPerc"), "memory": item.get("MemUsage"),
                "memory_percent": item.get("MemPerc"), "network_io": item.get("NetIO"),
                "block_io": item.get("BlockIO"), "pids": item.get("PIDs"),
            }
    for container in containers:
        container["stats"] = stats.get(str(container.get("name"))) or stats.get(str(container.get("id")))
    info: dict[str, Any] = {}
    try:
        raw_info = json.loads(info_text)
        info = {
            "server_version": raw_info.get("ServerVersion"), "driver": raw_info.get("Driver"),
            "docker_root_dir": raw_info.get("DockerRootDir"), "containers": raw_info.get("Containers"),
            "containers_running": raw_info.get("ContainersRunning"), "containers_stopped": raw_info.get("ContainersStopped"),
        }
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"available": error is None, "containers": containers, "running": len(containers), "info": info, "error": error}


def _decode_process_field(value: str) -> str | None:
    if not value:
        return None
    try:
        decoded = base64.b64decode(value).decode("utf-8", errors="replace").strip()
    except (ValueError, TypeError):
        return None
    return decoded or None


def extract_container_id(cgroup: str | None) -> str | None:
    if not cgroup:
        return None
    match = re.search(r"\b([0-9a-f]{64})\b", cgroup, re.IGNORECASE)
    return match.group(1).lower() if match else None


def extract_ownership_labels(cwd: str | None, container_name: str | None) -> list[dict[str, Any]]:
    sources = (("pwd", cwd), ("container", container_name))
    labels: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, pattern in (("employee_id", EMPLOYEE_ID_PATTERN), ("initials", INITIALS_PATTERN)):
        for source, text in sources:
            if not text:
                continue
            for match in pattern.finditer(text):
                value = match.group(1)
                normalized = value.casefold()
                if kind == "initials" and normalized in INITIALS_STOP_WORDS:
                    continue
                key = (kind, normalized)
                label = labels.setdefault(key, {"value": value, "kind": kind, "sources": []})
                if source not in label["sources"]:
                    label["sources"].append(source)
    return list(labels.values())


def _is_disk_io_wait(proc_state: str | None, wchan: str | None) -> bool:
    st = str(proc_state or "").strip().upper()
    wc = str(wchan or "").strip().lower()
    return st == "D" or any(tok in wc for tok in _DISK_WAIT_WCHAN_TOKENS)


def parse_process_details(text: str) -> dict[int, dict[str, Any]]:
    details: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) not in (7, 9) or not cells[0].isdigit():
            continue
        user, cwd, command, executable, name, cgroup = (_decode_process_field(value) for value in cells[1:7])
        proc_state = _decode_process_field(cells[7]) if len(cells) >= 9 else None
        wchan = _decode_process_field(cells[8]) if len(cells) >= 9 else None
        pid = int(cells[0])
        details[pid] = {
            "pid": pid, "name": name, "user": user, "cwd": cwd,
            "command": command, "executable": executable,
            "container_id": extract_container_id(cgroup),
            "proc_state": proc_state, "wchan": wchan,
            "disk_io_wait": _is_disk_io_wait(proc_state, wchan),
        }
    return details


def attach_process_details(
    devices: list[dict[str, Any]],
    details: dict[int, dict[str, Any]],
    docker: dict[str, Any],
) -> None:
    containers = docker.get("containers") or []
    by_id = {
        str(container.get("id") or "").lower(): container
        for container in containers if container.get("id")
    }
    for device in devices:
        enriched = []
        for record in device.get("processes") or []:
            pid = int(record["pid"])
            detail = details.get(pid) or {}
            container_id = str(detail.get("container_id") or "").lower()
            container = None
            if container_id:
                match = next(
                    (
                        item for item_id, item in by_id.items()
                        if item_id == container_id or item_id.startswith(container_id) or container_id.startswith(item_id)
                    ),
                    None,
                )
                container = {
                    "id": (match or {}).get("id") or container_id,
                    "short_id": container_id[:12],
                    "name": (match or {}).get("name"),
                    "image": (match or {}).get("image"),
                    "status": (match or {}).get("status"),
                    "source": "cgroup" if match else "cgroup-unmatched",
                }
            enriched.append({
                **record,
                "name": detail.get("name") or record.get("npu_process_name"),
                "user": detail.get("user"),
                "cwd": detail.get("cwd"),
                "command": detail.get("command"),
                "executable": detail.get("executable"),
                "proc_state": detail.get("proc_state"),
                "wchan": detail.get("wchan"),
                "disk_io_wait": bool(detail.get("disk_io_wait")),
                "details_observed_at": detail.get("details_observed_at"),
                "container": container,
                "ownership_labels": extract_ownership_labels(
                    detail.get("cwd"), (container or {}).get("name"),
                ),
            })
        device["processes"] = enriched


def is_device_busy(device: dict[str, Any], hbm_threshold_mb: int) -> bool:
    return (
        bool(device.get("processes"))
        or float(device.get("aicore_percent") or 0) > 1
        or int((device.get("hbm") or {}).get("used_mb") or 0) >= hbm_threshold_mb
    )
