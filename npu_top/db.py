"""SQLite persistence for registered servers, raw telemetry, and 15-minute metric rollups.

[Role] Owns `monitor.sqlite3` schema, server registry, bounded raw device telemetry samples, and history rollups.
[Invariants] Configures WAL + `busy_timeout=5000` + `max_page_count` once per thread-local connection; stores only shared authentication references, never credentials.
[Timeouts & Locks] Thread-local SQLite connections with `timeout=5.0`, `_write_lock` (`RLock`), and `_history_slots` (`BoundedSemaphore(2)`) with a `3s` progress-handler deadline.
[Side-Effect Policy] Local SQLite writes and bounded history pruning only; zero network I/O.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any

from . import dcmi_store
from .dcmi_store import SAMPLE_COLUMNS, TELEMETRY_RUN_INDEXES, TELEMETRY_RUN_TABLE
from .telemetry_extract import extract_raw_metrics, seconds_to_ns
from .telemetry_window import run_index_range


SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL DEFAULT 22,
  username TEXT NOT NULL DEFAULT 'root',
  tags_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_seen_at INTEGER,
  last_error TEXT,
  auth_ref TEXT,
  UNIQUE(host, port, username)
);
"""

HOST_METRICS = ("cpu_percent", "npu_util_percent", "memory_percent", "hbm_percent", "busy_npu_count")
DEVICE_METRICS = ("utilization_percent", "hbm_percent", "busy_percent")
SCHEMA_VERSION = 7

DEVICE_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS device_samples (
  server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  sample_ts_ns INTEGER NOT NULL,
  collected_at REAL NOT NULL,
  npu_id INTEGER NOT NULL,
  name TEXT,
  aicore_percent REAL,
  hbm_used_mb REAL,
  temperature_c REAL,
  power_w REAL,
  freq_mhz REAL,
  throttling_flag INTEGER,
  resource_interference_flag INTEGER,
  interval_ns INTEGER,
  schedule_slip_ns INTEGER,
  timestamp_source TEXT,
  clock_domain TEXT,
  run_id TEXT,
  sample_index INTEGER,
  scheduled_ts_ns INTEGER,
  monotonic_ns INTEGER,
  row_finished_ts_ns INTEGER,
  aicore_finished_ns INTEGER,
  hbm_finished_ns INTEGER,
  temperature_finished_ns INTEGER,
  power_finished_ns INTEGER,
  freq_finished_ns INTEGER,
  throttle_finished_ns INTEGER,
  getter_status_json TEXT,
  PRIMARY KEY (server_id, sample_ts_ns, npu_id)
);
"""
DEVICE_SAMPLES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_device_samples_window "
    "ON device_samples(server_id, sample_ts_ns, npu_id);",
    "CREATE INDEX IF NOT EXISTS idx_device_samples_ts ON device_samples(sample_ts_ns);",
    "CREATE INDEX IF NOT EXISTS idx_device_samples_scheduled "
    "ON device_samples(server_id, scheduled_ts_ns, npu_id);",
)


def rollup_schema(table: str, metrics: tuple[str, ...], device: bool = False) -> str:
    columns = ",".join(f"{key}_sum REAL NOT NULL DEFAULT 0,{key}_count INTEGER NOT NULL DEFAULT 0" for key in metrics)
    identity = "npu_id INTEGER NOT NULL,name TEXT," if device else "disk_max_percent REAL,npu_count INTEGER,"
    key = "server_id,bucket,npu_id" if device else "server_id,bucket"
    return f"""CREATE TABLE IF NOT EXISTS {table} (
        server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
        bucket INTEGER NOT NULL,last_collected_at INTEGER NOT NULL,sample_count INTEGER NOT NULL,
        {identity}{columns},PRIMARY KEY ({key}));
        CREATE INDEX IF NOT EXISTS idx_{table}_bucket ON {table}(bucket);"""


SCHEMA += rollup_schema("host_rollups", HOST_METRICS) + rollup_schema("device_rollups", DEVICE_METRICS, True)
SCHEMA += DEVICE_SAMPLES_TABLE + TELEMETRY_RUN_TABLE + "".join(TELEMETRY_RUN_INDEXES)


def bounded_history(method):
    @wraps(method)
    def query(self, *args, **kwargs):
        if not self._history_slots.acquire(blocking=False):
            raise sqlite3.OperationalError("History queries busy; retry later")
        connection = None
        try:
            connection = self.connection()
            deadline = time.monotonic() + 3
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            return method(self, *args, **kwargs)
        finally:
            if connection is not None:
                connection.set_progress_handler(None, 0)
            self._history_slots.release()
    return query


class Database:
    def __init__(self, path: Path, max_bytes: int = 1024 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._write_lock = threading.RLock()
        self._history_slots = threading.BoundedSemaphore(2)
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={max(1, self.max_bytes // page_size)}")
                connection.execute("PRAGMA journal_size_limit=8388608")
                connection.execute("PRAGMA cache_size=-2048")
                connection.execute("PRAGMA busy_timeout=5000")
                self._local.connection = connection
            except BaseException:
                connection.close()
                raise
        return connection

    @staticmethod
    def _create_device_samples(connection: sqlite3.Connection) -> None:
        connection.execute(DEVICE_SAMPLES_TABLE)
        for statement in DEVICE_SAMPLES_INDEXES:
            connection.execute(statement)

    @staticmethod
    def _ensure_device_sample_indexes(connection: sqlite3.Connection) -> None:
        for statement in DEVICE_SAMPLES_INDEXES:
            connection.execute(statement)

    def _migrate_device_samples(
        self, connection: sqlite3.Connection, columns: set[str],
    ) -> None:
        """Rebuild the integer-second table once, preserving all legacy rows."""
        legacy = "device_samples_legacy_seconds"
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (legacy,)).fetchone():
            raise RuntimeError(f"device sample migration is incomplete; {legacy} already exists")
        resource_flag = (
            "resource_interference_flag" if "resource_interference_flag" in columns else "NULL"
        )
        with connection:
            connection.execute(f"ALTER TABLE device_samples RENAME TO {legacy}")
            for index in (
                "idx_device_samples_window", "idx_device_samples_collected", "idx_device_samples_ts",
            ):
                connection.execute(f"DROP INDEX IF EXISTS {index}")
            self._create_device_samples(connection)
            connection.execute(
                f"""
                INSERT INTO device_samples(
                  server_id, sample_ts_ns, collected_at, npu_id, name, aicore_percent,
                  hbm_used_mb, temperature_c, power_w, freq_mhz, throttling_flag,
                  resource_interference_flag, interval_ns, schedule_slip_ns,
                  timestamp_source, clock_domain, run_id
                )
                SELECT server_id, collected_at * 1000000000, collected_at, npu_id, name,
                       aicore_percent, hbm_used_mb, temperature_c, power_w, freq_mhz,
                       throttling_flag, {resource_flag}, NULL, NULL,
                       'legacy_seconds', 'unix_wall', NULL
                FROM {legacy}
                """
            )
            connection.execute(f"DROP TABLE {legacy}")

    def initialize(self) -> None:
        with self._write_lock:
            connection = self.connection()
            legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE name='host_samples'").fetchone()
            if legacy:
                raise RuntimeError("Legacy history requires offline migrate-history.py before startup")
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(servers)")}
            if "auth_ref" not in columns:
                with connection:
                    connection.execute("ALTER TABLE servers ADD COLUMN auth_ref TEXT")
            sample_columns = {row[1] for row in connection.execute("PRAGMA table_info(device_samples)")}
            if "sample_ts_ns" not in sample_columns:
                self._migrate_device_samples(connection, sample_columns)
            else:
                for column, ddl in (
                    ("resource_interference_flag", "INTEGER"),
                    ("interval_ns", "INTEGER"),
                    ("schedule_slip_ns", "INTEGER"),
                    ("timestamp_source", "TEXT"),
                    ("clock_domain", "TEXT"),
                    ("run_id", "TEXT"),
                    ("sample_index", "INTEGER"),
                    ("scheduled_ts_ns", "INTEGER"),
                    ("monotonic_ns", "INTEGER"),
                    ("row_finished_ts_ns", "INTEGER"),
                    ("aicore_finished_ns", "INTEGER"),
                    ("hbm_finished_ns", "INTEGER"),
                    ("temperature_finished_ns", "INTEGER"),
                    ("power_finished_ns", "INTEGER"),
                    ("freq_finished_ns", "INTEGER"),
                    ("throttle_finished_ns", "INTEGER"),
                    ("getter_status_json", "TEXT"),
                ):
                    if column not in sample_columns:
                        with connection:
                            connection.execute(f"ALTER TABLE device_samples ADD COLUMN {column} {ddl}")
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(telemetry_runs)")}
            if "cards_json" not in run_columns:
                with connection:
                    connection.execute("ALTER TABLE telemetry_runs ADD COLUMN cards_json TEXT")
            with connection:
                self._ensure_device_sample_indexes(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            del self._local.connection

    @staticmethod
    def _server(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["tags"] = json.loads(result.pop("tags_json") or "[]")
        result.pop("bootstrap_receipt_json", None)
        return result

    def list_servers(self) -> list[dict[str, Any]]:
        rows = self.connection().execute("SELECT * FROM servers ORDER BY name COLLATE NOCASE").fetchall()
        return [self._server(row) for row in rows]

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        row = self.connection().execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        return self._server(row) if row else None

    def upsert_server(self, server: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        connection = self.connection()
        with connection:
            connection.execute(
                """
                INSERT INTO servers(id, name, host, port, username, tags_json, auth_ref, enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(host, port, username) DO UPDATE SET
                  name=excluded.name, tags_json=excluded.tags_json,
                  auth_ref=COALESCE(excluded.auth_ref, servers.auth_ref), enabled=1, updated_at=excluded.updated_at
                """,
                (
                    server["id"], server["name"], server["host"], server["port"], server["username"],
                    json.dumps(server.get("tags", []), ensure_ascii=False), server.get("auth_ref"), now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM servers WHERE host=? AND port=? AND username=?",
                (server["host"], server["port"], server["username"]),
            ).fetchone()
            return self._server(row)

    def update_server(
        self,
        server_id: str,
        *,
        enabled: bool | None = None,
        tags: list[str] | None = None,
        auth_ref: str | None = None,
    ) -> bool:
        assignments = ["updated_at=?"]
        values: list[Any] = [int(time.time())]
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        if tags is not None:
            assignments.append("tags_json=?")
            values.append(json.dumps(tags, ensure_ascii=False))
        if auth_ref is not None:
            assignments.append("auth_ref=?")
            values.append(auth_ref)
        values.append(server_id)
        connection = self.connection()
        with connection:
            cursor = connection.execute(f"UPDATE servers SET {', '.join(assignments)} WHERE id=?", values)
        return cursor.rowcount > 0

    def delete_server(self, server_id: str) -> bool:
        connection = self.connection()
        with connection:
            cursor = connection.execute("DELETE FROM servers WHERE id=?", (server_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _percent(used: Any, total: Any) -> float | None:
        return used * 100.0 / total if used is not None and total and total > 0 else None

    def _rollup(
        self,
        table: str,
        server_id: str,
        collected: int,
        values: dict[str, Any],
        metrics: tuple[str, ...],
        device: dict[str, Any] | None = None,
    ) -> None:
        bucket = collected // 900 * 900
        fields = ["server_id", "bucket", "last_collected_at", "sample_count"]
        args: list[Any] = [server_id, bucket, collected, 1]
        updates = [
            "last_collected_at=MAX(last_collected_at,excluded.last_collected_at)",
            "sample_count=sample_count+excluded.sample_count",
        ]
        if device is not None:
            fields += ["npu_id", "name"]
            args += [device["npu_id"], device.get("name", "Ascend NPU")]
            updates += ["name=excluded.name"]
        else:
            for key in ("disk_max_percent", "npu_count"):
                fields.append(key)
                args.append(values.get(key))
                updates.append(
                    f"{key}=CASE WHEN {key} IS NULL THEN excluded.{key} "
                    f"WHEN excluded.{key} IS NULL THEN {key} ELSE MAX({key},excluded.{key}) END"
                )
        for key in metrics:
            value = values.get(key)
            valid = isinstance(value, (int, float)) and math.isfinite(value)
            fields += [key + "_sum", key + "_count"]
            args += [value if valid else 0, int(valid)]
            updates += [f"{key}_{suffix}={key}_{suffix}+excluded.{key}_{suffix}" for suffix in ("sum", "count")]
        conflict = "server_id,bucket,npu_id" if device is not None else "server_id,bucket"
        self.connection().execute(
            f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in args)}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {','.join(updates)}",
            args,
        )

    @staticmethod
    def _insert_sample_rows(connection: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
        columns = ",".join(SAMPLE_COLUMNS)
        placeholders = ",".join("?" for _ in SAMPLE_COLUMNS)
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in SAMPLE_COLUMNS
            if column not in ("server_id", "sample_ts_ns", "npu_id")
        )
        connection.executemany(
            f"INSERT INTO device_samples({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(server_id, sample_ts_ns, npu_id) DO UPDATE SET {updates}",
            rows,
        )

    def _record_device_samples(
        self, server_id: str, sample_ts_ns: int, collected_at: float, devices: list[dict[str, Any]],
        *, interval_ns: int | None = None, schedule_slip_ns: int | None = None,
        timestamp_source: str = "probe.collected_at", clock_domain: str = "unix_wall",
        run_id: str | None = None, connection: sqlite3.Connection | None = None,
    ) -> int:
        rows = []
        for device in devices:
            npu_id = device.get("npu_id")
            if isinstance(npu_id, bool) or not isinstance(npu_id, int):
                continue
            metrics = extract_raw_metrics(device)
            rows.append((
                server_id, sample_ts_ns, collected_at, npu_id, metrics["name"],
                metrics["aicore_percent"], metrics["hbm_used_mb"], metrics["temperature_c"],
                metrics["power_w"], metrics["freq_mhz"], metrics["throttling_flag"],
                metrics["resource_interference_flag"], interval_ns, schedule_slip_ns,
                timestamp_source, clock_domain, run_id,
                None, None, None, sample_ts_ns,
                sample_ts_ns, sample_ts_ns, sample_ts_ns, sample_ts_ns,
                sample_ts_ns, sample_ts_ns, None,
            ))
        if not rows:
            return 0
        self._insert_sample_rows(connection or self.connection(), rows)
        return len(rows)

    @staticmethod
    def _snapshot_timestamp_ns(snapshot: dict[str, Any]) -> int:
        raw_ns = snapshot.get("collected_at_ns")
        if raw_ns is not None:
            return int(raw_ns)
        return seconds_to_ns(snapshot["collected_at"])

    def aggregate_snapshot(self, server_id: str, snapshot: dict[str, Any]) -> None:
        summary = snapshot.get("summary", {})
        values = dict(summary)
        values["memory_percent"] = self._percent(summary.get("memory_used_bytes"), summary.get("memory_total_bytes"))
        values["hbm_percent"] = self._percent(summary.get("hbm_used_mb"), summary.get("hbm_total_mb"))
        sample_ts_ns = self._snapshot_timestamp_ns(snapshot)
        collected = sample_ts_ns // 1_000_000_000
        self._rollup("host_rollups", server_id, collected, values, HOST_METRICS)
        devices = snapshot.get("devices", [])
        for device in devices:
            hbm = device.get("hbm") or {}
            self._rollup(
                "device_rollups",
                server_id,
                collected,
                {
                    "utilization_percent": device.get("aicore_percent"),
                    "hbm_percent": self._percent(hbm.get("used_mb"), hbm.get("total_mb")),
                    "busy_percent": 100 if device.get("busy") else 0,
                },
                DEVICE_METRICS,
                device,
            )
        self._record_device_samples(
            server_id, sample_ts_ns, sample_ts_ns / 1_000_000_000, devices,
            interval_ns=snapshot.get("sample_interval_ns"),
            schedule_slip_ns=snapshot.get("schedule_slip_ns"),
            timestamp_source=str(snapshot.get("timestamp_source") or "probe.collected_at"),
            clock_domain=str(snapshot.get("clock_domain") or "unix_wall"),
            run_id=snapshot.get("run_id"),
        )

    def record_success(self, server_id: str, snapshot: dict[str, Any], persist_sample: bool) -> None:
        with self._write_lock:
            connection = self.connection()
            try:
                with connection:
                    sample_ts_ns = self._snapshot_timestamp_ns(snapshot)
                    now = sample_ts_ns // 1_000_000_000
                    connection.execute(
                        "UPDATE servers SET last_seen_at=?,last_error=NULL,updated_at=? WHERE id=?",
                        (now, now, server_id),
                    )
                    if persist_sample:
                        self.aggregate_snapshot(server_id, snapshot)
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_FULL:
                    raise
                logging.error("History capacity exhausted; skipping sample and reclaiming oldest summaries")
                try:
                    self.prune(90, pressure=True)
                except Exception as cleanup:
                    exc.add_note(f"history reclamation also failed: {type(cleanup).__name__}")
                raise

    def record_failure(self, server_id: str, error: str, duration_ms: float | None, persist_event: bool = True) -> None:
        with self._write_lock:
            with self.connection() as connection:
                connection.execute(
                    "UPDATE servers SET last_error=?,updated_at=? WHERE id=?",
                    (error[-1200:], int(time.time()), server_id),
                )

    def record_dcmi_sample(
        self, server_id: str, sample: dict[str, Any], *,
        interval_ns: int | None = None, clock_domain: str = "unix_wall",
        hbm_usage_unit: str | None = None,
    ) -> int:
        return dcmi_store.record_sample(
            self, server_id, sample, interval_ns=interval_ns,
            clock_domain=clock_domain, hbm_usage_unit=hbm_usage_unit,
        )

    def ingest_dcmi_jsonl(
        self, path: Path | str, server_id: str, *,
        interval_ns: int | None = None, clock_domain: str = "unix_wall",
    ) -> dict[str, Any]:
        return dcmi_store.ingest_jsonl(
            self, path, server_id, interval_ns=interval_ns, clock_domain=clock_domain,
        )

    @staticmethod
    def _averages(metrics: tuple[str, ...]) -> str:
        return ",".join(f"SUM({key}_sum)*1.0/NULLIF(SUM({key}_count),0) AS {key}" for key in metrics)

    @bounded_history
    def history(self, server_id: str | None, since: int, bucket_seconds: int) -> list[dict[str, Any]]:
        bucket_seconds = max(900, bucket_seconds)
        where = "bucket >= ?"
        params: list[Any] = [since // 900 * 900]
        if server_id:
            where += " AND server_id=?"
            params.append(server_id)
        rows = self.connection().execute(
            f"""
            SELECT (bucket / ?) * ? AS bucket,{self._averages(HOST_METRICS)},
                   MAX(disk_max_percent) AS disk_max_percent,MAX(npu_count) AS npu_count
            FROM host_rollups WHERE {where} GROUP BY 1 ORDER BY 1
        """,
            [bucket_seconds, bucket_seconds, *params],
        ).fetchall()
        return [dict(row) for row in rows]

    @bounded_history
    def history_heatmap(
        self,
        server_id: str,
        since: int,
        bucket_seconds: int = 7200,
        timezone_offset_seconds: int = 0,
    ) -> list[dict[str, Any]]:
        offset = max(-50400, min(50400, int(timezone_offset_seconds)))
        bucket = max(3600, int(bucket_seconds))
        params = (offset, bucket, bucket, offset, server_id, since // 900 * 900)
        expression = "((bucket + ?) / ?) * ? - ?"
        rows = self.connection().execute(
            f"""
            SELECT {expression} AS time_bucket,SUM(sample_count) AS sample_count,
                   {self._averages(HOST_METRICS)},MAX(disk_max_percent) AS disk_max_percent
            FROM host_rollups WHERE server_id=? AND bucket>=? GROUP BY 1 ORDER BY 1
        """,
            params,
        ).fetchall()
        points = {row["time_bucket"]: {**dict(row), "bucket": row["time_bucket"], "devices": []} for row in rows}
        rows = self.connection().execute(
            f"""
            SELECT {expression} AS time_bucket,npu_id,MAX(name) AS name,{self._averages(DEVICE_METRICS)}
            FROM device_rollups WHERE server_id=? AND bucket>=? GROUP BY 1,npu_id ORDER BY 1,npu_id
        """,
            params,
        ).fetchall()
        for row in rows:
            if row["time_bucket"] in points:
                points[row["time_bucket"]]["devices"].append(
                    {key: row[key] for key in row.keys() if key != "time_bucket"}
                )
        return list(points.values())

    @bounded_history
    def telemetry_samples(
        self, server_id: str, devices: list[int], start_ts: float, end_ts: float,
        *, start_ts_ns: int | None = None, end_ts_ns: int | None = None,
    ) -> dict[str, Any]:
        """Return raw device samples, declared runs, and rollup coverage for one window."""
        if not devices:
            return {
                "samples": [], "runs": [], "rollup_sample_count": 0,
                "start_ts_ns": start_ts_ns, "end_ts_ns": end_ts_ns,
            }
        start_ns = int(start_ts_ns) if start_ts_ns is not None else seconds_to_ns(start_ts)
        end_ns = int(end_ts_ns) if end_ts_ns is not None else seconds_to_ns(end_ts)
        runs = [
            dict(row) for row in self.connection().execute(
                """
                SELECT run_id, clock_domain, cards_json, interval_ns, schedule_anchor_ns,
                       first_sample_index, last_sample_index, captured_samples,
                       declared_duration_ns, stop_reason, meta_json, summary_json
                FROM telemetry_runs
                WHERE server_id=?
                ORDER BY schedule_anchor_ns
                """,
                (server_id,),
            ).fetchall()
        ]
        scheduled_ranges = {
            run["run_id"]: bounds for run in runs
            if (bounds := run_index_range(run, start_ns, end_ns)) is not None
        }
        placeholders = ",".join("?" for _ in devices)
        conditions = []
        condition_params: list[Any] = []
        for run_id, (low, high) in scheduled_ranges.items():
            conditions.append("(run_id=? AND sample_index BETWEEN ? AND ?)")
            condition_params.extend([run_id, low, high])
        scheduled_run_ids = list(scheduled_ranges)
        if scheduled_run_ids:
            run_placeholders = ",".join("?" for _ in scheduled_run_ids)
            conditions.append(
                f"((run_id IS NULL OR sample_index IS NULL OR run_id NOT IN ({run_placeholders})) "
                "AND sample_ts_ns BETWEEN ? AND ?)"
            )
            condition_params.extend([*scheduled_run_ids, start_ns, end_ns])
        else:
            conditions.append("sample_ts_ns BETWEEN ? AND ?")
            condition_params.extend([start_ns, end_ns])
        rows = self.connection().execute(
            f"""
            SELECT npu_id, sample_ts_ns, collected_at, name, aicore_percent, hbm_used_mb,
                   temperature_c, power_w, freq_mhz, throttling_flag,
                   resource_interference_flag, interval_ns, schedule_slip_ns,
                   timestamp_source, clock_domain, run_id, sample_index, scheduled_ts_ns,
                   monotonic_ns, row_finished_ts_ns, aicore_finished_ns, hbm_finished_ns,
                   temperature_finished_ns, power_finished_ns, freq_finished_ns,
                   throttle_finished_ns, getter_status_json,
                   COALESCE(scheduled_ts_ns, sample_ts_ns) AS window_ts_ns
            FROM device_samples
            WHERE server_id=? AND npu_id IN ({placeholders})
              AND ({' OR '.join(conditions)})
            ORDER BY npu_id, sample_index, sample_ts_ns
            """,
            [
                server_id, *[int(device) for device in devices], *condition_params,
            ],
        ).fetchall()
        start_seconds = start_ns // 1_000_000_000
        end_seconds = end_ns // 1_000_000_000
        rollup = self.connection().execute(
            f"""
            SELECT COALESCE(SUM(sample_count), 0)
            FROM device_rollups
            WHERE server_id=? AND bucket>=? AND bucket<=?
              AND npu_id IN ({placeholders})
            """,
            [
                server_id, start_seconds // 900 * 900, end_seconds // 900 * 900,
                *[int(device) for device in devices],
            ],
        ).fetchone()[0]
        return {
            "samples": [dict(row) for row in rows],
            "runs": [dict(row) for row in runs],
            "rollup_sample_count": int(rollup or 0),
            "start_ts_ns": start_ns,
            "end_ts_ns": end_ns,
        }

    def latest_persisted(self) -> dict[str, int]:
        rows = self.connection().execute(
            "SELECT server_id,MAX(last_collected_at) FROM host_rollups GROUP BY server_id"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def prune(self, retention_days: int, pressure: bool = False) -> None:
        with self._write_lock:
            connection = self.connection()
            cutoff_seconds = int(time.time()) - retention_days * 86400
            # Raw telemetry shares the same retention window and pressure
            # reclamation as the rollups; units differ, so scale explicitly.
            tables = (
                ("device_samples", "sample_ts_ns", 1_000_000_000),
                ("telemetry_runs", "updated_at", 1),
                ("device_rollups", "bucket", 1),
                ("host_rollups", "bucket", 1),
            )
            for table, column, multiplier in tables:
                cutoff = cutoff_seconds * multiplier
                while True:
                    with connection:
                        cursor = connection.execute(
                            f"DELETE FROM {table} WHERE rowid IN "
                            f"(SELECT rowid FROM {table} WHERE {column}<? LIMIT 1000)",
                            (cutoff,),
                        )
                    if cursor.rowcount < 1000:
                        break
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            target = self.max_bytes * (0.75 if pressure else 0.85)
            while True:
                used = (
                    connection.execute("PRAGMA page_count").fetchone()[0]
                    - connection.execute("PRAGMA freelist_count").fetchone()[0]
                ) * page_size
                if used < target:
                    break
                existing = []
                for table, column, multiplier in tables:
                    oldest = connection.execute(
                        f"SELECT MIN({column}) FROM {table}"
                    ).fetchone()[0]
                    if oldest is not None:
                        existing.append(int(oldest) // multiplier)
                if not existing:
                    break
                oldest_seconds = min(existing)
                for table, column, multiplier in tables:
                    cutoff = (oldest_seconds + 86400) * multiplier
                    while True:
                        with connection:
                            cursor = connection.execute(
                                f"DELETE FROM {table} WHERE rowid IN "
                                f"(SELECT rowid FROM {table} WHERE {column}<? LIMIT 1000)",
                                (cutoff,),
                            )
                        if cursor.rowcount < 1000:
                            break
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
