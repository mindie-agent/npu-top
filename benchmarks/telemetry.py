"""[Role] Measure DCMI translation and persisted fractional-window queries.
[Invariants] Fixture-derived schemas use synthetic clocks and public scalar values;
success requires exact schedule accounting and visible unsupported metrics.
[Timeouts & Locks] Bounded local iterations; SQLite uses the component's WAL budget.
[Side-Effect Policy] Temporary local files and the requested benchmark result only.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from npu_top.dcmi_telemetry import decode_cause_mask  # noqa: E402
from npu_top.db import Database  # noqa: E402
from npu_top.telemetry_window import build_window  # noqa: E402


def decode() -> list[dict]:
    masks = [0, 1, 1 << 2, 1 << 3, (1 << 0) | (1 << 63)]
    assert decode_cause_mask(masks[1], ret=0)["thermal_limit"] is False
    assert decode_cause_mask(masks[2], ret=0)["thermal_limit"] is True
    assert decode_cause_mask(masks[3], ret=0)["power_limit"] is True
    assert decode_cause_mask(masks[4], ret=0)["unknown_bits"] == [63]
    assert decode_cause_mask(0, ret=-1)["thermal_limit"] is None
    iterations = 10_000
    started = time.perf_counter()
    for index in range(iterations):
        decode_cause_mask(masks[index % len(masks)], ret=0)
    return [{"name": "dcmi_cause_decode_latency", "unit": "us/op",
             "value": (time.perf_counter() - started) * 1e6 / iterations,
             "iterations": iterations}]


def persist_query() -> list[dict]:
    captures = [json.loads(line) for line in
                (ROOT / "tests/fixtures/dcmi-metrics-slice.jsonl").read_text().splitlines()]
    template = next(row for row in captures if row["type"] == "sample")
    anchor_ns = 1_700_000_000_025_000_000
    interval_ns = 200_000_000
    rows = []
    for index in range(500):
        row = copy.deepcopy(template)
        old_start = row["wall_before_ns"]
        new_start = anchor_ns + index * interval_ns
        row.update(run_id="release-synthetic", sample_index=index,
                   wall_before_ns=new_start, schedule_slip_ns=0)
        for key in ("wall_after_ns", "started_at_ns"):
            if key in row:
                row[key] += new_start - old_start
        for card in row["cards"].values():
            for getter in card["getters"].values():
                for key in ("started_at_ns", "finished_at_ns"):
                    if isinstance(getter.get(key), int):
                        getter[key] += new_start - old_start
        rows.append(row)
    meta = {"type": "meta", "run_id": "release-synthetic", "cards": [0],
            "interval_s": 0.2, "started_at_ns": anchor_ns, "duration_s": 100}
    start, end = "1700000000.125", "1700000010.875"
    with tempfile.TemporaryDirectory(prefix="npu-top-benchmark-") as directory:
        path = Path(directory)
        source = path / "input.jsonl"
        source.write_text("\n".join(json.dumps(row) for row in [meta, *rows]) + "\n")
        database_path = path / "telemetry.sqlite3"
        db = Database(database_path)
        db.initialize()
        server = db.upsert_server({"id": "synthetic", "name": "synthetic",
                                   "host": "192.0.2.1", "port": 22, "username": "root"})
        started = time.perf_counter()
        imported = db.ingest_dcmi_jsonl(source, server_id="synthetic")
        ingest_ms = (time.perf_counter() - started) * 1000
        assert imported["samples"] == 500, imported
        db.close()
        db = Database(database_path)
        db.initialize()

        def query():
            raw = db.telemetry_samples("synthetic", [0], start, end)
            return build_window(server, raw["samples"], devices=[0],
                                start_ts=start, end_ts=end, runs=raw["runs"],
                                now=1_700_000_101)

        first = query()
        assert first["window"]["start_ts_ns"] == 1_700_000_000_125_000_000
        assert first["window"]["end_ts_ns"] == 1_700_000_010_875_000_000
        assert first["devices"][0]["sample_count"] == 54, first
        assert first["coverage_ratio"] == 1.0, first
        assert first["devices"][0]["hbm_peak_mb"] is None
        assert first["resource_interference_detected"] is None
        iterations = 100
        started = time.perf_counter()
        for _ in range(iterations):
            result = query()
        query_us = (time.perf_counter() - started) * 1e6 / iterations
        assert result["coverage_ratio"] == 1.0
        # A real missing scheduled row must lower coverage after reopening.
        with db.connection() as connection:
            connection.execute("DELETE FROM device_samples WHERE sample_index=25")
        assert query()["coverage_ratio"] < 1.0
        db.close()
    return [
        {"name": "dcmi_500_sample_ingest_latency", "unit": "ms/op",
         "value": ingest_ms, "iterations": 1},
        {"name": "fractional_54_sample_window_latency", "unit": "us/op",
         "value": query_us, "iterations": iterations},
    ]


if __name__ == "__main__":
    metrics = decode() if sys.argv[1] == "decode" else persist_query()
    Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(
        json.dumps({"status": "pass", "metrics": metrics}) + "\n"
    )
