"""Live non-destructive NPU probe tests against real Ascend A2 and A3 hosts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from npu_top import npu_smi
from npu_top.agent_view import npu_status, server_status
from npu_top.probe import HostProbe


class LiveDefaultSshAdapter:
    """Read-only SSH adapter using invoking user's default SSH keys with ControlMaster=no."""

    def __init__(self, working_dir: Path) -> None:
        self.project_root = working_dir

    def preflight(self, server: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "category": "ssh_config_valid", "error": None}

    def ssh_base(self, server: dict[str, Any], *, batch_mode: bool = True) -> list[str]:
        return [
            "ssh",
            "-T",
            "-o",
            "ControlMaster=no",
            "-o",
            f"BatchMode={'yes' if batch_mode else 'no'}",
            "-o",
            "ConnectTimeout=5",
            "-p",
            str(server.get("port", 22)),
            f"{server.get('username', 'root')}@{server['host']}",
        ]

    def parse_npu(self, info: str, usages: str) -> dict[str, Any]:
        return npu_smi.parse_npu(info, usages)


@pytest.mark.live_npu
def test_live_a2_173_125_1_2_probe(tmp_path: Path) -> None:
    probe = HostProbe(LiveDefaultSshAdapter(tmp_path), timeout=8)
    server = {"id": "a2-2", "name": "a2-173-125-1-2", "host": "173.125.1.2", "port": 22, "username": "root", "enabled": True, "tags": ["a2"]}
    snapshot = probe.collect(server, include_infrastructure=True)
    assert snapshot["summary"]["npu_count"] == 8
    assert snapshot["summary"]["hbm_total_mb"] == 8 * 32768
    for idx, dev in enumerate(snapshot["devices"]):
        assert dev["npu_id"] == idx
        assert dev["name"] == "910B4"
        assert dev["health"] == "OK"
        assert dev["hbm"]["total_mb"] == 32768
        assert dev["power_w"] is not None
    status_view = npu_status(server, snapshot, include_processes=True, detailed_processes=True)
    assert status_view["summary"]["npu_count"] == 8


@pytest.mark.live_npu
def test_live_a3_192_168_13_165_idle_probe(tmp_path: Path) -> None:
    probe = HostProbe(LiveDefaultSshAdapter(tmp_path), timeout=8)
    server = {"id": "a3-165", "name": "a3-192-168-13-165", "host": "192.168.13.165", "port": 22, "username": "root", "enabled": True, "tags": ["a3"]}
    snapshot = probe.collect(server, include_infrastructure=True)
    assert snapshot["summary"]["npu_count"] == 8
    assert snapshot["summary"]["hbm_total_mb"] == 8 * 131072
    for idx, dev in enumerate(snapshot["devices"]):
        assert dev["npu_id"] == idx
        assert dev["name"] == "Ascend910"
        assert dev["health"] == "OK"
        assert dev["hbm"]["total_mb"] == 131072
        assert dev["power_w"] is not None
        assert [c["chip_id"] for c in dev["chips"]] == [0, 1]
        assert [c["phy_id"] for c in dev["chips"]] == [2 * idx, 2 * idx + 1]
    srv_view = server_status(server, snapshot, include_processes=True, detailed_processes=True, include_infrastructure=True)
    assert srv_view["summary"]["npu_count"] == 8


@pytest.mark.live_npu
def test_live_a3_192_168_13_154_busy_probe(tmp_path: Path) -> None:
    probe = HostProbe(LiveDefaultSshAdapter(tmp_path), timeout=8)
    server = {"id": "a3-154", "name": "a3-192-168-13-154", "host": "192.168.13.154", "port": 22, "username": "root", "enabled": True, "tags": ["a3"]}
    snapshot = probe.collect(server, include_infrastructure=True)
    assert snapshot["summary"]["npu_count"] == 8
    assert snapshot["summary"]["hbm_total_mb"] == 8 * 131072
    assert snapshot["summary"]["busy_npu_count"] >= 1
    for idx, dev in enumerate(snapshot["devices"]):
        assert dev["npu_id"] == idx
        assert dev["name"] == "Ascend910"
        assert [c["phy_id"] for c in dev["chips"]] == [2 * idx, 2 * idx + 1]
    all_procs = [proc for dev in snapshot["devices"] for proc in dev.get("processes", [])]
    assert len(all_procs) >= 1
    assert all(proc.get("phy_id") in range(16) for proc in all_procs if "chip_id" in proc)
