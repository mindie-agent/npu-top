"""Real-capture contracts for the initialize-once DCMI metrics sampler."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from npu_top import dcmi_metrics, dcmi_telemetry


# Values captured from A2 card 0 through the installed libdcmi ABI.
CAPTURE = {
    "current_freq": 800, "rated_freq": 1650, "power_raw": 916,
    "temperature": 44, "aicore_util": 0, "hbm_util": 8, "npu_util": 0,
    "hbm": {"memory_size": 32768, "freq": 1600, "memory_usage": 2875,
            "temp": 41, "bandwith_util_rate": 0},
}
ABI = {
    "main_cmd_lp": 8, "lp_sub_cmd_aicore_freqreduc_cause": 9,
    "freq_type_aicore_current": 7, "freq_type_aicore_max": 9,
    "utilization_types": {
        "DCMI_UTILIZATION_RATE_AICORE": 2,
        "DCMI_UTILIZATION_RATE_HBM": 6,
        "DCMI_UTILIZATION_RATE_NPU": 13,
    },
    "utilization_available": True, "enum_source": "header",
}


class FakeFunction:
    def __init__(self, impl):
        self.impl = impl
        self.calls = 0
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls += 1
        return self.impl(*args)


class FakeLibrary:
    def __init__(self, init_ret: int = 0, cause_mask: int = 1, power_ret: int = 0):
        self.init = FakeFunction(lambda: init_ret)
        self.frequency = FakeFunction(self._frequency)
        self.power = FakeFunction(lambda card, device, out: (setattr(out._obj, "value", CAPTURE["power_raw"]), power_ret)[1])
        self.temperature = FakeFunction(lambda card, device, out: (setattr(out._obj, "value", CAPTURE["temperature"]), 0)[1])
        self.utilization = FakeFunction(self._utilization)
        self.hbm = FakeFunction(self._hbm)
        self.device_info = FakeFunction(lambda card, device, main, sub, buffer, size: self._cause(buffer, size, cause_mask))

    @staticmethod
    def _frequency(card, device, freq_type, out):
        out._obj.value = CAPTURE["current_freq"] if freq_type == ABI["freq_type_aicore_current"] else CAPTURE["rated_freq"]
        return 0

    @staticmethod
    def _utilization(card, device, input_type, out):
        values = {
            ABI["utilization_types"]["DCMI_UTILIZATION_RATE_AICORE"]: CAPTURE["aicore_util"],
            ABI["utilization_types"]["DCMI_UTILIZATION_RATE_HBM"]: CAPTURE["hbm_util"],
            ABI["utilization_types"]["DCMI_UTILIZATION_RATE_NPU"]: CAPTURE["npu_util"],
        }
        out._obj.value = values.get(input_type, 0)
        return 0

    @staticmethod
    def _hbm(card, device, out):
        info = out._obj
        info.memory_size = CAPTURE["hbm"]["memory_size"]
        info.freq = CAPTURE["hbm"]["freq"]
        info.memory_usage = CAPTURE["hbm"]["memory_usage"]
        info.temp = CAPTURE["hbm"]["temp"]
        info.bandwith_util_rate = CAPTURE["hbm"]["bandwith_util_rate"]
        return 0

    @staticmethod
    def _cause(buffer, size, mask):
        buffer._obj.value = mask
        size._obj.value = 8
        return 0


def as_library(fake: FakeLibrary):
    # configure_library/sample_card use the real C symbol names.
    fake.dcmi_init = fake.init
    fake.dcmi_get_device_frequency = fake.frequency
    fake.dcmi_get_device_power_info = fake.power
    fake.dcmi_get_device_temperature = fake.temperature
    fake.dcmi_get_device_utilization_rate = fake.utilization
    fake.dcmi_get_device_hbm_info = fake.hbm
    fake.dcmi_get_device_info = fake.device_info
    return fake


class DcmiMetricsCaptureTests(unittest.TestCase):
    def test_real_capture_units_and_translation(self) -> None:
        card = dcmi_metrics.sample_card(as_library(FakeLibrary()), dcmi_telemetry, 0, 0, ABI)
        getters = card["getters"]
        self.assertEqual(getters["current_freq"]["ret"], 0)
        self.assertEqual(getters["current_freq"]["raw"], 800)
        self.assertEqual(getters["current_freq"]["unit"], "MHz")
        self.assertEqual(getters["rated_freq"]["raw"], 1650)
        self.assertEqual(getters["power"]["raw"], 916)
        self.assertEqual(getters["power"]["raw_unit"], "0.1W")
        self.assertAlmostEqual(getters["power"]["value"], 91.6)
        self.assertEqual(getters["power"]["unit"], "W")
        self.assertEqual(getters["temperature"]["raw"], 44)
        self.assertEqual(getters["temperature"]["unit"], "C")
        self.assertEqual(getters["hbm_utilization"]["raw"], 8)
        self.assertEqual(getters["hbm_utilization"]["unit"], "%")
        hbm = getters["hbm_info"]["value"]
        self.assertEqual(hbm["memory_size"], {"raw": 32768, "unit": "MB", "value": 32768})
        self.assertEqual(hbm["freq"], {"raw": 1600, "unit": "MHz", "value": 1600})
        self.assertIsNone(hbm["memory_usage"]["unit"])
        self.assertIsNone(hbm["memory_usage"]["value"])

    def test_idle_bit_is_not_a_throttle_and_unknown_bits_are_visible(self) -> None:
        card = dcmi_metrics.sample_card(
            as_library(FakeLibrary(cause_mask=(1 << 0) | (1 << 7) | (1 << 63))),
            dcmi_telemetry, 0, 0, ABI,
        )
        cause = card["getters"]["freq_reduc_cause"]["decoded"]
        self.assertTrue(cause["idle"])
        self.assertFalse(cause["thermal_limit"])
        self.assertFalse(cause["power_limit"])
        self.assertEqual(cause["unknown_bits"], [7, 63])
        self.assertEqual([reason["name"] for reason in cause["reasons"]], ["IDLE", "UNKNOWN", "UNKNOWN"])
        self.assertIn("aicore_utilization", card["skipped"])
        self.assertIn("npu_utilization", card["skipped"])
        self.assertEqual(card["errors"], {})

    def test_failed_getter_is_null_not_zero_or_false(self) -> None:
        card = dcmi_metrics.sample_card(
            as_library(FakeLibrary(power_ret=-1)), dcmi_telemetry, 0, 0, ABI,
        )
        power = card["getters"]["power"]
        self.assertEqual(power["ret"], -1)
        self.assertIsNone(power["raw"])
        self.assertIsNone(power["value"])
        self.assertEqual(power["error"], "DCMI ret -1")
        self.assertEqual(card["errors"]["power"], "DCMI ret -1")

    def test_cause_failure_is_null_not_false(self) -> None:
        card = dcmi_metrics.sample_card(
            as_library(FakeLibrary(cause_mask=(1 << 63))),
            dcmi_telemetry, 0, 0, ABI,
        )
        cause = card["getters"]["freq_reduc_cause"]["decoded"]
        self.assertEqual(cause["unknown_bits"], [63])
        self.assertFalse(cause["thermal_limit"])
        self.assertFalse(cause["power_limit"])
        self.assertIs(cause["idle"], False)


class DcmiMetricsStartupTests(unittest.TestCase):
    def test_nonzero_init_aborts_before_getters(self) -> None:
        fake = as_library(FakeLibrary(init_ret=7))
        with mock.patch.object(dcmi_metrics.ctypes, "CDLL", return_value=fake):
            with self.assertRaises(dcmi_metrics.StartupError) as context:
                dcmi_metrics.configure_library("/tmp/libdcmi.so")
        self.assertEqual(context.exception.stage, "init")
        self.assertEqual(context.exception.detail["ret"], 7)
        self.assertEqual(fake.frequency.calls, 0)
        self.assertEqual(fake.power.calls, 0)
        self.assertEqual(fake.device_info.calls, 0)

    def test_unparseable_header_aborts_without_guessed_enum(self) -> None:
        with self.assertRaises(dcmi_metrics.StartupError) as context:
            dcmi_metrics.resolve_abi("not a DCMI header", dcmi_telemetry)
        self.assertEqual(context.exception.stage, "header")

    def test_missing_cause_module_is_explicit(self) -> None:
        with self.assertRaises(dcmi_metrics.StartupError) as context:
            dcmi_metrics.load_cause_module("/nonexistent/dcmi_telemetry.py")
        self.assertEqual(context.exception.stage, "cause_module")

    def test_real_cause_module_loads_without_guessing(self) -> None:
        path = Path(__file__).resolve().parents[1] / "npu_top" / "dcmi_telemetry.py"
        module, resolved, digest = dcmi_metrics.load_cause_module(str(path))
        self.assertEqual(resolved, str(path))
        self.assertTrue(digest)
        self.assertIsNone(module.resolve_enum_values(None)[0])

    def test_cause_module_with_guessed_enums_is_rejected(self) -> None:
        source = (
            "def resolve_enum_values(text):\n"
            "    return 8, 9, 'guessed', None\n"
            "def parse_enum_values(text, name):\n"
            "    return {}\n"
            "def decode_cause_mask(mask, **kwargs):\n"
            "    return {}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cause.py"
            path.write_text(source)
            with self.assertRaises(dcmi_metrics.StartupError) as context:
                dcmi_metrics.load_cause_module(str(path))
        self.assertEqual(context.exception.stage, "cause_module")


if __name__ == "__main__":
    unittest.main()
