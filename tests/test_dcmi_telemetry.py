"""Contracts for the read-only DCMI AICORE_FREQREDUC_CAUSE decoder."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from npu_top import dcmi_telemetry


class DcmiEnumTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "dcmi_interface_api_enum_excerpt.txt"

    def test_installed_header_values_match_module_constants(self) -> None:
        text = self.fixture.read_text(encoding="utf-8")
        main_cmd = dcmi_telemetry.parse_enum_values(text, "dcmi_main_cmd")
        lp_sub = dcmi_telemetry.parse_enum_values(text, "DCMI_LP_SUB_CMD")
        self.assertEqual(main_cmd["DCMI_MAIN_CMD_LP"], dcmi_telemetry.DCMI_MAIN_CMD_LP)
        self.assertEqual(
            lp_sub["DCMI_LP_SUB_CMD_AICORE_FREQREDUC_CAUSE"],
            dcmi_telemetry.DCMI_LP_SUB_CMD_AICORE_FREQREDUC_CAUSE,
        )


class DcmiMaskDecodeTests(unittest.TestCase):
    def test_zero_mask_is_observed_rated_frequency(self) -> None:
        decoded = dcmi_telemetry.decode_cause_mask(0, ret=0, size_bytes=8)
        self.assertEqual(decoded["state"], "observed")
        self.assertEqual(decoded["raw_mask"], 0)
        self.assertEqual(decoded["bits_set"], [])
        self.assertEqual(decoded["reasons"], [])
        self.assertFalse(decoded["idle"])
        self.assertFalse(decoded["thermal_limit"])
        self.assertFalse(decoded["power_limit"])
        self.assertFalse(decoded["svfd"])

    def test_idle_bit_is_not_thermal_or_power_limiting(self) -> None:
        decoded = dcmi_telemetry.decode_cause_mask(1, ret=0, size_bytes=8)
        self.assertTrue(decoded["idle"])
        self.assertFalse(decoded["thermal_limit"])
        self.assertFalse(decoded["power_limit"])
        self.assertEqual([reason["name"] for reason in decoded["reasons"]], ["IDLE"])

    def test_thermal_and_power_bits_are_explicit(self) -> None:
        decoded = dcmi_telemetry.decode_cause_mask(
            (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 10),
            ret=0, size_bytes=8,
        )
        self.assertTrue(decoded["thermal_limit"])
        self.assertTrue(decoded["power_limit"])
        self.assertFalse(decoded["idle"])
        self.assertEqual(
            [reason["name"] for reason in decoded["reasons"]],
            ["THERMAL", "SW_EDP", "HW_EDP", "POWER_BREAK", "POWERCAPPING"],
        )

    def test_svfd_is_reported_separately_from_power_cap(self) -> None:
        decoded = dcmi_telemetry.decode_cause_mask(1 << 8, ret=0, size_bytes=8)
        self.assertTrue(decoded["svfd"])
        self.assertFalse(decoded["power_limit"])
        self.assertFalse(decoded["thermal_limit"])

    def test_unknown_bits_are_preserved(self) -> None:
        decoded = dcmi_telemetry.decode_cause_mask((1 << 0) | (1 << 7) | (1 << 63), ret=0, size_bytes=8)
        self.assertEqual(decoded["unknown_bits"], [7, 63])
        self.assertEqual(
            [reason["name"] for reason in decoded["reasons"]],
            ["IDLE", "UNKNOWN", "UNKNOWN"],
        )
        self.assertTrue(decoded["idle"])

    def test_failed_or_short_read_is_null_not_false(self) -> None:
        failed = dcmi_telemetry.decode_cause_mask(0, ret=-1, size_bytes=8)
        short = dcmi_telemetry.decode_cause_mask(0, ret=0, size_bytes=4)
        for decoded in (failed, short):
            self.assertEqual(decoded["state"], "unavailable")
            for key in ("raw_mask", "idle", "thermal_limit", "power_limit", "svfd"):
                self.assertIsNone(decoded[key])


class DcmiAggregateTests(unittest.TestCase):
    def test_all_cards_observed_reports_false_flags(self) -> None:
        records = {
            "0": dcmi_telemetry.decode_cause_mask(1, ret=0),
            "1": dcmi_telemetry.decode_cause_mask(0, ret=0),
        }
        aggregate = dcmi_telemetry.aggregate_cards(records)
        self.assertEqual(aggregate["state"], "observed")
        self.assertFalse(aggregate["thermal_limit"])
        self.assertFalse(aggregate["power_limit"])
        self.assertFalse(aggregate["idle"])  # card 1 is rated, not idle

    def test_partial_cards_leave_unknown_top_level_flags_null(self) -> None:
        records = {
            "0": dcmi_telemetry.decode_cause_mask(1, ret=0),
            "1": dcmi_telemetry.decode_cause_mask(0, ret=-1),
        }
        aggregate = dcmi_telemetry.aggregate_cards(records)
        self.assertEqual(aggregate["state"], "partial")
        self.assertIsNone(aggregate["thermal_limit"])
        self.assertIsNone(aggregate["power_limit"])
        self.assertIsNone(aggregate["idle"])
        self.assertIsNone(aggregate["cards"]["1"]["thermal_limit"])

    def test_observed_thermal_wins_even_with_unknown_peer(self) -> None:
        records = {
            "0": dcmi_telemetry.decode_cause_mask(1 << 2, ret=0),
            "1": dcmi_telemetry.decode_cause_mask(0, ret=-1),
        }
        aggregate = dcmi_telemetry.aggregate_cards(records)
        self.assertTrue(aggregate["thermal_limit"])
        self.assertIsNone(aggregate["power_limit"])

    def test_missing_library_is_unsupported_with_null_fields(self) -> None:
        payload = dcmi_telemetry.run_probe([0], header_path=str(DcmiEnumTests.fixture),
                                         library_path="/nonexistent/libdcmi.so")
        self.assertEqual(payload["state"], "unsupported")
        self.assertIsNone(payload["cards"]["0"]["thermal_limit"])
        self.assertIsNone(payload["cards"]["0"]["raw_mask"])

    def test_unverified_abi_never_loads_library(self) -> None:
        with patch.object(dcmi_telemetry.ctypes, "CDLL") as loader:
            payload = dcmi_telemetry.run_probe([0], header_path="/nonexistent/dcmi.h")
        loader.assert_not_called()
        self.assertEqual(payload["state"], "unavailable")
        self.assertIsNone(payload["main_cmd_lp"])

    def test_failed_init_never_reads_device(self) -> None:
        class Function:
            def __init__(self, result):
                self.result, self.calls = result, 0

            def __call__(self, *args):
                self.calls += 1
                return self.result

        class Library:
            dcmi_init = Function(-1)
            dcmi_get_device_info = Function(0)

        library = Library()
        with patch.object(dcmi_telemetry.ctypes, "CDLL", return_value=library):
            payload = dcmi_telemetry.run_probe([0], header_path=str(DcmiEnumTests.fixture))
        self.assertEqual(library.dcmi_get_device_info.calls, 0)
        self.assertEqual(payload["cards"]["0"]["error_type"], "DCMI_INIT_ERROR")
        self.assertIsNone(payload["cards"]["0"]["raw_mask"])


if __name__ == "__main__":
    unittest.main()
