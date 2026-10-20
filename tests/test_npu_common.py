"""Contracts for the optional `npu-smi info -t common` AICore-frequency probe."""
from __future__ import annotations

import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from npu_top.npu_smi import parse_npu_common
from npu_top.probe import HostProbe, build_frequency_script, frequency_probe_plan


FAST_OUTPUT = (
    "__NFM_SECTION__proc_stat\n"
    "cpu  100 0 50 850 0 0 0 0\n"
    "__NFM_SECTION__npu_info_rc\n"
    "0\n"
)
COMMON_OUTPUT = (
    "__NFM_SECTION__common_0\n"
    "\tNPU ID                         : 0\n"
    "\tChip Count                     : 1\n"
    "\n"
    "\tChip ID                        : 0\n"
    "\tAicore Freq(MHZ)               : 1650\n"
    "\tAicore curFreq(MHZ)            : 800\n"
    "\tTemperature(C)                 : 43\n"
    "\tNPU Real-time Power(W)         : 91.0\n"
    "\n"
    "\tChip Name                      : mcu\n"
    "\tTemperature(C)                 : 38\n"
    "__NFM_SECTION__common_rc_0\n0\n"
    "__NFM_SECTION__common_1\n"
    "\tNPU ID                         : 1\n"
    "\tChip Count                     : 1\n"
    "\n"
    "\tChip ID                        : 0\n"
    "\tAicore Freq(MHZ)               : 1650\n"
    "\tTemperature(C)                 : 41\n"
    "__NFM_SECTION__common_rc_1\n0\n"
)
COMMON_ALL_OUTPUT = COMMON_OUTPUT.replace(
    "\tAicore Freq(MHZ)               : 1650\n\tTemperature(C)                 : 41\n",
    "\tAicore Freq(MHZ)               : 1650\n\tAicore curFreq(MHZ)            : 800\n"
    "\tTemperature(C)                 : 41\n",
)
COMMON_RC_FAILURE = COMMON_ALL_OUTPUT.replace(
    "__NFM_SECTION__common_rc_1\n0\n", "__NFM_SECTION__common_rc_1\n1\n",
).replace(
    "\tAicore Freq(MHZ)               : 1650\n\tTemperature(C)                 : 41\n",
    "\tAicore Freq(MHZ)               : 1650\n\tTemperature(C)                 : 41\n",
)
DCMI_OUTPUT = json.dumps({
    "source": "dcmi", "state": "observed", "stage": "dcmi",
    "thermal_limit": False, "power_limit": False, "idle": True,
    "unknown_bits": [], "cards": {
        "0": {
            "state": "observed", "ret": 0, "size_bytes": 8,
            "raw_mask": 1, "raw_mask_hex": "0x0000000000000001",
            "bits_set": [0], "reasons": [{"bit": 0, "name": "IDLE", "category": "idle"}],
            "unknown_bits": [], "idle": True, "thermal_limit": False,
            "power_limit": False, "svfd": False,
        },
        "1": {
            "state": "observed", "ret": 0, "size_bytes": 8,
            "raw_mask": 1, "raw_mask_hex": "0x0000000000000001",
            "bits_set": [0], "reasons": [{"bit": 0, "name": "IDLE", "category": "idle"}],
            "unknown_bits": [], "idle": True, "thermal_limit": False,
            "power_limit": False, "svfd": False,
        },
    },
})


def _device(npu_id: int) -> dict:
    return {
        "npu_id": npu_id, "name": "910B4", "health": "OK", "power_w": 91.0,
        "temperature_c": 43, "bus_id": "0000:C1:00.0", "aicore_percent": 0,
        "memory": {"used_mb": 0, "total_mb": 0},
        "hbm": {"used_mb": 2875, "total_mb": 32768},
        "processes": [],
    }


class FakeAdapter:
    project_root = Path(".")
    devices = [_device(0), _device(1)]

    def preflight(self, server):
        return {"ok": True, "category": "ssh_config_valid", "error": None}

    def ssh_base(self, server, *, batch_mode=True):
        return []

    def parse_npu(self, info, usages):
        return {"devices": copy.deepcopy(self.devices), "process_records": []}


def _dcmi_reply(output: str = DCMI_OUTPUT) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, output, "")


class NpuCommonParserTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "a2-common-card0.txt"

    def test_a2_card_capture_maps_current_and_rated_frequency(self) -> None:
        parsed = parse_npu_common(self.fixture.read_text())
        self.assertEqual(sorted(parsed), [0])
        self.assertEqual(parsed[0]["freq_mhz"], 800.0)
        self.assertEqual(parsed[0]["freq_max_mhz"], 1650.0)
        self.assertEqual(parsed[0]["chips"][0]["freq_mhz"], 800.0)

    def test_mcu_temperature_never_becomes_frequency(self) -> None:
        parsed = parse_npu_common("NPU ID : 0\nChip ID : 0\nChip Name : mcu\nTemperature(C) : 38\n")
        self.assertIsNone(parsed[0]["freq_mhz"])
        self.assertIsNone(parsed[0]["freq_max_mhz"])

    def test_multi_chip_card_reports_maximum_current_frequency(self) -> None:
        parsed = parse_npu_common(
            "NPU ID : 0\nChip Count : 2\nChip ID : 0\nAicore curFreq(MHZ) : 800\n"
            "Chip ID : 1\nAicore curFreq(MHZ) : 1600\nAicore Freq(MHZ) : 1650\n"
        )
        self.assertEqual(parsed[0]["freq_mhz"], 1600.0)
        self.assertEqual(parsed[0]["freq_max_mhz"], 1650.0)
        self.assertEqual(parsed[0]["chips"][1]["freq_mhz"], 1600.0)

    def test_rated_frequency_is_not_substituted_for_missing_current_frequency(self) -> None:
        parsed = parse_npu_common("NPU ID : 0\nChip ID : 0\nAicore Freq(MHZ) : 1650\n")
        self.assertIsNone(parsed[0]["freq_mhz"])
        self.assertEqual(parsed[0]["freq_max_mhz"], 1650.0)

    def test_non_numeric_values_and_empty_output_stay_absent(self) -> None:
        self.assertIsNone(parse_npu_common("NPU ID : 0\nAicore curFreq(MHZ) : NA\n")[0]["freq_mhz"])
        self.assertEqual(parse_npu_common(""), {})


class FrequencyScriptTests(unittest.TestCase):
    def test_script_only_contains_validated_integer_cards(self) -> None:
        script = build_frequency_script([3, 0, 3, True, -1, "0; rm -rf /"])  # type: ignore[list-item]
        self.assertIn("for nfm_card in 0 3; do", script)
        self.assertNotIn("rm -rf", script)
        self.assertIn("timeout 2 npu-smi info -t common -i", script)
        self.assertIn("-ge 8", script)
        self.assertIn("__NFM_SECTION__common_rc_", script)
        self.assertIn('exit "$nfm_rc_all"', script)
        self.assertNotIn("\nexit 0\n", script)

    def test_script_budget_can_be_shrunk_without_truncating_cards(self) -> None:
        script = build_frequency_script([0, 1], budget_seconds=5)
        self.assertIn("-ge 5", script)
        self.assertIn("timeout 2", script)

    def test_empty_ids_produce_no_script(self) -> None:
        self.assertEqual(build_frequency_script([]), "")

    def test_outer_timeout_is_inner_budget_plus_card_plus_grace(self) -> None:
        self.assertEqual(frequency_probe_plan(20), (8, 13))
        self.assertEqual(frequency_probe_plan(10), (5, 10))
        self.assertIsNone(frequency_probe_plan(5))


class ProbeFrequencyMergeTests(unittest.TestCase):
    def _probe(self, timeout: int = 20) -> HostProbe:
        return HostProbe(FakeAdapter(), timeout=timeout)

    def test_missing_card_downgrades_status_to_partial(self) -> None:
        probe = self._probe()
        calls: list[tuple[str, int]] = []

        def fake_run_script(_server, script, timeout):
            calls.append((script, timeout))
            if len(calls) == 1:
                return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")
            return subprocess.CompletedProcess([], 0, COMMON_OUTPUT, "")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply()):
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], 13)
        self.assertIn("-ge 8", calls[1][0])
        self.assertEqual(snapshot["devices"][0]["freq_mhz"], 800.0)
        self.assertEqual(snapshot["devices"][0]["freq_max_mhz"], 1650.0)
        self.assertNotIn("freq_mhz", snapshot["devices"][1])
        self.assertEqual(snapshot["frequency_probe"]["state"], "partial")
        self.assertEqual(snapshot["frequency_probe"]["missing_cards"], [1])
        self.assertEqual(snapshot["frequency_probe"]["card_exit_codes"], {"0": 0, "1": 0})
        self.assertEqual(snapshot["frequency_probe"]["failed_cards"], [])

    def test_all_cards_observed_is_observed(self) -> None:
        probe = self._probe()
        calls: list[int] = []

        def fake_run_script(_server, script, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")
            return subprocess.CompletedProcess([], 0, COMMON_ALL_OUTPUT, "")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply()):
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(snapshot["frequency_probe"]["state"], "observed")
        self.assertEqual(snapshot["frequency_probe"]["missing_cards"], [])
        self.assertEqual(snapshot["frequency_probe"]["failed_cards"], [])

    def test_nonzero_card_exit_code_is_preserved_and_partial(self) -> None:
        probe = self._probe()
        calls: list[int] = []

        def fake_run_script(_server, script, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")
            return subprocess.CompletedProcess([], 1, COMMON_RC_FAILURE, "card 1 failed")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply()):
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        status = snapshot["frequency_probe"]
        self.assertEqual(status["state"], "partial")
        self.assertEqual(status["card_exit_codes"], {"0": 0, "1": 1})
        self.assertEqual(status["failed_cards"], [1])
        self.assertEqual(snapshot["devices"][0]["frequency_exit_code"], 0)
        self.assertEqual(snapshot["devices"][1]["frequency_exit_code"], 1)
        self.assertNotIn("freq_mhz", snapshot["devices"][1])
        self.assertNotIn("freq_max_mhz", snapshot["devices"][1])
        self.assertEqual(status["observed_cards"], [0])
        self.assertEqual(status["missing_cards"], [1])

    def test_frequency_timeout_degrades_without_failing_the_probe(self) -> None:
        probe = self._probe()
        calls: list[str] = []

        def fake_run_script(_server, script, timeout):
            calls.append(script)
            if len(calls) == 1:
                return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")
            raise subprocess.TimeoutExpired("ssh", timeout)

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply()):
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(snapshot["summary"]["npu_count"], 2)
        self.assertNotIn("freq_mhz", snapshot["devices"][0])
        self.assertEqual(snapshot["frequency_probe"]["state"], "unavailable")
        self.assertEqual(snapshot["frequency_probe"]["error_type"], "TimeoutExpired")

    def test_frequency_timeout_salvages_complete_earlier_sections(self) -> None:
        probe = self._probe()
        calls: list[str] = []

        def fake_run_script(_server, script, timeout):
            calls.append(script)
            if len(calls) == 1:
                return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")
            raise subprocess.TimeoutExpired("ssh", timeout, output=COMMON_OUTPUT.encode())

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply()):
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(snapshot["devices"][0]["freq_mhz"], 800.0)
        self.assertNotIn("freq_mhz", snapshot["devices"][1])
        self.assertEqual(snapshot["frequency_probe"]["state"], "partial")
        self.assertEqual(snapshot["frequency_probe"]["error_type"], "TimeoutExpired")
        self.assertTrue(snapshot["frequency_probe"]["timed_out"])
        self.assertEqual(snapshot["frequency_probe"]["missing_cards"], [1])

    def test_configured_timeout_too_small_skips_optional_passes(self) -> None:
        probe = self._probe(timeout=5)
        calls: list[str] = []

        def fake_run_script(_server, script, timeout):
            calls.append(script)
            return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python") as python_run:
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(len(calls), 1)
        python_run.assert_not_called()
        self.assertEqual(snapshot["frequency_probe"]["state"], "unavailable")
        self.assertEqual(snapshot["frequency_probe"]["error_type"], "ConfiguredTimeoutTooSmall")
        self.assertEqual(snapshot["dcmi"]["state"], "unavailable")
        self.assertEqual(snapshot["dcmi"]["error_type"], "ConfiguredTimeoutTooSmall")

    def test_no_npu_devices_skip_both_optional_round_trips(self) -> None:
        probe = self._probe()
        probe.adapter.devices = []
        calls: list[str] = []

        def fake_run_script(_server, script, timeout):
            calls.append(script)
            return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python") as python_run:
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(len(calls), 1)
        python_run.assert_not_called()
        self.assertEqual(snapshot["frequency_probe"], {"state": "not_applicable"})
        self.assertEqual(snapshot["dcmi"], {"state": "not_applicable", "stage": "dcmi"})

    def test_dcmi_observed_is_merged_per_device(self) -> None:
        probe = self._probe()
        calls: list[int] = []

        def fake_run_script(_server, script, timeout):
            calls.append(timeout)
            return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply()) as python_run:
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        python_run.assert_called_once()
        self.assertEqual(python_run.call_args.args[2], ["--cards", "0,1", "--device", "0"])
        self.assertEqual(python_run.call_args.args[3], 8)
        self.assertIn("AICORE_FREQREDUC_CAUSE", python_run.call_args.args[1])
        self.assertEqual(snapshot["dcmi"]["state"], "observed")
        self.assertEqual(snapshot["devices"][0]["dcmi_freq_reduc"]["raw_mask"], 1)
        self.assertTrue(snapshot["devices"][0]["dcmi_freq_reduc"]["idle"])
        self.assertFalse(snapshot["devices"][0]["dcmi_freq_reduc"]["thermal_limit"])

    def test_dcmi_invalid_json_degrades_without_failing_the_probe(self) -> None:
        probe = self._probe()
        calls: list[int] = []

        def fake_run_script(_server, script, timeout):
            calls.append(timeout)
            return subprocess.CompletedProcess([], 0, FAST_OUTPUT, "")

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script), \
             mock.patch.object(probe, "_run_python", return_value=_dcmi_reply("not json")):
            snapshot = probe.collect({"id": "a2", "name": "a2", "host": "127.0.0.1"}, False)

        self.assertEqual(snapshot["summary"]["npu_count"], 2)
        self.assertEqual(snapshot["dcmi"]["state"], "unavailable")
        self.assertEqual(snapshot["dcmi"]["error_type"], "JSONDecodeError")


if __name__ == "__main__":
    unittest.main()
