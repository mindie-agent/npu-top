"""Telemetry-window HTTP route, MCP contract, and full-capture replay tests.

The integration fixtures are the repository's captured A2/A3/A5 `npu-smi` text,
not hand-written snapshot mocks. HTTP tests exercise the real loopback server and
SQLite store; replay tests are opt-in through `MINDIE_TEST_DCMI_CAPTURE`.
"""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from npu_top.api import App, AppServer
from npu_top.db import Database
from npu_top.mcp import handle_request
from npu_top.npu_smi import parse_npu_smi_info
from npu_top.settings import Settings
from npu_top.telemetry_window import build_window


FIXTURES = Path(__file__).parent / "fixtures"


def captured_devices(name: str) -> list[dict]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return parse_npu_smi_info(text)["devices"]


def snapshot_at(timestamp: int, devices: list[dict]) -> dict:
    return {
        "collected_at": timestamp,
        "summary": {
            "npu_count": len(devices),
            "busy_npu_count": 0,
            "npu_util_percent": 0.0,
            "hbm_used_mb": sum(int((device.get("hbm") or {}).get("used_mb") or 0) for device in devices),
            "hbm_total_mb": sum(int((device.get("hbm") or {}).get("total_mb") or 0) for device in devices),
        },
        "devices": devices,
    }


class StubScheduler:
    def snapshots(self) -> dict:
        return {}

    def runtime_state(self) -> dict:
        return {
            "mode": "idle", "effective_interval": 120, "idle_interval": 120,
            "history_interval": 30, "active_viewers": 0, "collecting": False,
            "last_cycle_at": None, "cycle_duration_ms": None, "allowed_intervals": [1, 5, 10, 30],
        }


class StubAdapter:
    def validate_endpoint(self, host, port, username) -> None:
        return None


DCMI_FIXTURE = FIXTURES / "dcmi-metrics-slice.jsonl"


def dcmi_scheduled_timestamps(path: Path = DCMI_FIXTURE) -> list[int]:
    timestamps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") != "sample":
            continue
        timestamps.append(int(record["wall_before_ns"]) - int(record.get("schedule_slip_ns") or 0))
    return timestamps


def dcmi_sample_timestamps(path: Path = DCMI_FIXTURE) -> list[int]:
    timestamps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "sample":
            timestamps.append(int(record["wall_before_ns"]))
    return timestamps


class TelemetryWindowHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db = Database(self.state / "test.sqlite3")
        self.db.initialize()
        self.db.upsert_server({
            "id": "s1", "name": "a2-lab", "host": "192.0.2.10", "port": 22,
            "username": "root", "tags": ["A2"],
        })
        self.start = 1_700_000_000 // 900 * 900
        devices = captured_devices("a2-173-125-1-2.txt")
        self.db.record_success("s1", snapshot_at(self.start, devices), True)
        self.db.record_success("s1", snapshot_at(self.start + 30, devices), True)
        settings = Settings(self.state, self.state, "127.0.0.1", 8789, 120, 30, 60, 90, 4, 12, 8192)
        self.app = App(settings, self.db, StubAdapter(), StubScheduler())  # type: ignore[arg-type]
        self.app.web_root = self.state
        self.server = AppServer(("127.0.0.1", 0), self.app)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.db.close()
        self.temp.cleanup()

    def request(self, path: str) -> tuple[int, str | None, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = json.loads(response.read().decode())
            return response.status, response.getheader("X-MindIE-Top-Contract"), body
        finally:
            connection.close()

    def test_get_route_returns_observation_enveloped_window(self) -> None:
        status, contract, payload = self.request(
            f"/api/agent/telemetry-window?host=192.0.2.10&devices=0,1"
            f"&start_ts={self.start}&end_ts={self.start + 30}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(contract, "observation-only")
        self.assertEqual(payload["source"], "sqlite")
        self.assertEqual(payload["observation"]["allocation_authority"], False)
        self.assertEqual([device["npu_id"] for device in payload["devices"]], [0, 1])
        self.assertIsNone(payload["throttling_detected"])
        self.assertIsNone(payload["resource_interference_detected"])
        self.assertIsNone(payload["freq_mhz_min"])
        self.assertEqual(payload["window"]["start_ts"], self.start)
        self.assertEqual(payload["window"]["end_ts"], self.start + 30)
        self.assertIsNone(payload["devices"][0]["coverage_ratio"])
        self.assertEqual(payload["window"]["coverage_basis"], "schedule_unavailable")

    def test_fractional_query_strings_preserve_ns_window(self) -> None:
        ingested = self.db.ingest_dcmi_jsonl(DCMI_FIXTURE, "s1")
        scheduled = dcmi_scheduled_timestamps()
        start_ns = ingested["first_scheduled_ts_ns"]
        end_ns = ingested["last_scheduled_ts_ns"]
        self.assertEqual(start_ns, min(scheduled))
        self.assertEqual(end_ns, max(scheduled))
        start = Decimal(start_ns) / Decimal(1_000_000_000)
        end = Decimal(end_ns) / Decimal(1_000_000_000)
        status, contract, payload = self.request(
            f"/api/agent/telemetry-window?host=192.0.2.10&devices=0"
            f"&start_ts={start:f}&end_ts={end:f}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(contract, "observation-only")
        self.assertEqual(payload["window"]["start_ts_ns"], start_ns)
        self.assertEqual(payload["window"]["end_ts_ns"], end_ns)
        self.assertEqual(payload["window"]["interval_source"], "run_metadata")
        self.assertEqual(payload["window"]["sample_interval_ns"], 200_000_000)
        self.assertEqual(payload["coverage_ratio"], 1.0)
        self.assertEqual(payload["devices"][0]["sample_count"], len(dcmi_sample_timestamps()))

    def test_get_route_rejects_missing_or_malformed_parameters(self) -> None:
        for path in (
            "/api/agent/telemetry-window?host=192.0.2.10&devices=0&start_ts=1",
            "/api/agent/telemetry-window?host=192.0.2.10&devices=x&start_ts=1&end_ts=2",
            f"/api/agent/telemetry-window?host=192.0.2.10&devices=0&start_ts={self.start}&end_ts={self.start}",
            f"/api/agent/telemetry-window?host=missing&devices=0&start_ts={self.start}&end_ts={self.start + 1}",
        ):
            with self.subTest(path=path):
                status, contract, payload = self.request(path)
                self.assertIn(status, (400, 404))
                self.assertEqual(contract, "observation-only")
                self.assertIn("error", payload)


class TelemetryWindowMcpTests(unittest.TestCase):
    class Client:
        def __init__(self) -> None:
            self.arguments = None

        def telemetry_window(self, host, devices, start_ts, end_ts):
            self.arguments = (host, devices, start_ts, end_ts)
            return {
                "source": "sqlite",
                "server": {"host": host},
                "window": {"start_ts": start_ts, "end_ts": end_ts, "raw_sample_count": 0},
                "coverage_ratio": 0.0,
                "throttling_detected": None,
                "resource_interference_detected": None,
                "freq_mhz_min": None,
                "power_max_w": None,
                "measurement_limitations": [
                    "No raw device samples",
                    "Resource interference was not measured explicitly; resource_interference_detected is null.",
                ],
                "devices": [],
            }

    def test_tool_is_listed_with_explicit_input_contract(self) -> None:
        listed = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, self.Client())
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        self.assertIn("npu_telemetry_window", tools)
        schema = tools["npu_telemetry_window"]["inputSchema"]
        self.assertEqual(set(schema["required"]), {"host", "devices", "start_ts", "end_ts"})
        self.assertEqual(schema["properties"]["devices"]["items"]["type"], "integer")

    def test_call_dispatches_exact_window_arguments(self) -> None:
        client = self.Client()
        response = handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "npu_telemetry_window",
                "arguments": {"host": "192.0.2.10", "devices": [0, 1], "start_ts": 10, "end_ts": 20},
            },
        }, client)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(client.arguments, ("192.0.2.10", [0, 1], 10, 20))
        payload = response["result"]["structuredContent"]
        self.assertIsNone(payload["resource_interference_detected"])
        self.assertIn("Resource interference", " ".join(payload["measurement_limitations"]))
        self.assertIn("telemetry 10..20", response["result"]["content"][0]["text"])
        self.assertIn("limitation:", response["result"]["content"][0]["text"])

    def test_call_preserves_fractional_timestamp_strings(self) -> None:
        client = self.Client()
        response = handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "npu_telemetry_window",
                "arguments": {
                    "host": "192.0.2.10", "devices": [0],
                    "start_ts": "1792433955.06677835", "end_ts": "1792433956.06676142",
                },
            },
        }, client)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(
            client.arguments,
            ("192.0.2.10", [0], "1792433955.06677835", "1792433956.06676142"),
        )

    def test_call_rejects_invalid_devices(self) -> None:
        response = handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "npu_telemetry_window",
                "arguments": {"host": "192.0.2.10", "devices": [], "start_ts": 10, "end_ts": 20},
            },
        }, self.Client())
        self.assertTrue(response["result"]["isError"])


FULL_DCMI_CAPTURE = (
    Path(os.environ["MINDIE_TEST_DCMI_CAPTURE"])
    if os.environ.get("MINDIE_TEST_DCMI_CAPTURE") else None
)


@unittest.skipUnless(FULL_DCMI_CAPTURE is not None, "set MINDIE_TEST_DCMI_CAPTURE for full-capture replay")
class FullDcmiCaptureReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.meta = None
        cls.summary = None
        cls.samples = []
        for line in FULL_DCMI_CAPTURE.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("type") == "meta":
                cls.meta = record
            elif record.get("type") == "summary":
                cls.summary = record
            elif record.get("type") == "sample":
                cls.samples.append(record)
        if cls.meta is None or cls.summary is None or not cls.samples:
            raise AssertionError("full DCMI capture is incomplete")
        cls.interval_ns = int(Decimal(str(cls.meta["interval_s"])) * Decimal(1_000_000_000))
        cls.samples_by_index = {int(row["sample_index"]): row for row in cls.samples}
        cls.scheduled = {
            index: int(row["wall_before_ns"]) - int(row.get("schedule_slip_ns") or 0)
            for index, row in cls.samples_by_index.items()
        }
        candidates = sorted(
            cls.scheduled[index] - index * cls.interval_ns for index in cls.samples_by_index
        )
        cls.anchor_ns = candidates[len(candidates) // 2]
        cls.last_index = int(cls.summary["sample_count"]) - 1

    @staticmethod
    def _ceil_div(value: int, divisor: int) -> int:
        return -((-value) // divisor)

    def _window_bounds(self, start_ns: int, end_ns: int) -> tuple[int, int]:
        low = max(0, self._ceil_div(start_ns - self.anchor_ns, self.interval_ns))
        high = min(self.last_index, (end_ns - self.anchor_ns) // self.interval_ns)
        return low, high

    @staticmethod
    def _getter(sample: dict, name: str) -> tuple[object, int | None, bool]:
        getter = sample["cards"]["0"]["getters"].get(name) or {}
        finished = getter.get("finished_at_ns")
        valid = (
            getter.get("ret") == 0 and not getter.get("error")
            and not getter.get("skipped")
        )
        return getter.get("value"), int(finished) if isinstance(finished, int) else None, valid

    def _expected_throttle(self, indices: list[int], end_ns: int) -> bool | None:
        flags = []
        for index in indices:
            sample = self.samples_by_index.get(index)
            if sample is None:
                continue
            getter = sample["cards"]["0"]["getters"].get("freq_reduc_cause") or {}
            finish = getter.get("finished_at_ns")
            if (
                getter.get("ret") != 0 or getter.get("error") or getter.get("skipped")
                or not isinstance(finish, int) or finish > end_ns
            ):
                continue
            decoded = getter.get("decoded") or {}
            if decoded.get("state") == "observed" and not decoded.get("unknown_bits"):
                flags.append(decoded)
        if any(flag.get("thermal_limit") or flag.get("power_limit") or flag.get("svfd") for flag in flags):
            return True
        return False if len(flags) == len(indices) else None

    def _expected_field(self, indices: list[int], name: str, end_ns: int) -> list[object]:
        values = []
        for index in indices:
            sample = self.samples_by_index.get(index)
            if sample is None:
                continue
            value, finished, valid = self._getter(sample, name)
            if valid and finished is not None and finished <= end_ns and value is not None:
                values.append(value)
        return values

    def test_full_capture_replay_five_c4_fractional_windows(self) -> None:
        windows = [
            (
                self.anchor_ns + 100 * self.interval_ns,
                self.anchor_ns + 110 * self.interval_ns,
            ),
            (
                self.anchor_ns + 200 * self.interval_ns + self.interval_ns // 2,
                self.anchor_ns + 205 * self.interval_ns + self.interval_ns // 3,
            ),
            (
                self.anchor_ns + 300 * self.interval_ns - self.interval_ns // 3,
                self.anchor_ns + 310 * self.interval_ns + self.interval_ns // 4,
            ),
            (
                self.anchor_ns + 449 * self.interval_ns,
                self.anchor_ns + 450 * self.interval_ns,
            ),
            (
                self.anchor_ns + 2195 * self.interval_ns,
                self.anchor_ns + 2200 * self.interval_ns,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "full.sqlite3"
            db = Database(path)
            db.initialize()
            server = db.upsert_server({
                "id": "s1", "name": "a2", "host": "192.0.2.10", "port": 22,
                "username": "root", "tags": ["A2"],
            })
            ingested = db.ingest_dcmi_jsonl(FULL_DCMI_CAPTURE, "s1")
            self.assertEqual(ingested["samples"], 2201)
            self.assertEqual(ingested["interval_ns"], self.interval_ns)
            self.assertEqual(ingested["schedule_anchor_ns"], self.anchor_ns)
            self.assertEqual(ingested["last_sample_index"], self.last_index)
            db.close()
            db = Database(path)
            db.initialize()
            try:
                for start_ns, end_ns in windows:
                    low, high = self._window_bounds(start_ns, end_ns)
                    expected_indices = list(range(low, high + 1))
                    raw = db.telemetry_samples(
                        "s1", [0], start_ns / 1_000_000_000, end_ns / 1_000_000_000,
                        start_ts_ns=start_ns, end_ts_ns=end_ns,
                    )
                    payload = build_window(
                        server, raw["samples"], devices=[0],
                        start_ts=start_ns / 1_000_000_000, end_ts=end_ns / 1_000_000_000,
                        start_ts_ns=start_ns, end_ts_ns=end_ns, runs=raw["runs"],
                    )
                    with self.subTest(start_ns=start_ns, end_ns=end_ns):
                        self.assertEqual(payload["window"]["start_ts_ns"], start_ns)
                        self.assertEqual(payload["window"]["end_ts_ns"], end_ns)
                        self.assertEqual(payload["window"]["coverage_basis"], "declared_sample_index_phase")
                        self.assertEqual(payload["window"]["clock_domains"], ["unix_wall"])
                        self.assertEqual(payload["window"]["schedule_runs"][0]["interval_ns"], self.interval_ns)
                        self.assertIsNotNone(payload["window"]["schedule_phase_drift_ns_max"])
                        self.assertEqual(payload["window"]["scheduled_sample_count"], len(expected_indices))
                        self.assertEqual(payload["window"]["observed_sample_count"], len(expected_indices))
                        self.assertEqual(payload["window"]["missing_sample_count"], 0)
                        self.assertEqual(payload["coverage_ratio"], 1.0)
                        seen_indices = {int(row["sample_index"]) for row in raw["samples"]}
                        self.assertEqual(seen_indices, set(expected_indices))
                        for index in expected_indices:
                            self.assertEqual(
                                next(row for row in raw["samples"] if row["sample_index"] == index)["sample_ts_ns"],
                                int(self.samples_by_index[index]["wall_before_ns"]),
                            )
                        device = payload["devices"][0]
                        self.assertEqual(device["expected_sample_count"], len(expected_indices))
                        temperature = self._expected_field(expected_indices, "temperature", end_ns)
                        power = self._expected_field(expected_indices, "power", end_ns)
                        frequency = self._expected_field(expected_indices, "current_freq", end_ns)
                        self.assertEqual(
                            device["field_coverage"]["temperature_c"]["observed"], len(temperature),
                        )
                        self.assertEqual(device["field_coverage"]["power_w"]["observed"], len(power))
                        self.assertEqual(device["field_coverage"]["freq_mhz"]["observed"], len(frequency))
                        if temperature:
                            self.assertAlmostEqual(device["temp_max_c"], max(temperature), places=3)
                        else:
                            self.assertIsNone(device["temp_max_c"])
                        if power:
                            self.assertAlmostEqual(device["power_max_w"], max(power), places=3)
                        else:
                            self.assertIsNone(device["power_max_w"])
                        if frequency:
                            self.assertAlmostEqual(device["freq_mhz_min"], min(frequency), places=3)
                        else:
                            self.assertIsNone(device["freq_mhz_min"])
                        self.assertIsNone(device["aicore_p50"])
                        self.assertIsNone(device["hbm_peak_mb"])
                        self.assertIs(device["throttling_detected"], self._expected_throttle(expected_indices, end_ns))
                        self.assertIsNone(device["resource_interference_detected"])
            finally:
                db.close()

    def test_hypothetical_missing_first_sample_lowers_declared_coverage(self) -> None:
        # Derived negative case from the real capture, not a synthetic hardware capture.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-first.sqlite3"
            db = Database(path)
            db.initialize()
            server = db.upsert_server({
                "id": "s1", "name": "a2", "host": "192.0.2.10", "port": 22,
                "username": "root", "tags": ["A2"],
            })
            db.ingest_dcmi_jsonl(FULL_DCMI_CAPTURE, "s1")
            db.connection().execute(
                "DELETE FROM device_samples WHERE run_id=? AND sample_index=0",
                (self.meta["run_id"],),
            )
            start_ns = self.anchor_ns
            end_ns = self.anchor_ns + self.last_index * self.interval_ns
            raw = db.telemetry_samples(
                "s1", [0], start_ns / 1_000_000_000, end_ns / 1_000_000_000,
                start_ts_ns=start_ns, end_ts_ns=end_ns,
            )
            payload = build_window(
                server, raw["samples"], devices=[0],
                start_ts=start_ns / 1_000_000_000, end_ts=end_ns / 1_000_000_000,
                start_ts_ns=start_ns, end_ts_ns=end_ns, runs=raw["runs"],
            )
            try:
                self.assertEqual(payload["window"]["scheduled_sample_count"], 2201)
                self.assertEqual(payload["window"]["observed_sample_count"], 2200)
                self.assertEqual(payload["window"]["missing_sample_count"], 1)
                self.assertLess(payload["coverage_ratio"], 1.0)
                self.assertGreater(payload["coverage_ratio"], 0.999)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
