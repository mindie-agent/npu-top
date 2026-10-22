"""Telemetry-window aggregation, persistence, and percentile tests.

The integration fixtures are the repository's captured A2/A3/A5 `npu-smi` text,
not hand-written snapshot mocks. Those captures expose aicore, HBM, power, and
temperature, but no frequency register and no throttling flag; the tests assert
that the window reports those as unavailable instead of inferring them.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from npu_top.db import Database
from npu_top.npu_smi import parse_npu_smi_info
from npu_top.telemetry_window import (
    build_window, extract_dcmi_metrics, extract_raw_metrics, percentile,
)


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


class TelemetryWindowDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db = Database(self.state / "test.sqlite3")
        self.db.initialize()
        self.server = self.db.upsert_server({
            "id": "s1", "name": "a2-lab", "host": "192.0.2.10", "port": 22,
            "username": "root", "tags": ["A2"],
        })
        self.start = 1_700_000_000 // 900 * 900

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def record(self, fixture: str, timestamp: int) -> list[dict]:
        devices = captured_devices(fixture)
        self.db.record_success(self.server["id"], snapshot_at(timestamp, devices), True)
        return devices

    def window(self, devices: list[int], start: int, end: int) -> dict:
        raw = self.db.telemetry_samples(self.server["id"], devices, start, end)
        return build_window(
            self.server, raw["samples"], devices=devices, start_ts=start, end_ts=end,
            runs=raw["runs"], rollup_sample_count=raw["rollup_sample_count"],
            now=self.start + 3600,
        )

    def test_captured_a2_samples_provide_only_observed_metrics(self) -> None:
        devices = self.record("a2-173-125-1-2.txt", self.start)
        self.record("a2-173-125-1-2.txt", self.start + 30)
        payload = self.window([0], self.start, self.start + 30)
        device = payload["devices"][0]
        captured = devices[0]
        self.assertEqual(device["sample_count"], 2)
        self.assertEqual(device["aicore_p50"], captured["aicore_percent"])
        self.assertEqual(device["aicore_p95"], captured["aicore_percent"])
        self.assertEqual(device["hbm_peak_mb"], captured["hbm"]["used_mb"])
        self.assertEqual(device["temp_max_c"], captured["temperature_c"])
        self.assertEqual(device["power_max_w"], captured["power_w"])
        self.assertIsNone(device["coverage_ratio"])
        self.assertIsNone(device["expected_sample_count"])
        self.assertEqual(device["field_coverage"]["temperature_c"]["basis"], "schedule_unavailable")
        self.assertIsNone(device["field_coverage"]["temperature_c"]["coverage_ratio"])
        self.assertIsNone(device["freq_mhz_min"])
        self.assertIsNone(device["throttling_detected"])
        self.assertIsNone(device["resource_interference_detected"])
        self.assertIsNone(payload["coverage_ratio"])
        self.assertIsNone(payload["throttling_detected"])
        self.assertIsNone(payload["resource_interference_detected"])
        self.assertIsNone(payload["freq_mhz_min"])
        self.assertEqual(payload["power_max_w"], captured["power_w"])
        self.assertEqual(payload["temp_max_c"], captured["temperature_c"])
        self.assertEqual(payload["window"]["start_ts"], self.start)
        self.assertEqual(payload["window"]["end_ts"], self.start + 30)
        for key in (
            "window", "coverage_ratio", "throttling_detected", "resource_interference_detected",
            "freq_mhz_min", "power_max_w", "temp_max_c", "measurement_limitations",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["window"]["sampling_basis"], "raw_device_samples")
        self.assertEqual(payload["window"]["coverage_basis"], "schedule_unavailable")
        self.assertIsNone(payload["window"]["scheduled_sample_count"])
        self.assertEqual(payload["window"]["rollup_sample_count"], 2)
        limitations = " ".join(payload["measurement_limitations"])
        device_limitations = " ".join(device["measurement_limitations"])
        self.assertIn("schedule phase", limitations)
        self.assertIn("frequency getter", device_limitations)
        self.assertIn("throttling", device_limitations)
        self.assertIn("null rather than false", device_limitations)
        self.assertIn("Temperature", device_limitations)
        self.assertIn("Resource interference", limitations)

    def test_fleet_aggregates_require_every_requested_device(self) -> None:
        devices = self.record("a2-173-125-1-2.txt", self.start)
        payload = self.window([0, 1], self.start, self.start + 30)
        self.assertEqual(payload["power_max_w"], max(devices[0]["power_w"], devices[1]["power_w"]))
        self.assertEqual(payload["temp_max_c"], max(devices[0]["temperature_c"], devices[1]["temperature_c"]))
        self.assertIsNone(payload["freq_mhz_min"])
        partial = self.window([0, 99], self.start, self.start + 30)
        self.assertIsNone(partial["power_max_w"])
        self.assertIsNone(partial["temp_max_c"])
        self.assertIsNone(partial["freq_mhz_min"])
        self.assertIn("requested device(s) 99", " ".join(partial["measurement_limitations"]))

    def test_extract_raw_metrics_does_not_invent_frequency_or_throttle(self) -> None:
        device = captured_devices("a3-192-168-13-154-busy.txt")[0]
        metrics = extract_raw_metrics(device)
        self.assertEqual(metrics["power_w"], device["power_w"])
        self.assertEqual(metrics["temperature_c"], device["temperature_c"])
        self.assertEqual(metrics["hbm_used_mb"], device["hbm"]["used_mb"])
        self.assertIsNone(metrics["freq_mhz"])
        self.assertIsNone(metrics["throttling_flag"])

    def test_dcmi_idle_svfd_and_unknown_bits_keep_their_meaning(self) -> None:
        from npu_top.dcmi_telemetry import decode_cause_mask

        for mask, expected in ((1, 0), (1 << 8, 1), (1 << 7, None), ((1 << 7) | (1 << 2), 1)):
            device = {"dcmi_freq_reduc": decode_cause_mask(mask)}
            self.assertEqual(extract_raw_metrics(device)["throttling_flag"], expected)

    def test_explicit_process_interference_survives_database_and_partial_windows(self) -> None:
        devices = captured_devices("a2-173-125-1-2.txt")
        devices[0]["resource_interference_detected"] = False
        self.db.record_success(self.server["id"], snapshot_at(self.start, devices), True)
        self.db.record_success(self.server["id"], snapshot_at(self.start + 30, devices), True)
        assert self.window([0], self.start, self.start + 30)["resource_interference_detected"] is False
        devices[0]["resource_interference_detected"] = None
        self.db.record_success(self.server["id"], snapshot_at(self.start + 30, devices), True)
        assert self.window([0], self.start, self.start + 30)["resource_interference_detected"] is None
        devices[0]["resource_interference_detected"] = True
        self.db.record_success(self.server["id"], snapshot_at(self.start + 30, devices), True)
        assert self.window([0], self.start, self.start + 30)["resource_interference_detected"] is True

    def test_explicit_frequency_and_throttle_columns_are_projected(self) -> None:
        # These row keys mirror the device_samples schema. Current probe captures
        # do not populate them; this validates the explicit-register contract
        # without claiming the hardware supplied a value.
        rows = [
            {
                "npu_id": 0, "collected_at": self.start, "name": "Ascend910",
                "aicore_percent": 20.0, "hbm_used_mb": 100.0, "temperature_c": 50.0,
                "power_w": 150.0, "freq_mhz": 1200.0, "throttling_flag": 0,
            },
            {
                "npu_id": 0, "collected_at": self.start + 30, "name": "Ascend910",
                "aicore_percent": 80.0, "hbm_used_mb": 300.0, "temperature_c": 60.0,
                "power_w": 210.0, "freq_mhz": 900.0, "throttling_flag": 1,
            },
        ]
        payload = build_window(
            self.server, rows, devices=[0], start_ts=self.start, end_ts=self.start + 30,
            now=self.start + 3600,
        )
        device = payload["devices"][0]
        self.assertEqual(device["aicore_p50"], 50.0)
        self.assertEqual(device["aicore_p95"], 77.0)
        self.assertEqual(device["hbm_peak_mb"], 300.0)
        self.assertEqual(device["temp_max_c"], 60.0)
        self.assertEqual(device["power_max_w"], 210.0)
        self.assertEqual(device["freq_mhz_min"], 900.0)
        self.assertIs(device["throttling_detected"], True)
        self.assertIs(payload["throttling_detected"], True)
        self.assertIsNone(payload["resource_interference_detected"])
        self.assertEqual(payload["freq_mhz_min"], 900.0)
        self.assertEqual(payload["power_max_w"], 210.0)
        self.assertEqual(payload["temp_max_c"], 60.0)
        self.assertIn("Resource interference", " ".join(payload["measurement_limitations"]))
        self.assertNotIn(
            "frequency register", " ".join(payload["measurement_limitations"]),
        )

    def test_unscheduled_probe_window_does_not_invent_coverage(self) -> None:
        self.record("a2-173-125-1-2.txt", self.start)
        self.record("a2-173-125-1-2.txt", self.start + 60)
        payload = self.window([0], self.start, self.start + 60)
        self.assertEqual(payload["window"]["duration_seconds"], 60)
        self.assertIsNone(payload["devices"][0]["coverage_ratio"])
        self.assertIsNone(payload["devices"][0]["expected_sample_count"])
        self.assertIsNone(payload["coverage_ratio"])
        self.assertIsNone(payload["window"]["scheduled_sample_count"])
        self.assertIn("schedule phase", " ".join(payload["measurement_limitations"]))
        self.assertIn(
            "scheduled coverage is null", " ".join(payload["devices"][0]["measurement_limitations"]),
        )

    def test_historical_rollups_do_not_fake_raw_distributions(self) -> None:
        self.record("a3-192-168-13-165-idle.txt", self.start)
        self.db.connection().execute("DELETE FROM device_samples")
        payload = self.window([0], self.start, self.start + 30)
        self.assertEqual(payload["window"]["raw_sample_count"], 0)
        self.assertEqual(payload["window"]["rollup_sample_count"], 1)
        device = payload["devices"][0]
        self.assertEqual(device["sample_count"], 0)
        for key in ("aicore_p50", "aicore_p95", "hbm_peak_mb", "temp_max_c", "power_max_w", "freq_mhz_min"):
            self.assertIsNone(device[key], key)
        for key in ("temp_max_c", "power_max_w", "freq_mhz_min"):
            self.assertIsNone(payload[key], key)
        self.assertIsNone(payload["resource_interference_detected"])
        limitations = " ".join(payload["measurement_limitations"])
        self.assertIn("No raw device samples", limitations)
        self.assertIn("15-minute rollup", limitations)
        self.assertIn("not used", limitations)
        self.assertIn("Resource interference", limitations)

    def test_prune_bounds_raw_samples_with_existing_retention(self) -> None:
        now = int(time.time())
        self.record("a2-173-125-1-2.txt", now - 100 * 86400)
        self.record("a2-173-125-1-2.txt", now)
        self.db.prune(90)
        counts = self.db.connection().execute(
            "SELECT COUNT(*), MIN(collected_at) FROM device_samples WHERE server_id='s1'"
        ).fetchone()
        self.assertEqual(counts[0], len(captured_devices("a2-173-125-1-2.txt")))
        self.assertGreaterEqual(counts[1], now - 90 * 86400)

    def test_pressure_pruning_discards_raw_samples_with_rollups(self) -> None:
        self.record("a2-173-125-1-2.txt", int(time.time()))
        self.db.max_bytes = 1
        self.db.prune(90)
        self.assertEqual(
            self.db.connection().execute("SELECT COUNT(*) FROM device_samples").fetchone()[0], 0,
        )
        self.assertEqual(
            self.db.connection().execute("SELECT COUNT(*) FROM device_rollups").fetchone()[0], 0,
        )


DCMI_FIXTURE = FIXTURES / "dcmi-metrics-slice.jsonl"


def actual_dcmi_card() -> dict:
    for line in DCMI_FIXTURE.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "sample":
            return record["cards"]["0"]
    raise AssertionError("DCMI slice contains no sample")


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


class PreciseDcmiTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "precise.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self.server = self.db.upsert_server({
            "id": "s1", "name": "a2-lab", "host": "192.0.2.10", "port": 22,
            "username": "root", "tags": ["A2"],
        })
        self.ingest = self.db.ingest_dcmi_jsonl(DCMI_FIXTURE, "s1")

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def query(self, start_ns: int, end_ns: int) -> dict:
        return self.db.telemetry_samples(
            "s1", [0], start_ns / 1_000_000_000, end_ns / 1_000_000_000,
            start_ts_ns=start_ns, end_ts_ns=end_ns,
        )

    def window(self, start_ns: int, end_ns: int) -> dict:
        raw = self.query(start_ns, end_ns)
        return build_window(
            self.server, raw["samples"], devices=[0],
            start_ts=start_ns / 1_000_000_000, end_ts=end_ns / 1_000_000_000,
            start_ts_ns=start_ns, end_ts_ns=end_ns, runs=raw["runs"],
            rollup_sample_count=raw["rollup_sample_count"],
        )

    def test_single_record_dcmi_sample_creates_live_run_schedule(self) -> None:
        sample = next(
            json.loads(line) for line in DCMI_FIXTURE.read_text().splitlines()
            if json.loads(line).get("type") == "sample"
        )
        self.db.upsert_server({
            "id": "s2", "name": "live", "host": "192.0.2.20", "port": 22,
            "username": "root", "tags": ["A2"],
        })
        self.assertEqual(
            self.db.record_dcmi_sample("s2", sample, interval_ns=200_000_000), 1,
        )
        scheduled = int(sample["wall_before_ns"]) - int(sample["schedule_slip_ns"])
        end_ns = max(
            int(getter["finished_at_ns"]) for getter in sample["cards"]["0"]["getters"].values()
            if isinstance(getter.get("finished_at_ns"), int)
        )
        raw = self.db.telemetry_samples(
            "s2", [0], scheduled / 1_000_000_000, end_ns / 1_000_000_000,
            start_ts_ns=scheduled, end_ts_ns=end_ns,
        )
        payload = build_window(
            self.db.get_server("s2"), raw["samples"], devices=[0],
            start_ts=scheduled / 1_000_000_000, end_ts=end_ns / 1_000_000_000,
            start_ts_ns=scheduled, end_ts_ns=end_ns, runs=raw["runs"],
        )
        self.assertEqual(raw["samples"][0]["sample_index"], 0)
        self.assertEqual(payload["window"]["scheduled_sample_count"], 1)
        self.assertEqual(payload["coverage_ratio"], 1.0)
        self.assertEqual(payload["devices"][0]["freq_mhz_min"], 800.0)

    def test_dcmi_getter_status_and_hbm_unit_are_respected(self) -> None:
        card = actual_dcmi_card()
        metrics = extract_dcmi_metrics(card)
        self.assertIsNone(metrics["aicore_percent"])
        self.assertIsNone(metrics["hbm_used_mb"])
        self.assertEqual(metrics["getter_status"]["aicore_utilization"]["skipped"], True)
        promoted = extract_dcmi_metrics(card, hbm_usage_unit="MB")
        self.assertEqual(promoted["hbm_used_mb"], 2875.0)

        failed = json.loads(json.dumps(card))
        failed["getters"]["power"]["ret"] = -1
        failed["getters"]["power"]["error"] = "DCMI ret -1"
        failed["getters"]["current_freq"]["error"] = "read failed"
        failed["getters"]["freq_reduc_cause"]["ret"] = -1
        failed_metrics = extract_dcmi_metrics(failed)
        self.assertIsNone(failed_metrics["power_w"])
        self.assertIsNone(failed_metrics["freq_mhz"])
        self.assertIsNone(failed_metrics["throttling_flag"])
        self.assertEqual(failed_metrics["getter_status"]["power"]["ret"], -1)
        self.assertEqual(failed_metrics["getter_status"]["power"]["error"], "DCMI ret -1")

    def test_real_dcmi_slice_keeps_same_second_samples_across_reopen(self) -> None:
        timestamps = dcmi_sample_timestamps()
        scheduled = dcmi_scheduled_timestamps()
        self.assertEqual(self.ingest["samples"], len(timestamps))
        self.assertEqual(self.ingest["first_ts_ns"], timestamps[0])
        self.assertEqual(self.ingest["last_ts_ns"], timestamps[-1])
        self.assertEqual(self.ingest["first_scheduled_ts_ns"], min(scheduled))
        self.assertEqual(self.ingest["last_scheduled_ts_ns"], max(scheduled))
        self.assertEqual(self.ingest["interval_ns"], 200_000_000)
        for key in (
            "state", "server_id", "run_id", "samples", "rows", "first_ts_ns",
            "last_ts_ns", "first_scheduled_ts_ns", "last_scheduled_ts_ns", "interval_ns",
            "interval_seconds", "schedule_anchor_ns", "first_sample_index",
            "last_sample_index", "timestamp_sources", "clock_domain", "meta",
            "capture_summary",
        ):
            self.assertIn(key, self.ingest)
        self.assertLess(len({stamp // 1_000_000_000 for stamp in timestamps}), len(timestamps))

        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        end_ns = max(scheduled) + 100_000_000
        raw = self.query(min(scheduled), end_ns)
        stored = [row["sample_ts_ns"] for row in raw["samples"]]
        self.assertEqual(stored, timestamps)
        payload = self.window(min(scheduled), end_ns)
        window = payload["window"]
        self.assertEqual(window["start_ts_ns"], min(scheduled))
        self.assertEqual(window["end_ts_ns"], end_ns)
        self.assertEqual(window["sample_interval_ns"], 200_000_000)
        self.assertEqual(window["interval_source"], "run_metadata")
        self.assertEqual(window["timestamp_sources"], ["dcmi.wall_before_ns"])
        self.assertEqual(window["clock_domains"], ["unix_wall"])
        self.assertEqual(window["observed_interval_seconds"]["median"], 0.2)
        self.assertEqual(window["scheduled_sample_count"], 6)
        self.assertEqual(window["observed_sample_count"], 6)
        self.assertEqual(window["missing_sample_count"], 0)
        self.assertEqual(payload["coverage_ratio"], 1.0)
        device = payload["devices"][0]
        self.assertEqual(device["sample_count"], len(timestamps))
        self.assertEqual(device["expected_sample_count"], len(timestamps))
        self.assertEqual(device["field_coverage"]["aicore_percent"]["coverage_ratio"], 0.0)
        self.assertEqual(device["field_coverage"]["hbm_used_mb"]["coverage_ratio"], 0.0)
        self.assertEqual(device["field_coverage"]["temperature_c"]["coverage_ratio"], 1.0)
        self.assertEqual(device["field_coverage"]["power_w"]["coverage_ratio"], 1.0)
        self.assertEqual(device["field_coverage"]["freq_mhz"]["coverage_ratio"], 1.0)
        self.assertEqual(device["field_coverage"]["throttling_flag"]["coverage_ratio"], 1.0)
        self.assertIsNone(device["aicore_p50"])
        self.assertIsNone(device["aicore_p95"])
        self.assertIsNone(device["hbm_peak_mb"])
        self.assertEqual(device["temp_max_c"], 44.0)
        self.assertEqual(device["power_max_w"], 91.7)
        self.assertEqual(device["freq_mhz_min"], 800.0)
        self.assertIs(device["throttling_detected"], False)
        self.assertIsNone(device["resource_interference_detected"])
        self.assertIsNone(payload["resource_interference_detected"])
        limitations = " ".join(payload["measurement_limitations"])
        device_limitations = " ".join(device["measurement_limitations"])
        self.assertIn("AICore utilization was observed in 0/6", device_limitations)
        self.assertIn("HBM usage was observed in 0/6", device_limitations)
        self.assertIn("continuous health", limitations)
        self.assertIn("clock domain", limitations)
        self.assertIsNotNone(window["schedule_slip_ms_max"])

    def test_fractional_window_boundaries_are_not_truncated(self) -> None:
        scheduled = dcmi_scheduled_timestamps()
        start_ns = min(scheduled) + 50_000_000
        end_ns = max(scheduled) - 30_000_000
        raw = self.query(start_ns, end_ns)
        self.assertEqual(len(raw["samples"]), 4)
        payload = self.window(start_ns, end_ns)
        self.assertEqual(payload["window"]["start_ts_ns"], start_ns)
        self.assertEqual(payload["window"]["end_ts_ns"], end_ns)
        self.assertEqual(payload["window"]["start_ts"], start_ns / 1_000_000_000)
        self.assertEqual(payload["window"]["duration_seconds"], (end_ns - start_ns) / 1_000_000_000)
        self.assertEqual(payload["devices"][0]["expected_sample_count"], 4)
        self.assertEqual(payload["devices"][0]["coverage_ratio"], 1.0)

    def test_getter_finishing_after_window_end_is_excluded(self) -> None:
        scheduled = dcmi_scheduled_timestamps()
        payload = self.window(min(scheduled), max(scheduled))
        device = payload["devices"][0]
        field = device["field_coverage"]["temperature_c"]
        self.assertGreaterEqual(field["late_finish_excluded"], 1)
        self.assertLess(field["coverage_ratio"], 1.0)
        self.assertIn("finished after the window end", " ".join(device["measurement_limitations"]))

    def test_missing_sample_lowers_scheduled_coverage(self) -> None:
        scheduled = dcmi_scheduled_timestamps()
        self.db.connection().execute(
            "DELETE FROM device_samples WHERE sample_ts_ns=?", (dcmi_sample_timestamps()[2],),
        )
        payload = self.window(min(scheduled), max(scheduled))
        device = payload["devices"][0]
        self.assertEqual(device["sample_count"], 5)
        self.assertEqual(device["expected_sample_count"], 6)
        self.assertEqual(device["coverage_ratio"], 0.833333)
        self.assertEqual(payload["window"]["scheduled_sample_count"], 6)
        self.assertEqual(payload["window"]["observed_sample_count"], 5)
        self.assertEqual(payload["window"]["missing_sample_count"], 1)
        self.assertIn("Scheduled coverage", " ".join(device["measurement_limitations"]))
        self.assertIn("Scheduled coverage is below 100%", " ".join(payload["measurement_limitations"]))

    def test_read_failed_getter_lowers_field_coverage_and_blocks_false_throttle(self) -> None:
        scheduled = dcmi_scheduled_timestamps()
        self.db.connection().execute(
            "UPDATE device_samples SET power_w=NULL, throttling_flag=NULL WHERE sample_ts_ns=?",
            (dcmi_sample_timestamps()[1],),
        )
        payload = self.window(min(scheduled), max(scheduled) + 100_000_000)
        device = payload["devices"][0]
        self.assertEqual(device["field_coverage"]["power_w"]["observed"], 5)
        self.assertEqual(device["field_coverage"]["power_w"]["coverage_ratio"], 0.833333)
        self.assertEqual(device["field_coverage"]["throttling_flag"]["observed"], 5)
        self.assertEqual(device["field_coverage"]["temperature_c"]["coverage_ratio"], 1.0)
        self.assertIsNone(device["throttling_detected"])
        self.assertIsNone(payload["throttling_detected"])
        limitations = " ".join(device["measurement_limitations"])
        self.assertIn("Power was observed in 5/6 scheduled points", limitations)
        self.assertIn("absence of throttling cannot be concluded", limitations)

    def test_row_interval_mutation_does_not_override_declared_run_schedule(self) -> None:
        scheduled = dcmi_scheduled_timestamps()
        self.db.connection().execute(
            "UPDATE device_samples SET interval_ns=NULL WHERE sample_ts_ns=?",
            (dcmi_sample_timestamps()[1],),
        )
        payload = self.window(min(scheduled), max(scheduled))
        self.assertEqual(payload["window"]["interval_source"], "run_metadata")
        self.assertEqual(payload["window"]["sample_interval_ns"], 200_000_000)
        self.assertEqual(payload["window"]["scheduled_sample_count"], 6)
        self.assertEqual(payload["coverage_ratio"], 1.0)

    def test_replaying_the_same_stream_is_idempotent(self) -> None:
        second = self.db.ingest_dcmi_jsonl(DCMI_FIXTURE, "s1")
        self.assertEqual(second["samples"], self.ingest["samples"])
        count = self.db.connection().execute(
            "SELECT COUNT(*) FROM device_samples WHERE server_id='s1'"
        ).fetchone()[0]
        self.assertEqual(count, len(dcmi_sample_timestamps()))

    def test_record_success_preserves_fractional_probe_timestamp(self) -> None:
        devices = captured_devices("a2-173-125-1-2.txt")[:1]
        for collected_at in (1_700_000_000.25, 1_700_000_000.75):
            self.db.record_success("s1", {
                "collected_at": collected_at,
                "summary": {"npu_count": 1, "busy_npu_count": 0},
                "devices": devices,
            }, True)
        raw = self.query(1_700_000_000_000_000_000, 1_700_000_001_000_000_000)
        self.assertEqual(
            [row["sample_ts_ns"] for row in raw["samples"]],
            [1_700_000_000_250_000_000, 1_700_000_000_750_000_000],
        )
        self.assertEqual({row["timestamp_source"] for row in raw["samples"]}, {"probe.collected_at"})
        self.assertIsNone(raw["samples"][0]["interval_ns"])

    def test_old_integer_second_table_migrates_without_data_loss(self) -> None:
        old_path = Path(self.temp.name) / "old.sqlite3"
        connection = sqlite3.connect(old_path)
        connection.executescript("""
            CREATE TABLE servers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
              port INTEGER NOT NULL DEFAULT 22, username TEXT NOT NULL DEFAULT 'root',
              tags_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              last_seen_at INTEGER, last_error TEXT
            );
            CREATE TABLE device_samples (
              server_id TEXT NOT NULL, collected_at INTEGER NOT NULL, npu_id INTEGER NOT NULL,
              name TEXT, aicore_percent REAL, hbm_used_mb REAL, temperature_c REAL,
              power_w REAL, freq_mhz REAL, throttling_flag INTEGER,
              resource_interference_flag INTEGER,
              PRIMARY KEY (server_id, collected_at, npu_id)
            );
            INSERT INTO servers VALUES (
              's1','old','192.0.2.11',22,'root','[]',1,1,1,1,NULL
            );
            INSERT INTO device_samples VALUES (
              's1', 1700000000, 0, 'legacy', 12.0, 34.0, 40.0, 50.0, 800.0, 0, 1
            );
        """)
        connection.commit()
        connection.close()
        db = Database(old_path)
        db.initialize()
        try:
            row = db.telemetry_samples("s1", [0], 1_700_000_000, 1_700_000_001)["samples"][0]
            self.assertEqual(row["sample_ts_ns"], 1_700_000_000_000_000_000)
            self.assertEqual(row["timestamp_source"], "legacy_seconds")
            self.assertEqual(row["clock_domain"], "unix_wall")
            self.assertIsNone(row["interval_ns"])
            self.assertEqual(row["resource_interference_flag"], 1)
            payload = build_window(
                db.get_server("s1"), [row], devices=[0],
                start_ts=1_700_000_000, end_ts=1_700_000_001,
            )
            self.assertEqual(payload["window"]["timestamp_sources"], ["legacy_seconds"])
            self.assertIn("legacy second-resolution", " ".join(payload["measurement_limitations"]))
        finally:
            db.close()


class PercentileTests(unittest.TestCase):
    def test_percentiles_use_linear_interpolation(self) -> None:
        self.assertEqual(percentile([], 0.5), None)
        self.assertEqual(percentile([7], 0.95), 7)
        self.assertEqual(percentile([0, 10, 20, 30], 0.50), 15)
        self.assertAlmostEqual(percentile([0, 10, 20, 30], 0.95), 28.5)


if __name__ == "__main__":
    unittest.main()
