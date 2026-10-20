"""DCMI JSONL persistence into subsecond raw samples and declared run schedules.

[Role] Builds raw sample rows with per-field finish timestamps/getter status and
persists the stream's declared sample index, interval, schedule anchor and bounds.
[Invariants] Only observed getter values are stored; a missing/failed getter stays
`None`, the original stream is read-only, and schedule fields are never invented.
[Timeouts & Locks] Caller owns the database write lock/timeouts; functions do no I/O
except reading the explicitly supplied JSONL path.
[Side-Effect Policy] Local SQLite writes only; no network, NPU, or source mutation.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .telemetry_extract import dcmi_timestamp_ns, extract_dcmi_metrics, seconds_to_ns


TELEMETRY_RUN_TABLE = """
CREATE TABLE IF NOT EXISTS telemetry_runs (
  server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  clock_domain TEXT NOT NULL,
  cards_json TEXT,
  interval_ns INTEGER,
  schedule_anchor_ns INTEGER,
  first_sample_index INTEGER,
  last_sample_index INTEGER,
  captured_samples INTEGER,
  declared_duration_ns INTEGER,
  stop_reason TEXT,
  meta_json TEXT,
  summary_json TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (server_id, run_id, clock_domain)
);
"""
TELEMETRY_RUN_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_telemetry_runs_window "
    "ON telemetry_runs(server_id, schedule_anchor_ns, last_sample_index);",
)

SAMPLE_COLUMNS = (
    "server_id", "sample_ts_ns", "collected_at", "npu_id", "name", "aicore_percent",
    "hbm_used_mb", "temperature_c", "power_w", "freq_mhz", "throttling_flag",
    "resource_interference_flag", "interval_ns", "schedule_slip_ns", "timestamp_source",
    "clock_domain", "run_id", "sample_index", "scheduled_ts_ns", "monotonic_ns",
    "row_finished_ts_ns", "aicore_finished_ns", "hbm_finished_ns",
    "temperature_finished_ns", "power_finished_ns", "freq_finished_ns",
    "throttle_finished_ns", "getter_status_json",
)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _sample_index(sample: dict[str, Any]) -> int | None:
    value = _int_or_none(sample.get("sample_index"))
    return value if value is not None and value >= 0 else None


def _scheduled_ts_ns(sample_ts_ns: int, schedule_slip_ns: int | None) -> int | None:
    return sample_ts_ns - schedule_slip_ns if schedule_slip_ns is not None else None


def _run_anchor_ns(scheduled_ts_ns: int | None, sample_index: int | None, interval_ns: int | None) -> int | None:
    if scheduled_ts_ns is None or sample_index is None or interval_ns is None:
        return None
    return scheduled_ts_ns - sample_index * interval_ns


def _rows_for_sample(
    server_id: str, sample: dict[str, Any], *, interval_ns: int | None,
    clock_domain: str, hbm_usage_unit: str | None, run_id: str | None,
) -> tuple[list[dict[str, Any]], int, str, int | None, int | None]:
    sample_ts_ns, timestamp_source = dcmi_timestamp_ns(sample)
    cards = sample.get("cards")
    if not isinstance(cards, dict) or not cards:
        raise ValueError("DCMI sample has no cards object")
    sample_index = _sample_index(sample)
    slip_ns = _int_or_none(sample.get("schedule_slip_ns"))
    scheduled_ts_ns = _scheduled_ts_ns(sample_ts_ns, slip_ns)
    monotonic_ns = _int_or_none(sample.get("monotonic_ns"))
    row_finished_ts_ns = _int_or_none(sample.get("wall_after_ns"))
    rows = []
    for raw_card, card in cards.items():
        if not isinstance(card, dict):
            raise ValueError(f"DCMI card {raw_card!r} is not an object")
        try:
            npu_id = int(raw_card)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"DCMI card id is not an integer: {raw_card!r}") from exc
        metrics = extract_dcmi_metrics(card, hbm_usage_unit=hbm_usage_unit)
        finished = metrics["field_finished_ns"]
        row = {
            "server_id": server_id,
            "sample_ts_ns": sample_ts_ns,
            "collected_at": sample_ts_ns / 1_000_000_000,
            "npu_id": npu_id,
            "name": metrics["name"],
            "aicore_percent": metrics["aicore_percent"],
            "hbm_used_mb": metrics["hbm_used_mb"],
            "temperature_c": metrics["temperature_c"],
            "power_w": metrics["power_w"],
            "freq_mhz": metrics["freq_mhz"],
            "throttling_flag": metrics["throttling_flag"],
            "resource_interference_flag": metrics["resource_interference_flag"],
            "interval_ns": interval_ns,
            "schedule_slip_ns": slip_ns,
            "timestamp_source": timestamp_source,
            "clock_domain": clock_domain,
            "run_id": run_id or sample.get("run_id"),
            "sample_index": sample_index,
            "scheduled_ts_ns": scheduled_ts_ns,
            "monotonic_ns": monotonic_ns,
            "row_finished_ts_ns": row_finished_ts_ns,
            "aicore_finished_ns": finished["aicore_percent"],
            "hbm_finished_ns": finished["hbm_used_mb"],
            "temperature_finished_ns": finished["temperature_c"],
            "power_finished_ns": finished["power_w"],
            "freq_finished_ns": finished["freq_mhz"],
            "throttle_finished_ns": finished["throttling_flag"],
            "getter_status_json": json.dumps(
                metrics["getter_status"], sort_keys=True, separators=(",", ":"),
            ),
        }
        rows.append(row)
    if not rows:
        raise ValueError("DCMI sample produced no device rows")
    return rows, sample_ts_ns, timestamp_source, sample_index, scheduled_ts_ns


def _upsert_run(
    connection: Any, server_id: str, run_id: str | None, *, interval_ns: int | None,
    anchor_ns: int | None, first_index: int | None, last_index: int | None,
    captured_samples: int | None, declared_duration_ns: int | None,
    stop_reason: str | None, clock_domain: str, cards: list[int] | None,
    meta: dict[str, Any] | None, summary: dict[str, Any] | None,
) -> None:
    if not run_id:
        return
    now = int(time.time())
    connection.execute(
        """
        INSERT INTO telemetry_runs(
          server_id, run_id, clock_domain, cards_json, interval_ns, schedule_anchor_ns,
          first_sample_index, last_sample_index, captured_samples,
          declared_duration_ns, stop_reason, meta_json, summary_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(server_id, run_id, clock_domain) DO UPDATE SET
          cards_json=COALESCE(excluded.cards_json, telemetry_runs.cards_json),
          interval_ns=COALESCE(telemetry_runs.interval_ns, excluded.interval_ns),
          schedule_anchor_ns=COALESCE(telemetry_runs.schedule_anchor_ns, excluded.schedule_anchor_ns),
          first_sample_index=CASE
            WHEN telemetry_runs.first_sample_index IS NULL THEN excluded.first_sample_index
            WHEN excluded.first_sample_index IS NULL THEN telemetry_runs.first_sample_index
            ELSE MIN(telemetry_runs.first_sample_index, excluded.first_sample_index) END,
          last_sample_index=CASE
            WHEN telemetry_runs.last_sample_index IS NULL THEN excluded.last_sample_index
            WHEN excluded.last_sample_index IS NULL THEN telemetry_runs.last_sample_index
            ELSE MAX(telemetry_runs.last_sample_index, excluded.last_sample_index) END,
          captured_samples=COALESCE(excluded.captured_samples, telemetry_runs.captured_samples),
          declared_duration_ns=COALESCE(excluded.declared_duration_ns, telemetry_runs.declared_duration_ns),
          stop_reason=COALESCE(excluded.stop_reason, telemetry_runs.stop_reason),
          meta_json=COALESCE(excluded.meta_json, telemetry_runs.meta_json),
          summary_json=COALESCE(excluded.summary_json, telemetry_runs.summary_json),
          updated_at=excluded.updated_at
        """,
        (
            server_id, run_id, clock_domain,
            json.dumps(sorted({int(card) for card in cards}), sort_keys=True) if cards else None,
            interval_ns, anchor_ns, first_index, last_index, captured_samples,
            declared_duration_ns, stop_reason,
            json.dumps(meta, sort_keys=True) if meta is not None else None,
            json.dumps(summary, sort_keys=True) if summary is not None else None,
            now,
        ),
    )


def record_sample(
    db: Any, server_id: str, sample: dict[str, Any], *,
    interval_ns: int | None = None, clock_domain: str = "unix_wall",
    hbm_usage_unit: str | None = None,
) -> int:
    """Persist one DCMI sampler record and update its declared run bounds."""
    if db.get_server(server_id) is None:
        raise ValueError(f"server is not registered: {server_id}")
    run_id = sample.get("run_id")
    rows, sample_ts_ns, _source, sample_index, scheduled_ts_ns = _rows_for_sample(
        server_id, sample, interval_ns=interval_ns, clock_domain=clock_domain,
        hbm_usage_unit=hbm_usage_unit, run_id=run_id,
    )
    anchor_ns = _run_anchor_ns(scheduled_ts_ns, sample_index, interval_ns)
    with db._write_lock:
        connection = db.connection()
        with connection:
            db._insert_sample_rows(
                connection, [tuple(row[column] for column in SAMPLE_COLUMNS) for row in rows],
            )
            seconds = sample_ts_ns // 1_000_000_000
            connection.execute(
                "UPDATE servers SET last_seen_at=?,updated_at=? WHERE id=?",
                (seconds, seconds, server_id),
            )
            _upsert_run(
                connection, server_id, run_id, interval_ns=interval_ns, anchor_ns=anchor_ns,
                first_index=sample_index, last_index=sample_index, captured_samples=None,
                declared_duration_ns=None, stop_reason=None, clock_domain=clock_domain,
                cards=sorted({int(card) for card in sample.get("cards", {})}), meta=None, summary=None,
            )
    return len(rows)


def ingest_jsonl(
    db: Any, path: Path | str, server_id: str, *,
    interval_ns: int | None = None, clock_domain: str = "unix_wall",
) -> dict[str, Any]:
    """Replay a DCMI JSONL stream, preserving declared schedule and getter status."""
    if db.get_server(server_id) is None:
        raise ValueError(f"server is not registered: {server_id}")
    capture_path = Path(path)
    meta: dict[str, Any] | None = None
    capture_summary: dict[str, Any] | None = None
    run_id: str | None = None
    meta_interval_ns = interval_ns
    hbm_usage_unit: str | None = None
    pending: list[tuple[Any, ...]] = []
    sample_count = 0
    row_count = 0
    first_ts_ns: int | None = None
    last_ts_ns: int | None = None
    sample_indices: list[int] = []
    scheduled_ts: list[tuple[int, int]] = []
    timestamp_sources: set[str] = set()
    cards_seen: set[int] = set()

    def flush(connection: Any) -> None:
        nonlocal pending, row_count
        if pending:
            db._insert_sample_rows(connection, pending)
            row_count += len(pending)
            pending = []

    with db._write_lock:
        connection = db.connection()
        with connection:
            with capture_path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{capture_path}:{line_number}: invalid JSON") from exc
                    if not isinstance(record, dict):
                        raise ValueError(f"{capture_path}:{line_number}: record must be an object")
                    kind = record.get("type")
                    if kind == "meta":
                        if meta is not None and record.get("run_id") != run_id:
                            raise ValueError(f"{capture_path}:{line_number}: multiple run ids")
                        meta = record
                        run_id = record.get("run_id") or run_id
                        if meta_interval_ns is None and record.get("interval_s") is not None:
                            meta_interval_ns = seconds_to_ns(record["interval_s"])
                        units = record.get("units") if isinstance(record.get("units"), dict) else {}
                        unit = units.get("hbm_memory_usage")
                        hbm_usage_unit = unit if isinstance(unit, str) else None
                    elif kind == "sample":
                        rows, sample_ts_ns, source, sample_index, scheduled_ts_ns = _rows_for_sample(
                            server_id, record, interval_ns=meta_interval_ns,
                            clock_domain=clock_domain, hbm_usage_unit=hbm_usage_unit,
                            run_id=run_id or record.get("run_id"),
                        )
                        pending.extend(tuple(row[column] for column in SAMPLE_COLUMNS) for row in rows)
                        cards_seen.update(int(card) for card in record.get("cards", {}))
                        sample_count += 1
                        first_ts_ns = sample_ts_ns if first_ts_ns is None else min(first_ts_ns, sample_ts_ns)
                        last_ts_ns = sample_ts_ns if last_ts_ns is None else max(last_ts_ns, sample_ts_ns)
                        timestamp_sources.add(source)
                        run_id = record.get("run_id") or run_id
                        if sample_index is not None:
                            sample_indices.append(sample_index)
                        if sample_index is not None and scheduled_ts_ns is not None:
                            scheduled_ts.append((sample_index, scheduled_ts_ns))
                        if len(pending) >= 1000:
                            flush(connection)
                    elif kind == "summary":
                        capture_summary = record
                    elif kind == "error":
                        raise ValueError(f"{capture_path}:{line_number}: DCMI capture error: {record}")
                    else:
                        raise ValueError(f"{capture_path}:{line_number}: unknown record type {kind!r}")
            if sample_count == 0:
                raise ValueError(f"{capture_path}: capture contains no sample records")
            flush(connection)
            anchor_candidates = [
                scheduled_ts_ns - index * meta_interval_ns
                for index, scheduled_ts_ns in scheduled_ts
                if meta_interval_ns is not None
            ]
            anchor_ns = (
                sorted(anchor_candidates)[len(anchor_candidates) // 2]
                if anchor_candidates else None
            )
            summary_count = None
            if capture_summary is not None:
                summary_count = _int_or_none(capture_summary.get("sample_count"))
            first_index = min(sample_indices) if sample_indices else None
            last_index = max(sample_indices) if sample_indices else None
            if summary_count is not None and summary_count > 0:
                first_index = 0
                last_index = max(last_index or 0, summary_count - 1)
            declared_duration_ns = (
                seconds_to_ns(meta.get("duration_s"))
                if meta is not None and meta.get("duration_s") is not None else None
            )
            stop_reason = (
                capture_summary.get("stop_reason")
                if capture_summary is not None else None
            )
            _upsert_run(
                connection, server_id, run_id, interval_ns=meta_interval_ns,
                anchor_ns=anchor_ns, first_index=first_index, last_index=last_index,
                captured_samples=summary_count if summary_count is not None else sample_count,
                declared_duration_ns=declared_duration_ns, stop_reason=stop_reason,
                clock_domain=clock_domain,
                cards=sorted(cards_seen) if cards_seen else (
                    [int(card) for card in meta.get("cards", [])] if meta else None
                ),
                meta=meta, summary=capture_summary,
            )
            seconds = last_ts_ns // 1_000_000_000 if last_ts_ns is not None else int(time.time())
            connection.execute(
                "UPDATE servers SET last_seen_at=?,updated_at=? WHERE id=?",
                (seconds, seconds, server_id),
            )
    scheduled_values = [scheduled_ts_ns for _index, scheduled_ts_ns in scheduled_ts]
    return {
        "state": "completed",
        "server_id": server_id,
        "run_id": run_id,
        "samples": sample_count,
        "rows": row_count,
        "first_ts_ns": first_ts_ns,
        "last_ts_ns": last_ts_ns,
        "first_scheduled_ts_ns": min(scheduled_values) if scheduled_values else None,
        "last_scheduled_ts_ns": max(scheduled_values) if scheduled_values else None,
        "interval_ns": meta_interval_ns,
        "interval_seconds": meta_interval_ns / 1_000_000_000 if meta_interval_ns is not None else None,
        "schedule_anchor_ns": anchor_ns,
        "first_sample_index": first_index,
        "last_sample_index": last_index,
        "timestamp_sources": sorted(timestamp_sources),
        "clock_domain": clock_domain,
        "meta": meta,
        "capture_summary": capture_summary,
    }
