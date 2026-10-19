"""Live read-only A2 check for the `npu-smi info -t common` frequency pass."""
from __future__ import annotations


import pytest



@pytest.mark.live_npu
def test_live_a2_common_frequency_is_observed_not_inferred(standing_npu_snapshots) -> None:
    server, snapshot = standing_npu_snapshots['173.125.1.2']
    assert snapshot["frequency_probe"]["state"] == "observed", snapshot["frequency_probe"]
    for device in snapshot["devices"]:
        assert device["freq_mhz"] is not None
        assert device["freq_max_mhz"] >= device["freq_mhz"]
        # The common query itself has no throttle flag; DCMI remains a separate observation.
        assert "throttled" not in device
        assert "throttling_detected" not in device
