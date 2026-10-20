"""Live read-only A2 check for DCMI AICORE_FREQREDUC_CAUSE."""
from __future__ import annotations


import pytest



@pytest.mark.live_npu
def test_live_a2_dcmi_freq_reduc_cause_is_decoded(standing_npu_snapshots) -> None:
    server, snapshot = standing_npu_snapshots['173.125.1.2']
    dcmi = snapshot["dcmi"]
    assert dcmi["state"] == "observed"
    assert dcmi["enum_source"] == "header"
    assert dcmi["main_cmd_lp"] == 8
    assert dcmi["lp_sub_cmd_aicore_freqreduc_cause"] == 9
    assert dcmi["init_ret"] == 0
    for card in range(8):
        record = dcmi["cards"][str(card)]
        assert record["state"] == "observed"
        assert record["ret"] == 0
        assert record["size_bytes"] == 8
        assert isinstance(record["raw_mask"], int)
        assert isinstance(record["idle"], bool)
        assert isinstance(record["thermal_limit"], bool)
        assert isinstance(record["power_limit"], bool)
        assert record["unknown_bits"] == []
        if record["idle"]:
            assert 0 in record["bits_set"]
        if not record["thermal_limit"]:
            assert not any(reason["bit"] == 2 for reason in record["reasons"])
    assert snapshot["devices"][0]["dcmi_freq_reduc"]["card_id"] == 0
