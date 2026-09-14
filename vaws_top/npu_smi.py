"""Parsers for ``npu-smi info`` and ``npu-smi info -t usages`` text output.

The monitor owns this parser so that it never has to locate another project
on disk to interpret Ascend devices. The layouts handled here are:

* the two-row device table (one ``NPU Name | Health | Power Temp`` header row
  followed by one or more ``Chip [Phy-ID] | Bus-Id | AICore Memory HBM``
  rows, as printed by 910B-class and A3/910C-class drivers);
* the process table in both the ``NPU Chip | PID | Name | Memory`` and the
  older flat ``NPU | PID | Name | Memory`` layouts;
* the single-ID four-column Ascend950/A5 table and chipless process rows;
* the ``key : value`` blocks printed by ``npu-smi info -t usages``.

Everything returned here is *observed* device state at collection time.
"""

from __future__ import annotations

import re

from .npu_single_id import parse_single_id_table
from typing import Any


_PAIR = r"(\d+)\s*/\s*(\d+)"
_CHIP_METRICS = re.compile(
    rf"^(?P<bus>\S+)\s+(?P<aicore>-?\d+(?:\.\d+)?)\s+{_PAIR}\s+{_PAIR}"
)
_HEADER_METRICS = re.compile(
    r"^(?P<health>[A-Za-z][A-Za-z_-]*)\s+(?P<power>-?\d+(?:\.\d+)?|NA|-)\s+(?P<temp>-?\d+(?:\.\d+)?|NA|-)"
)
_INT_CELL = re.compile(r"^\d+(?:\s+\d+)?$")
# "0 910B4" / "3 Ascend910" start a logical device; "0 0" (chip, phy-id) does not.
_HEADER_CELL = re.compile(r"^(\d+)\s+(?!\d+$)([A-Za-z0-9][A-Za-z0-9_.-]*)$")
_USAGE_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ()%/_.-]*?)\s*:\s*(.*?)\s*$")


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return cells


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _new_device(npu_id: int, name: str | None) -> dict[str, Any]:
    return {
        "npu_id": npu_id, "name": name, "health": None, "power_w": None, "temperature_c": None,
        "bus_id": None, "aicore_percent": None,
        "memory": {"used_mb": 0, "total_mb": 0}, "hbm": {"used_mb": 0, "total_mb": 0},
        "chips": [], "processes": [],
    }


def _finalize_device(device: dict[str, Any]) -> None:
    chips = device["chips"]
    if chips:
        device["bus_id"] = chips[0].get("bus_id")
        utilizations = [chip["aicore_percent"] for chip in chips if chip.get("aicore_percent") is not None]
        device["aicore_percent"] = max(utilizations) if utilizations else None
        device["memory"] = {
            "used_mb": sum(chip["memory"]["used_mb"] for chip in chips),
            "total_mb": sum(chip["memory"]["total_mb"] for chip in chips),
        }
        device["hbm"] = {
            "used_mb": sum(chip["hbm"]["used_mb"] for chip in chips),
            "total_mb": sum(chip["hbm"]["total_mb"] for chip in chips),
        }
    if len(chips) <= 1:
        # A single-die card is rendered as the logical device itself; a
        # per-die list would only duplicate the same numbers.
        device.pop("chips")


def parse_npu_smi_info(text: str) -> dict[str, Any]:
    single_id = parse_single_id_table(text)
    if single_id is not None:
        return single_id
    devices: list[dict[str, Any]] = []
    by_id: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    in_process_table = False
    process_records: list[dict[str, Any]] = []

    for line in text.splitlines():
        cells = _cells(line)
        if cells is None:
            continue
        joined = " ".join(cells)
        if "Process id" in joined or "Process name" in joined:
            in_process_table = True
            continue
        if not cells or not cells[0]:
            continue
        first = cells[0]

        if in_process_table:
            if "No running processes" in joined:
                continue
            if len(cells) < 3 or not _INT_CELL.match(first) or not cells[1].isdigit():
                continue
            identity = [int(token) for token in first.split()]
            npu_id = identity[0]
            chip_id = identity[1] if len(identity) > 1 else None
            device = by_id.get(npu_id)
            memory = _number(cells[3]) if len(cells) > 3 else None
            record: dict[str, Any] = {
                "npu_id": npu_id, "pid": int(cells[1]), "npu_process_name": cells[2] or None,
                "npu_memory_mb": int(memory) if memory is not None else None,
            }
            if chip_id is not None:
                record["chip_id"] = chip_id
                if device:
                    phy = next((chip.get("phy_id") for chip in device.get("chips") or [] if chip["chip_id"] == chip_id), None)
                    if phy is not None:
                        record["phy_id"] = phy
            process_records.append(record)
            if device is not None:
                device["processes"].append(dict(record))
            continue

        header = _HEADER_CELL.match(first)
        if header and len(cells) >= 2:
            npu_id = int(header.group(1))
            current = by_id.get(npu_id)
            if current is None:
                current = _new_device(npu_id, header.group(2))
                by_id[npu_id] = current
                devices.append(current)
            metrics = _HEADER_METRICS.match(" ".join(cells[1:]))
            if metrics:
                current["health"] = metrics.group("health")
                current["power_w"] = _number(metrics.group("power"))
                current["temperature_c"] = _number(metrics.group("temp"))
            continue

        if _INT_CELL.match(first) and len(cells) >= 2 and current is not None:
            metrics = _CHIP_METRICS.match(" ".join(cells[1:]))
            if not metrics:
                continue
            identity = [int(token) for token in first.split()]
            chip = {
                "chip_id": identity[0],
                "phy_id": identity[1] if len(identity) > 1 else None,
                "bus_id": metrics.group("bus"),
                "aicore_percent": _number(metrics.group("aicore")),
                "memory": {"used_mb": int(metrics.group(3)), "total_mb": int(metrics.group(4))},
                "hbm": {"used_mb": int(metrics.group(5)), "total_mb": int(metrics.group(6))},
            }
            if chip["phy_id"] is None:
                chip.pop("phy_id")
            current["chips"].append(chip)

    for device in devices:
        _finalize_device(device)
    return {"devices": devices, "process_records": process_records}


def parse_npu_smi_usages(text: str) -> dict[int, dict[str, Any]]:
    """Return ``{npu_id: {"aicore_percent", "hbm_percent", "chips": {chip_id: {...}}}}``."""
    usages: dict[int, dict[str, Any]] = {}
    block: dict[str, Any] | None = None
    chip_id: int | None = None
    for line in text.splitlines():
        match = _USAGE_LINE.match(line)
        if not match:
            continue
        key = re.sub(r"\s+", " ", match.group(1)).strip().casefold()
        value = match.group(2).strip()
        if key == "npu id":
            try:
                npu_id = int(value)
            except ValueError:
                block = None
                continue
            block = usages.setdefault(npu_id, {"aicore_percent": None, "hbm_percent": None, "chips": {}})
            chip_id = None
            continue
        if block is None:
            continue
        if key == "chip id":
            try:
                chip_id = int(value)
            except ValueError:
                chip_id = None
            continue
        number = _number(value)
        if number is None:
            continue
        target = block if chip_id is None else block["chips"].setdefault(chip_id, {})
        if key.startswith("aicore usage rate"):
            target["aicore_percent"] = number
        elif key.startswith("hbm usage rate"):
            target["hbm_percent"] = number
    return usages


def apply_usage_overrides(devices: list[dict[str, Any]], usages: dict[int, dict[str, Any]]) -> None:
    """Prefer the utilization counters from ``-t usages`` when present."""
    for device in devices:
        usage = usages.get(int(device["npu_id"]))
        if not usage:
            continue
        chips = device.get("chips") or []
        applied = False
        for chip in chips:
            override = usage["chips"].get(chip["chip_id"])
            if override and override.get("aicore_percent") is not None:
                chip["aicore_percent"] = override["aicore_percent"]
                applied = True
        if applied:
            utilizations = [chip["aicore_percent"] for chip in chips if chip.get("aicore_percent") is not None]
            device["aicore_percent"] = max(utilizations) if utilizations else device.get("aicore_percent")
        elif usage.get("aicore_percent") is not None:
            device["aicore_percent"] = usage["aicore_percent"]


def parse_npu(info: str, usages: str = "") -> dict[str, Any]:
    parsed = parse_npu_smi_info(info)
    if not parsed["devices"]:
        raise ValueError("npu-smi output contained no recognizable NPU devices")
    apply_usage_overrides(parsed["devices"], parse_npu_smi_usages(usages))
    return parsed
