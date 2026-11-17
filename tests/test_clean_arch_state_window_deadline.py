"""Orthogonal behavioral tests for `npu-top` state-dir ownership, streaming 90d windows, and shared probe deadlines.

[Role]
  Verify via independent filesystem, SQLite, memory (`tracemalloc`), and wall-clock oracles that:
  1. Unconfigured `Settings.load()` resolves to owned MindIE/XDG storage without creating
     or polluting `Path.cwd() / "data"`, adopts `./data` ONLY when a genuine `npu-top`
     SQLite database (`monitor.sqlite3` / `npu_top.sqlite3`) already exists (creating zero
     WAL sidecars on read-only check), and ignores arbitrary unrelated `./data` directories.
  2. `Database.telemetry_window` streams 90-day multi-device raw samples via bounded SQLite
     cursor batches (`<= 512 MiB` peak memory, `< 64 MiB` Python allocation delta on 200k rows)
     while matching `build_window` coverage, quantiles, and `measurement_limitations` 100%.
  3. `HostProbe.collect` enforces one shared monotonic deadline across base+infra, frequency,
     DCMI, and process-detail passes so slow sequential sub-probes skip with
     `reason="collection_deadline_exhausted"` instead of exceeding the outer budget, while
     preserving completed base NPU observations and Receipt Honesty on SQLite write errors.

[Invariants]
  * All tests run under sandboxed `tmp_path` environments (`HOME`, `MINDIE_DATA_HOME`, `XDG_DATA_HOME`).

[Timeouts & Locks]
  * Each test completes well within Tier 1 (`< 15s`).

[Side-Effect Policy]
  * Local temporary directories and SQLite files only; zero network or host config writes.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from npu_top.agent_view import npu_status
from npu_top.client import NpuTopClient, VawsTopClient
from npu_top.db import Database
from npu_top.probe import HostProbe
from npu_top.scheduler import AdaptiveScheduler
from npu_top.settings import Settings
from npu_top.telemetry_window import build_window


def test_settings_default_uses_owned_data_dir_and_never_pollutes_unrelated_cwd_data(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "arbitrary-project"
    unrelated_data = fake_cwd / "data"
    unrelated_data.mkdir(parents=True)
    # Unrelated SQLite DB (no npu-top tables) + user CSV inside ./data must NOT be adopted or touched
    other_db = unrelated_data / "monitor.sqlite3"
    with sqlite3.connect(other_db) as conn:
        conn.execute("CREATE TABLE training_metrics (step INTEGER PRIMARY KEY, loss REAL)")
    user_csv = unrelated_data / "dataset.csv"
    user_csv.write_text("id,val\n1,2\n", encoding="utf-8")
    before_files = sorted(p.name for p in unrelated_data.iterdir())

    monkeypatch.chdir(fake_cwd)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("MINDIE_TOP_STATE_DIR", raising=False)
    monkeypatch.delenv("NFM_STATE_DIR", raising=False)
    monkeypatch.delenv("MINDIE_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    s_default = Settings.load()
    assert s_default.state_dir == (fake_home / ".local" / "share" / "mindie-agent" / "npu-top").resolve()
    assert sorted(p.name for p in unrelated_data.iterdir()) == before_files, "read-only probe must not create -wal/-shm"

    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "custom-mindie-data"))
    s_mindie = Settings.load()
    assert s_mindie.state_dir == (tmp_path / "custom-mindie-data" / "npu-top").resolve()

    # Genuine existing ./data/monitor.sqlite3 IS adopted in place without creating WAL files during load()
    legacy_cwd = tmp_path / "legacy-install"
    legacy_data = legacy_cwd / "data"
    legacy_db = Database(legacy_data / "monitor.sqlite3")
    legacy_db.initialize()
    legacy_db.close()
    # Remove any -wal/-shm from init() to verify Settings.load() itself creates zero sidecars
    for suffix in ("-wal", "-shm"):
        sidecar = legacy_data / f"monitor.sqlite3{suffix}"
        if sidecar.exists():
            sidecar.unlink()
    monkeypatch.chdir(legacy_cwd)
    s_legacy = Settings.load()
    assert s_legacy.state_dir == legacy_data.resolve()
    assert not (legacy_data / "monitor.sqlite3-wal").exists()


def test_streaming_90d_telemetry_window_stays_bounded_and_matches_exact_metrics(tmp_path):
    db = Database(tmp_path / "window.sqlite3")
    db.initialize()
    server = db.upsert_server({"id": "a3-01", "name": "a3-01", "host": "192.168.13.165", "port": 22, "username": "root", "tags": ["a3"]})
    sid = server["id"]

    # Span a full 90-day range with 120,000 raw rows across 4 NPU cards + declared schedule run
    start_s = 1_750_000_000
    span_s = 89 * 86400
    end_s = start_s + span_s
    n_ticks = 30_000
    interval_s = span_s // n_ticks
    interval_ns = interval_s * 1_000_000_000
    anchor_ns = start_s * 1_000_000_000

    conn = db.connection()
    with conn:
        conn.execute(
            """
            INSERT INTO telemetry_runs(
                server_id, run_id, clock_domain, cards_json, interval_ns, schedule_anchor_ns,
                first_sample_index, last_sample_index, captured_samples, declared_duration_ns,
                stop_reason, updated_at
            ) VALUES (?, 'run-90d', 'unix_wall', '[0,1,2,3]', ?, ?, 0, ?, ?, ?, 'completed', ?)
            """,
            (sid, interval_ns, anchor_ns, n_ticks - 1, n_ticks - 2, span_s * 1_000_000_000, end_s),
        )
        batch = []
        for idx in range(n_ticks):
            if idx in (7, 1999):  # two missing scheduled points to exercise partial coverage math
                continue
            ts_ns = anchor_ns + idx * interval_ns
            ts_sec = ts_ns / 1_000_000_000
            for card in range(4):
                util = float((idx + card * 13) % 101)
                hbm = float(4096 + ((idx + card) % 1024))
                temp = float(42 + (idx % 25))
                pwr = float(120 + (idx % 80))
                freq = float(1650 - (idx % 50))
                throt = 1 if (card == 2 and idx == 500) else 0
                batch.append((
                    sid, ts_ns, ts_sec, card, "Ascend910C", util, hbm, temp, pwr, freq,
                    throt, 0, interval_ns, 200_000, "dcmi_wall_ns", "unix_wall",
                    "run-90d", idx, ts_ns, idx * interval_ns, ts_ns,
                    ts_ns, ts_ns, ts_ns, ts_ns, ts_ns, ts_ns, '{"power":{"ret":0}}',
                ))
            if len(batch) >= 8000:
                conn.executemany(
                    """
                    INSERT INTO device_samples(
                        server_id, sample_ts_ns, collected_at, npu_id, name, aicore_percent,
                        hbm_used_mb, temperature_c, power_w, freq_mhz, throttling_flag,
                        resource_interference_flag, interval_ns, schedule_slip_ns,
                        timestamp_source, clock_domain, run_id, sample_index, scheduled_ts_ns,
                        monotonic_ns, row_finished_ts_ns, aicore_finished_ns, hbm_finished_ns,
                        temperature_finished_ns, power_finished_ns, freq_finished_ns,
                        throttle_finished_ns, getter_status_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    batch,
                )
                batch.clear()
        if batch:
            conn.executemany(
                """
                INSERT INTO device_samples(
                    server_id, sample_ts_ns, collected_at, npu_id, name, aicore_percent,
                    hbm_used_mb, temperature_c, power_w, freq_mhz, throttling_flag,
                    resource_interference_flag, interval_ns, schedule_slip_ns,
                    timestamp_source, clock_domain, run_id, sample_index, scheduled_ts_ns,
                    monotonic_ns, row_finished_ts_ns, aicore_finished_ns, hbm_finished_ns,
                    temperature_finished_ns, power_finished_ns, freq_finished_ns,
                    throttle_finished_ns, getter_status_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                batch,
            )

    tracemalloc.start()
    try:
        streamed = db.telemetry_window(
            server, [0, 1, 2, 3], start_s, end_s,
            start_ts_ns=anchor_ns, end_ts_ns=anchor_ns + (n_ticks - 1) * interval_ns, now=end_s,
        )
        _cur, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 64 * 1024 * 1024, f"streaming window peak allocation {peak_bytes} exceeded 64 MiB"
    assert streamed["window"]["raw_sample_count"] == (n_ticks - 2) * 4
    assert streamed["window"]["scheduled_sample_count"] == n_ticks
    assert streamed["window"]["observed_sample_count"] == n_ticks - 2
    assert streamed["window"]["missing_sample_count"] == 2
    assert streamed["throttling_detected"] is True
    assert streamed["resource_interference_detected"] is False

    # Verify exact parity against materializing a small sub-window
    sub_end_ns = anchor_ns + 50 * interval_ns
    raw_sub = db.telemetry_samples(sid, [0, 2], start_s, start_s + 50 * interval_s, start_ts_ns=anchor_ns, end_ts_ns=sub_end_ns)
    expected_sub = build_window(
        server, raw_sub["samples"], devices=[0, 2],
        start_ts=start_s, end_ts=start_s + 50 * interval_s,
        start_ts_ns=anchor_ns, end_ts_ns=sub_end_ns,
        runs=raw_sub["runs"], rollup_sample_count=raw_sub["rollup_sample_count"], now=end_s,
    )
    actual_sub = db.telemetry_window(
        server, [0, 2], start_s, start_s + 50 * interval_s,
        start_ts_ns=anchor_ns, end_ts_ns=sub_end_ns, now=end_s, batch_size=11,
    )
    assert actual_sub == expected_sub
    db.close()

    # Sparse high-frequency 100ms 90-day run (index ~77,760,000) with only 16 retained rows must use < 8 MiB
    sparse_runs = [{
        "run_id": "sparse-100ms-90d",
        "clock_domain": "unix_wall",
        "cards_json": "[0,1,2,3,4,5,6,7]",
        "interval_ns": 100_000_000,
        "schedule_anchor_ns": anchor_ns,
        "first_sample_index": 0,
        "last_sample_index": 77_759_999,
    }]
    sparse_rows = [
        {
            "npu_id": card,
            "sample_ts_ns": anchor_ns + idx * 100_000_000,
            "collected_at": (anchor_ns + idx * 100_000_000) / 1_000_000_000,
            "aicore_percent": 50.0,
            "hbm_used_mb": 1024.0,
            "run_id": "sparse-100ms-90d",
            "sample_index": idx,
            "scheduled_ts_ns": anchor_ns + idx * 100_000_000,
            "aicore_finished_ns": anchor_ns + idx * 100_000_000,
            "hbm_finished_ns": anchor_ns + idx * 100_000_000,
        }
        for idx in (0, 77_759_999)
        for card in range(8)
    ]
    tracemalloc.start()
    try:
        sparse_win = build_window(
            server, sparse_rows, devices=list(range(8)),
            start_ts=start_s, end_ts=start_s + 90 * 86400,
            start_ts_ns=anchor_ns, end_ts_ns=anchor_ns + 77_759_999 * 100_000_000,
            runs=sparse_runs, now=end_s,
        )
        _scur, sparse_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sparse_peak < 8 * 1024 * 1024, f"sparse 100ms 90d window allocated {sparse_peak} bytes"
    assert sparse_win["window"]["observed_sample_count"] == 2

    # Adversarial 4096-stride sparse 90-day 8-card stream (18,984 pages * 8 cards = 151,872 rows)
    def page_adversarial_rows():
        for k in range(18_984):
            idx = 4095 + 4096 * k
            ts_ns = anchor_ns + idx * 100_000_000
            for card in range(8):
                yield {
                    "npu_id": card, "sample_ts_ns": ts_ns, "scheduled_ts_ns": ts_ns,
                    "collected_at": ts_ns / 1_000_000_000, "name": "910B4",
                    "aicore_percent": 40.0 + card, "hbm_used_mb": 4096.0,
                    "temperature_c": 52.0, "power_w": 210.0, "freq_mhz": 1650.0,
                    "throttling_flag": 0, "resource_interference_flag": 0,
                    "run_id": "sparse-100ms-90d", "sample_index": idx,
                    "row_finished_ts_ns": ts_ns + 1000, "aicore_finished_ns": ts_ns + 1000,
                    "hbm_finished_ns": ts_ns + 1000, "temperature_finished_ns": ts_ns + 1000,
                    "power_finished_ns": ts_ns + 1000, "freq_finished_ns": ts_ns + 1000,
                    "throttle_finished_ns": ts_ns + 1000,
                    "timestamp_source": "monotonic_calibrated", "clock_domain": "CLOCK_REALTIME",
                }

    tracemalloc.start()
    try:
        adv_win = build_window(
            server, page_adversarial_rows(), devices=list(range(8)),
            start_ts=start_s, end_ts=start_s + 90 * 86400,
            start_ts_ns=anchor_ns, end_ts_ns=anchor_ns + 77_759_999 * 100_000_000,
            runs=sparse_runs, now=end_s,
        )
        _acur, adv_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert adv_peak < 32 * 1024 * 1024, f"4096-stride 151k-row window allocated {adv_peak} bytes"
    assert adv_win["window"]["observed_sample_count"] == 18_984
    assert adv_win["devices"][0]["observed_scheduled_sample_count"] == 18_984


def test_near_quota_16mib_database_startup_succeeds_without_sqlite_full(tmp_path: Path):
    db_path = tmp_path / "near_quota.sqlite3"
    db = Database(db_path, max_bytes=16 * 1024 * 1024)
    db.initialize()
    db.upsert_server({"id": "srv-q", "name": "srv-q", "host": "10.0.0.9", "port": 22, "username": "root"})
    conn = db.connection()
    rows = [
        (
            "srv-q", (1_700_000_000 + step) * 1_000_000_000, float(1_700_000_000 + step),
            card, "Ascend910B4", 50.0, 8192.0, 55.0, 220.0, 1650.0, 0,
            "host_probe", "unix_wall",
        )
        for step in range(5184)
        for card in range(8)
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO device_samples(
                server_id, sample_ts_ns, collected_at, npu_id, name,
                aicore_percent, hbm_used_mb, temperature_c, power_w, freq_mhz,
                throttling_flag, timestamp_source, clock_domain
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    db.close()

    reopened = Database(db_path, max_bytes=16 * 1024 * 1024)
    reopened.initialize()
    assert len(reopened.list_servers()) == 1
    reopened.close()


def test_shared_monotonic_probe_deadline_skips_exhausted_optional_passes_and_preserves_base():
    class FakeAdapter:
        def preflight(self, _server):
            return {"ok": True}

        def parse_npu(self, _info, _usages):
            return {
                "devices": [
                    {
                        "npu_id": 0, "name": "910B4", "aicore_percent": 55.0,
                        "hbm": {"used_mb": 4096, "total_mb": 32768},
                        "processes": [{"pid": 1234, "npu_memory_mb": 2048}],
                    }
                ],
                "process_records": [],
            }

    probe = HostProbe(FakeAdapter(), timeout=20)
    calls: list[tuple[str, int]] = []

    def slow_base_run_script(_server, script, timeout):
        calls.append(("script", timeout))
        if "__NFM_SECTION__proc_stat" in script:
            time.sleep(0.25)
            return subprocess.CompletedProcess(
                [], 0,
                "__NFM_SECTION__hostname\na2-host\n"
                "__NFM_SECTION__proc_stat\ncpu  100 0 50 850 0 0 0 0\n"
                "__NFM_SECTION__meminfo\nMemTotal: 1024 kB\nMemAvailable: 512 kB\n"
                "__NFM_SECTION__npu_info_rc\n0\n",
                "",
            )
        raise AssertionError("optional script pass should have been skipped after collection deadline exhausted")

    probe._run_script = slow_base_run_script  # type: ignore[method-assign]
    probe._run_python = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DCMI must be skipped"))  # type: ignore[method-assign]

    t0 = time.monotonic()
    # Total budget = 2.0s; after 0.25s base pass, remaining ~1.75s < min required for freq (6s), DCMI (9s), proc (4s)
    snap = probe.collect({"id": "s1", "name": "s1", "host": "127.0.0.1"}, False, budget_seconds=2.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5
    assert len(calls) == 1
    assert snap["summary"]["npu_count"] == 1
    assert snap["summary"]["hbm_used_mb"] == 4096
    assert snap["frequency_probe"]["state"] == "skipped"
    assert snap["frequency_probe"]["reason"] == "collection_deadline_exhausted"
    assert snap["dcmi"]["state"] == "skipped"
    assert snap["dcmi"]["reason"] == "collection_deadline_exhausted"
    assert snap["process_details"]["state"] == "skipped"
    assert snap["process_details"]["reason"] == "collection_deadline_exhausted"

    # Also verify include_infrastructure=True with tight 20s budget obeys sum(inner_timeouts) + 4 <= 20
    import re

    seen_infra: list[tuple[str, int]] = []
    probe2 = HostProbe(FakeAdapter(), timeout=20)
    probe2._run_script = lambda _srv, scr, tout: (  # type: ignore[method-assign]
        seen_infra.append((scr, tout))
        or subprocess.CompletedProcess([], 0, "__NFM_SECTION__proc_stat\ncpu  100 0 50 850 0 0 0 0\n__NFM_SECTION__npu_info_rc\n0\n", "")
    )
    probe2._run_python = lambda *a, **kw: subprocess.CompletedProcess([], 0, '{"state":"observed","cards":{}}', "")  # type: ignore[method-assign]
    probe2.collect({"id": "s2", "name": "s2", "host": "127.0.0.1"}, True, budget_seconds=20.0)
    base_scr, base_tout = seen_infra[0]
    inner_vals = [int(x) for x in re.findall(r"\btimeout\s+(\d+)\b", base_scr)]
    assert base_tout == 20
    assert sum(inner_vals) + 4 <= base_tout, (inner_vals, base_tout)

    # 10s budget still executes live INFRA_SCRIPT within sum(inner)+4 <= 10;
    # <9s budget skips INFRA_SCRIPT and forced live infrastructure raises TimeoutError instead of returning stale cache
    seen_10s: list[tuple[str, int]] = []
    probe3 = HostProbe(FakeAdapter(), timeout=20)
    probe3._run_script = lambda _srv, scr, tout: (  # type: ignore[method-assign]
        seen_10s.append((scr, tout))
        or subprocess.CompletedProcess(
            [], 0,
            "__NFM_SECTION__proc_stat\ncpu  100 0 50 850 0 0 0 0\n"
            "__NFM_SECTION__npu_info_rc\n0\n"
            "__NFM_SECTION__disk\n/dev/nvme0n1 1000 400 600 40% /data\n",
            "",
        )
    )
    probe3._run_python = lambda *a, **kw: subprocess.CompletedProcess([], 0, '{"state":"observed","cards":{}}', "")  # type: ignore[method-assign]
    snap_10s = probe3.collect({"id": "s3", "name": "s3", "host": "127.0.0.1"}, True, budget_seconds=10.0)
    assert snap_10s["infrastructure_probe"]["state"] == "observed"
    scr_10s, tout_10s = seen_10s[0]
    assert "__NFM_SECTION__disk" in scr_10s
    assert sum(int(x) for x in re.findall(r"\btimeout\s+(\d+)\b", scr_10s)) + 4 <= tout_10s

    settings = SimpleNamespace(
        idle_interval=120, history_interval=30, infrastructure_interval=60,
        retention_days=90, max_workers=1,
    )
    db_stub = SimpleNamespace(
        latest_persisted=lambda: {}, close=lambda: None,
        list_servers=lambda: [{"id": "s3", "name": "s3", "host": "127.0.0.1", "enabled": True}],
        record_success=lambda *_: None,
        prune=lambda *_: None,
    )
    sched = AdaptiveScheduler(settings, db_stub, probe3)
    sched.start()
    try:
        with pytest.raises(TimeoutError, match="live collection timed out"):
            sched.collect_and_wait("s3", force_infrastructure=True, timeout=5)
        assert "s3" not in sched._last_infra
        assert sched.snapshots()["s3"]["infrastructure_probe"]["state"] == "skipped"
    finally:
        sched.stop()


def test_db_persistence_failure_preserves_live_observation_with_receipt_honesty():
    assert VawsTopClient is NpuTopClient
    settings = SimpleNamespace(
        idle_interval=120, history_interval=30, infrastructure_interval=60,
        retention_days=90, max_workers=1,
    )
    db = SimpleNamespace(
        latest_persisted=lambda: {},
        close=lambda: None,
        list_servers=lambda: [{"id": "s1", "name": "s1", "host": "10.0.0.2", "enabled": True}],
        record_success=lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )
    probe = SimpleNamespace(
        collect=lambda *_: {
            "server_id": "s1", "collected_at": 200,
            "summary": {"npu_count": 8, "busy_npu_count": 2, "hbm_used_mb": 1024, "hbm_total_mb": 65536},
            "devices": [],
        }
    )
    scheduler = AdaptiveScheduler(settings, db, probe)
    scheduler._collect_cycle(set())
    snap = scheduler.snapshots()["s1"]
    assert snap["status"] == "online"
    assert snap["recording"]["state"] == "failed"
    assert snap["recording"]["receipt_unconfirmed"] is True
    view = npu_status({"id": "s1", "name": "s1", "host": "10.0.0.2", "enabled": True}, snap, now=201)
    assert view["server"]["status"] == "online"
    assert view["receipt_unconfirmed"] is True
    assert "live observation preserved" in view["receipt_warning"]


def test_pr_checklist_gate_device_tool_profile_in_npu_top():
    gate_path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_pr_checklist.py"
    spec = importlib.util.spec_from_file_location("npu_top_pr_gate", gate_path)
    gate = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gate)

    template = (Path(__file__).resolve().parents[1] / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert gate.validate_pr_body(template, profile="device-tool")
