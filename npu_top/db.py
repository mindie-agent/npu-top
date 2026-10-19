"""SQLite persistence for registered servers, bootstrap receipts, and 15-minute metric rollups.

[Role] Owns `monitor.sqlite3` schema, server registry, idempotent SSH bootstrap receipts, and bounded history rollups.
[Invariants] Configures WAL + `busy_timeout=5000` + `max_page_count` once per thread-local connection; never replays `sending`/`unknown`/`completed` bootstrap receipts.
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
import uuid
from functools import wraps
from pathlib import Path
from typing import Any


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
  bootstrap_receipt_json TEXT,
  UNIQUE(host, port, username)
);
"""

HOST_METRICS = ("cpu_percent", "npu_util_percent", "memory_percent", "hbm_percent", "busy_npu_count")
DEVICE_METRICS = ("utilization_percent", "hbm_percent", "busy_percent")
SCHEMA_VERSION = 2


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

    def initialize(self) -> None:
        with self._write_lock:
            connection = self.connection()
            legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE name='host_samples'").fetchone()
            if legacy:
                raise RuntimeError("Legacy history requires offline migrate-history.py before startup")
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(servers)")}
            if "bootstrap_receipt_json" not in columns:
                with connection:
                    connection.execute("ALTER TABLE servers ADD COLUMN bootstrap_receipt_json TEXT")
            with connection:
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
        receipt = result.pop("bootstrap_receipt_json", None)
        result["bootstrap_receipt"] = json.loads(receipt) if receipt else None
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
                INSERT INTO servers(id, name, host, port, username, tags_json, enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(host, port, username) DO UPDATE SET
                  name=excluded.name, tags_json=excluded.tags_json, enabled=1, updated_at=excluded.updated_at
                """,
                (
                    server["id"], server["name"], server["host"], server["port"], server["username"],
                    json.dumps(server.get("tags", []), ensure_ascii=False), now, now,
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
    ) -> bool:
        assignments = ["updated_at=?"]
        values: list[Any] = [int(time.time())]
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        if tags is not None:
            assignments.append("tags_json=?")
            values.append(json.dumps(tags, ensure_ascii=False))
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

    def bootstrap_receipt(self, server_id: str):
        server = self.get_server(server_id)
        if server is None:
            raise ValueError("bootstrap server is not registered")
        return server["bootstrap_receipt"]

    def reserve_bootstrap(self, server_id: str, method: str, attempts: int):
        with self._write_lock:
            connection = self.connection()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                receipt = self.bootstrap_receipt(server_id)
                if receipt and receipt["state"] in {"sending", "unknown", "completed"}:
                    return False, receipt
                receipt = {
                    "operation_id": uuid.uuid4().hex,
                    "state": "sending",
                    "method": method,
                    "attempts": attempts,
                    "at": int(time.time()),
                }
                connection.execute("UPDATE servers SET bootstrap_receipt_json=? WHERE id=?", (json.dumps(receipt), server_id))
            return True, receipt

    def finish_bootstrap(self, server_id: str, operation_id: str, state: str):
        with self._write_lock:
            connection = self.connection()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                receipt = self.bootstrap_receipt(server_id)
                if receipt is None or receipt["operation_id"] != operation_id:
                    raise RuntimeError("bootstrap receipt identity changed; remote action was not repeated")
                receipt = {**receipt, "state": state, "at": int(time.time())}
                connection.execute("UPDATE servers SET bootstrap_receipt_json=? WHERE id=?", (json.dumps(receipt), server_id))
            return receipt

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

    def aggregate_snapshot(self, server_id: str, snapshot: dict[str, Any]) -> None:
        summary = snapshot.get("summary", {})
        values = dict(summary)
        values["memory_percent"] = self._percent(summary.get("memory_used_bytes"), summary.get("memory_total_bytes"))
        values["hbm_percent"] = self._percent(summary.get("hbm_used_mb"), summary.get("hbm_total_mb"))
        collected = int(snapshot["collected_at"])
        self._rollup("host_rollups", server_id, collected, values, HOST_METRICS)
        for device in snapshot.get("devices", []):
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

    def record_success(self, server_id: str, snapshot: dict[str, Any], persist_sample: bool) -> None:
        with self._write_lock:
            connection = self.connection()
            try:
                with connection:
                    now = int(snapshot["collected_at"])
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

    def latest_persisted(self) -> dict[str, int]:
        rows = self.connection().execute(
            "SELECT server_id,MAX(last_collected_at) FROM host_rollups GROUP BY server_id"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def prune(self, retention_days: int, pressure: bool = False) -> None:
        with self._write_lock:
            connection = self.connection()
            cutoff = int(time.time()) - retention_days * 86400
            for table in ("device_rollups", "host_rollups"):
                while True:
                    with connection:
                        cursor = connection.execute(
                            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE bucket<? LIMIT 1000)",
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
                oldest = connection.execute("SELECT MIN(bucket) FROM host_rollups").fetchone()[0]
                if oldest is None:
                    break
                for table in ("device_rollups", "host_rollups"):
                    while True:
                        with connection:
                            cursor = connection.execute(
                                f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE bucket<? LIMIT 1000)",
                                (oldest + 86400,),
                            )
                        if cursor.rowcount < 1000:
                            break
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
