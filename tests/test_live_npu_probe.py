"""Live non-destructive NPU probe tests against real Ascend A2 and A3 hosts."""
from __future__ import annotations


import pytest

from npu_top.agent_view import npu_status, server_status


@pytest.mark.live_npu
def test_live_a2_173_125_1_2_probe(standing_npu_snapshots) -> None:
    server, snapshot = standing_npu_snapshots['173.125.1.2']
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
def test_live_a3_192_168_13_165_idle_probe(standing_npu_snapshots) -> None:
    server, snapshot = standing_npu_snapshots['192.168.13.165']
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
def test_live_a3_192_168_13_154_probe(standing_npu_snapshots) -> None:
    server, snapshot = standing_npu_snapshots['192.168.13.154']
    assert snapshot["summary"]["npu_count"] == 8
    assert snapshot["summary"]["hbm_total_mb"] == 8 * 131072
    for idx, dev in enumerate(snapshot["devices"]):
        assert dev["npu_id"] == idx
        assert dev["name"] == "Ascend910"
        assert [c["phy_id"] for c in dev["chips"]] == [2 * idx, 2 * idx + 1]
        for proc in dev.get("processes", []):
            if "chip_id" in proc:
                assert proc["phy_id"] == 2 * idx + proc["chip_id"]
    # Standing hosts may be idle; validate the observed projection, not a workload.
    status_view = npu_status(server, snapshot, include_processes=True, detailed_processes=True)
    assert status_view["summary"]["busy_npu_count"] == sum(bool(dev["busy"]) for dev in snapshot["devices"])
