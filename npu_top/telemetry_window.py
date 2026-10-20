"""Task-window hardware telemetry over persisted raw device samples.

[Role] Projects declared-schedule raw device samples into a precise `[start_ts, end_ts]`
window with per-device p50/p95, peaks, scheduled/field coverage, throttling and explicit
`measurement_limitations`.
[Invariants] Uses only observed getters finished inside the window; coverage requires
declared sample_index/phase and is `null` otherwise; absent/failed data stays `None` and
is never replaced by temperature inference, fabricated zeros, or elapsed-bucket estimates.
[Timeouts & Locks] Pure in-memory aggregation over caller-supplied rows; zero I/O or locks.
[Side-Effect Policy] Side-effect free.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable

from .agent_view import observation_envelope
from .telemetry_extract import (
    NS_PER_SECOND, extract_dcmi_metrics, extract_raw_metrics, seconds_to_ns,
)

__all__ = [
    "build_window", "extract_dcmi_metrics", "extract_raw_metrics", "percentile",
    "run_index_range", "seconds_to_ns",
]


def percentile(values: Iterable[float], fraction: float) -> float | None:
    """Linear-interpolation percentile; returns `None` for an empty series."""
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = min(1.0, max(0.0, float(fraction))) * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _ratio(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


FIELD_VALUE_COLUMNS = {
    "aicore_percent": "aicore_percent",
    "hbm_used_mb": "hbm_used_mb",
    "temperature_c": "temperature_c",
    "power_w": "power_w",
    "freq_mhz": "freq_mhz",
    "throttling_flag": "throttling_flag",
    "resource_interference_flag": "resource_interference_flag",
}
FIELD_FINISH_COLUMNS = {
    "aicore_percent": "aicore_finished_ns",
    "hbm_used_mb": "hbm_finished_ns",
    "temperature_c": "temperature_finished_ns",
    "power_w": "power_finished_ns",
    "freq_mhz": "freq_finished_ns",
    "throttling_flag": "throttle_finished_ns",
    "resource_interference_flag": None,
}
FIELD_LABELS = {
    "aicore_percent": "AICore utilization",
    "hbm_used_mb": "HBM usage",
    "temperature_c": "Temperature",
    "power_w": "Power",
    "freq_mhz": "Frequency",
    "throttling_flag": "Throttling",
    "resource_interference_flag": "Resource interference",
}


def _ceil_div(value: int, divisor: int) -> int:
    return -((-int(value)) // int(divisor))


def run_index_range(run: dict[str, Any], start_ns: int, end_ns: int) -> tuple[int, int] | None:
    interval_ns = run.get("interval_ns")
    anchor_ns = run.get("schedule_anchor_ns")
    first_index = run.get("first_sample_index")
    last_index = run.get("last_sample_index")
    if (
        isinstance(interval_ns, bool) or not isinstance(interval_ns, int) or interval_ns <= 0
        or isinstance(anchor_ns, bool) or not isinstance(anchor_ns, int)
        or isinstance(first_index, bool) or not isinstance(first_index, int)
        or isinstance(last_index, bool) or not isinstance(last_index, int)
    ):
        return None
    low = max(first_index, _ceil_div(start_ns - anchor_ns, interval_ns))
    high = min(last_index, (end_ns - anchor_ns) // interval_ns)
    return (low, high)


def _run_expected_count(run: dict[str, Any], start_ns: int, end_ns: int) -> int | None:
    bounds = run_index_range(run, start_ns, end_ns)
    return None if bounds is None else max(0, bounds[1] - bounds[0] + 1)


def _scheduled_key(
    row: dict[str, Any], runs: dict[str, dict[str, Any]], start_ns: int, end_ns: int,
) -> tuple[str, int] | None:
    run_id = row.get("run_id")
    index = row.get("sample_index")
    if not isinstance(run_id, str) or isinstance(index, bool) or not isinstance(index, int):
        return None
    run = runs.get(run_id)
    if run is None:
        return None
    bounds = run_index_range(run, start_ns, end_ns)
    if bounds is None or not bounds[0] <= index <= bounds[1]:
        return None
    return (run_id, index)


def _field_finish_ns(row: dict[str, Any], field: str) -> int | None:
    finish_column = FIELD_FINISH_COLUMNS[field]
    if finish_column is None:
        value = row.get("row_finished_ts_ns") or row.get("sample_ts_ns")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    value = row.get(finish_column)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _field_observed(row: dict[str, Any], field: str, end_ns: int) -> bool:
    value = row.get(FIELD_VALUE_COLUMNS[field])
    finish_ns = _field_finish_ns(row, field)
    return value is not None and finish_ns is not None and finish_ns <= end_ns


def _field_late(row: dict[str, Any], field: str, end_ns: int) -> bool:
    finish_ns = _field_finish_ns(row, field)
    return row.get(FIELD_VALUE_COLUMNS[field]) is not None and finish_ns is not None and finish_ns > end_ns


def _getter_status_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        try:
            statuses = json.loads(row.get("getter_status_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(statuses, dict):
            continue
        for name, status in statuses.items():
            if not isinstance(status, dict):
                continue
            item = counts.setdefault(name, {"observed": 0, "failed": 0, "skipped": 0})
            if status.get("skipped"):
                item["skipped"] += 1
            elif status.get("ret") != 0 or status.get("error"):
                item["failed"] += 1
            else:
                item["observed"] += 1
    return counts


def _run_has_card(run: dict[str, Any], npu_id: int) -> bool:
    raw = run.get("cards_json")
    if not raw:
        return True
    try:
        cards = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return True
    return not isinstance(cards, list) or not cards or npu_id in cards


def _device_window(
    npu_id: int,
    rows: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_ranges = {
        run_id: bounds for run_id, run in runs.items()
        if _run_has_card(run, npu_id)
        and (bounds := run_index_range(run, start_ns, end_ns)) is not None
    }
    schedule_available = bool(run_ranges)
    expected = sum(max(0, high - low + 1) for low, high in run_ranges.values()) if schedule_available else None
    scheduled_observed = {
        key for row in rows
        if (key := _scheduled_key(row, runs, start_ns, end_ns)) is not None
    }
    scheduled_row_count = sum(
        1 for row in rows if _scheduled_key(row, runs, start_ns, end_ns) is not None
    )
    unscheduled_row_count = len(rows) - scheduled_row_count
    observed_scheduled = len(scheduled_observed) if schedule_available else None
    if schedule_available and expected:
        coverage = min(1.0, observed_scheduled / expected)
    else:
        coverage = None
    missing = max(0, expected - observed_scheduled) if schedule_available and expected is not None else None
    values = {
        field: [
            row[FIELD_VALUE_COLUMNS[field]] for row in rows
            if _field_observed(row, field, end_ns)
        ]
        for field in FIELD_VALUE_COLUMNS
    }
    field_coverage = {}
    for field, label in FIELD_LABELS.items():
        observed_rows = [row for row in rows if _field_observed(row, field, end_ns)]
        if schedule_available:
            observed_count = len({
                key for row in observed_rows
                if (key := _scheduled_key(row, runs, start_ns, end_ns)) is not None
            })
            field_expected = expected
            ratio = observed_count / expected if expected else None
            basis = "declared_sample_index_phase"
        else:
            observed_count = len(observed_rows)
            field_expected = None
            ratio = None
            basis = "schedule_unavailable"
        field_coverage[field] = {
            "observed": observed_count,
            "expected": field_expected,
            "coverage_ratio": _ratio(ratio) if ratio is not None else None,
            "basis": basis,
            "late_finish_excluded": sum(1 for row in rows if _field_late(row, field, end_ns)),
        }
    aicore = values["aicore_percent"]
    hbm = values["hbm_used_mb"]
    temperature = values["temperature_c"]
    power = values["power_w"]
    frequency = values["freq_mhz"]
    flags = values["throttling_flag"]
    interference_flags = values["resource_interference_flag"]
    if any(flag == 1 for flag in flags):
        throttling: bool | None = True
    elif flags and len(flags) == len(rows):
        throttling = False
    else:
        throttling = None
    if any(flag == 1 for flag in interference_flags):
        interference: bool | None = True
    elif interference_flags and len(interference_flags) == len(rows):
        interference = False
    else:
        interference = None
    timestamps = sorted({
        int(row.get("scheduled_ts_ns") or row["sample_ts_ns"]) for row in rows
    })
    getter_status = _getter_status_counts(rows)
    limitations: list[str] = []
    if not rows:
        limitations.append("No raw sample was observed for this device in the requested window.")
    else:
        if not schedule_available:
            limitations.append(
                "No declared sample_index/schedule phase is available for this device; "
                "scheduled coverage is null, not full."
            )
        elif not expected:
            limitations.append(
                "The window contains no declared scheduled sample points for this device."
            )
        elif coverage < 0.999:
            limitations.append(
                f"Scheduled coverage is {coverage:.1%} ({observed_scheduled}/{expected} sample points); "
                f"{missing} declared point(s) are missing."
            )
        if schedule_available and unscheduled_row_count:
            limitations.append(
                f"{unscheduled_row_count} raw row(s) had no matching declared schedule and are "
                "excluded from scheduled coverage and field counts."
            )
        for field, label in FIELD_LABELS.items():
            item = field_coverage[field]
            if item["late_finish_excluded"]:
                limitations.append(
                    f"{item['late_finish_excluded']} {label} getter value(s) finished after the "
                    "window end and were excluded."
                )
            if item["basis"] == "declared_sample_index_phase" and item["expected"]:
                if item["observed"] < item["expected"]:
                    limitations.append(
                        f"{label} was observed in {item['observed']}/{item['expected']} scheduled "
                        "points; failed, skipped, or late-finished getter values are excluded."
                    )
        failed = {name: item for name, item in getter_status.items() if item["failed"] or item["skipped"]}
        if failed:
            limitations.append(
                "Getter failures/skips: " + ", ".join(
                    f"{name}=failed:{item['failed']}/skipped:{item['skipped']}"
                    for name, item in sorted(failed.items())
                )
            )
        if not frequency:
            limitations.append(
                "No explicit frequency getter was observed by the window end; freq_mhz_min is null."
            )
        if not flags:
            limitations.append(
                "No explicit throttling flag/register was observed by the window end; "
                "throttling_detected is null rather than false. Temperature alone is not "
                "treated as evidence of no throttling."
            )
        elif not any(flags) and len(flags) < len(rows):
            limitations.append(
                f"Explicit throttling flag was missing in {len(rows) - len(flags)} of "
                f"{len(rows)} raw samples; absence of throttling cannot be concluded for them."
            )
    payload = {
        "npu_id": npu_id,
        "name": next((row["name"] for row in rows if row.get("name")), None),
        "sample_count": len(rows),
        "expected_sample_count": expected,
        "observed_scheduled_sample_count": observed_scheduled,
        "unscheduled_row_count": unscheduled_row_count,
        "missing_sample_count": missing,
        "coverage_ratio": _ratio(coverage) if coverage is not None else None,
        "field_coverage": field_coverage,
        "throttling_detected": throttling,
        "resource_interference_detected": interference,
        "aicore_p50": _round(percentile(aicore, 0.50)),
        "aicore_p95": _round(percentile(aicore, 0.95)),
        "hbm_peak_mb": _round(max(hbm) if hbm else None),
        "temp_max_c": _round(max(temperature) if temperature else None),
        "power_max_w": _round(max(power) if power else None),
        "freq_mhz_min": _round(min(frequency) if frequency else None),
        "getter_status": getter_status,
        "timestamp_sources": sorted({
            str(row["timestamp_source"]) for row in rows if row.get("timestamp_source")
        }),
        "clock_domains": sorted({
            str(row["clock_domain"]) for row in rows if row.get("clock_domain")
        }),
        "measurement_limitations": limitations,
    }
    facts = {
        "missing_aicore": not aicore,
        "missing_hbm": not hbm,
        "missing_temperature": not temperature,
        "missing_power": not power,
        "missing_frequency": not frequency,
        "missing_throttle": not flags,
        "partial_throttle": bool(flags) and len(flags) < len(rows),
        "partial_register": any(
            0 < field_coverage[field]["observed"] < len(rows)
            for field in FIELD_VALUE_COLUMNS
        ),
        "coverage": coverage,
        "schedule_available": schedule_available,
        "expected": expected,
        "observed_scheduled": observed_scheduled,
        "missing": missing,
        "max_gap_ns": max(
            (right - left for left, right in zip(timestamps, timestamps[1:])), default=0,
        ),
        "first_sample": timestamps[0] if timestamps else None,
        "last_sample": timestamps[-1] if timestamps else None,
        "has_samples": bool(rows),
    }
    return payload, facts


def _global_limitations(
    device_payloads: list[dict[str, Any]],
    facts: dict[int, dict[str, Any]],
    raw_sample_count: int,
    rollup_sample_count: int,
    schedule_available: bool,
    expected_total: int | None,
    observed_total: int | None,
    phase_drift_ns_max: int | None,
    timestamp_sources: list[str],
    clock_domains: list[str],
) -> list[str]:
    limitations: list[str] = []
    if schedule_available:
        limitations.append(
            "Coverage uses declared sample_index/phase and interval from ingested run metadata; "
            "full scheduled coverage is sampling completeness, not continuous health."
        )
    else:
        limitations.append(
            "No declared sample_index/schedule phase is available; scheduled coverage is null, "
            "not full, and must not be interpreted as continuous health."
        )
    if schedule_available and expected_total == 0:
        limitations.append("The window contains no declared scheduled sample points.")
    if schedule_available and expected_total and observed_total is not None and expected_total > observed_total:
        limitations.append(
            f"{expected_total - observed_total} declared scheduled sample point(s) are missing "
            "from the raw window."
        )
    if phase_drift_ns_max is not None:
        limitations.append(
            "Scheduled wall phase is derived as wall_before_ns - schedule_slip_ns; "
            f"maximum observed phase drift is {phase_drift_ns_max / 1_000_000:g} ms. "
            "Controller clock offset/skew was not measured independently."
        )
    if "legacy_seconds" in timestamp_sources or not timestamp_sources:
        limitations.append(
            "Some samples have no subsecond timestamp source; legacy second-resolution timestamps "
            "are interpreted as Unix wall-clock seconds."
        )
    if clock_domains:
        limitations.append(
            "Timestamp clock domain(s): " + ", ".join(clock_domains)
            + "; window bounds use the same host wall-clock domain."
        )
    else:
        limitations.append(
            "Timestamp clock domain was not recorded; window matching assumes Unix wall-clock seconds."
        )
    if any(item["resource_interference_detected"] is None for item in device_payloads):
        limitations.append(
            "Resource interference was not measured explicitly for every raw sample; "
            "resource_interference_detected is null, not false. Idle or utilization "
            "observations are not treated as interference."
        )
    if raw_sample_count == 0:
        limitations.append(
            "No raw device samples exist for the requested host, devices, and window; "
            "p50/p95, exact peaks, frequency, and throttling cannot be reported."
        )
        if rollup_sample_count:
            limitations.append(
                f"The window contains {rollup_sample_count} 15-minute rollup sample(s), but "
                "rollup aggregates cannot reconstruct a raw distribution or exact maxima; "
                "they are not used for these metrics."
            )
        return limitations
    missing_devices = [str(item["npu_id"]) for item in device_payloads if not facts[item["npu_id"]]["has_samples"]]
    if missing_devices:
        limitations.append(
            "No raw samples were observed for requested device(s) " + ", ".join(missing_devices) + "."
        )
    missing_frequency = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["missing_frequency"]]
    if missing_frequency:
        limitations.append(
            "No explicit frequency getter was observed by the window end for device(s) "
            + ", ".join(missing_frequency) + "; top-level freq_mhz_min remains null."
        )
    missing_throttle = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["missing_throttle"]]
    if missing_throttle:
        limitations.append(
            "No explicit throttling flag/register was observed by the window end for device(s) "
            + ", ".join(missing_throttle) + "; throttling_detected is null rather than false."
        )
    partial_throttle = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["partial_throttle"]]
    if partial_throttle:
        limitations.append(
            "Explicit throttling flag was missing in some raw samples for device(s) "
            + ", ".join(partial_throttle) + "; absence of throttling cannot be concluded for them."
        )
    partial_register = [
        str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["partial_register"]
    ]
    if partial_register:
        limitations.append(
            "One or more getter fields were only partially observed for device(s) "
            + ", ".join(partial_register) + "; per-device field_coverage lists each gap."
        )
    low_coverage = [
        f"{item['npu_id']} ({facts[item['npu_id']]['coverage']:.1%})"
        for item in device_payloads
        if facts[item["npu_id"]]["schedule_available"]
        and facts[item["npu_id"]]["expected"]
        and facts[item["npu_id"]]["coverage"] is not None
        and facts[item["npu_id"]]["coverage"] < 0.999
    ]
    if low_coverage:
        limitations.append(
            "Scheduled coverage is below 100% for device(s) " + ", ".join(low_coverage)
            + "; missing sample points reduce distribution and peak confidence."
        )
    return limitations


def build_window(
    server: dict[str, Any],
    samples: list[dict[str, Any]],
    *,
    devices: list[int],
    start_ts: int | float | str,
    end_ts: int | float | str,
    start_ts_ns: int | None = None,
    end_ts_ns: int | None = None,
    runs: list[dict[str, Any]] | None = None,
    rollup_sample_count: int = 0,
    now: int | None = None,
) -> dict[str, Any]:
    """Project declared-schedule raw samples into the C2 telemetry-window payload."""
    start_ns = int(start_ts_ns) if start_ts_ns is not None else seconds_to_ns(start_ts)
    end_ns = int(end_ts_ns) if end_ts_ns is not None else seconds_to_ns(end_ts)
    if end_ns <= start_ns:
        raise ValueError("telemetry window must have positive duration")
    samples = [dict(row) for row in samples]
    for row in samples:
        if row.get("sample_ts_ns") is None:
            if row.get("collected_at") is None:
                raise ValueError("raw sample lacks sample_ts_ns and collected_at")
            row["sample_ts_ns"] = seconds_to_ns(row["collected_at"])
            row["collected_at"] = int(row["sample_ts_ns"]) / NS_PER_SECOND
        for column in (
            "interval_ns", "timestamp_source", "clock_domain", "run_id", "sample_index",
            "scheduled_ts_ns", "monotonic_ns", "row_finished_ts_ns", "getter_status_json",
        ):
            row.setdefault(column, None)
        row.setdefault("resource_interference_flag", None)
        for field in FIELD_VALUE_COLUMNS:
            row.setdefault(field, None)
        for finish_column in FIELD_FINISH_COLUMNS.values():
            if finish_column is not None:
                row.setdefault(finish_column, None)
        if row.get("run_id") is None:
            for finish_column in FIELD_FINISH_COLUMNS.values():
                if finish_column is not None and row.get(finish_column) is None:
                    row[finish_column] = int(row["sample_ts_ns"])
    runs = [dict(run) for run in runs or []]
    runs_by_id = {str(run["run_id"]): run for run in runs if run.get("run_id")}
    run_ranges = {
        run_id: bounds for run_id, run in runs_by_id.items()
        if (bounds := run_index_range(run, start_ns, end_ns)) is not None
    }
    schedule_available = bool(run_ranges)
    expected_total = sum(max(0, high - low + 1) for low, high in run_ranges.values()) if schedule_available else None
    observed_keys = {
        key for row in samples
        if (key := _scheduled_key(row, runs_by_id, start_ns, end_ns)) is not None
    }
    observed_total = len(observed_keys) if schedule_available else len({
        int(row.get("scheduled_ts_ns") or row["sample_ts_ns"]) for row in samples
    })
    requested = list(dict.fromkeys(int(device) for device in devices))
    grouped: dict[int, list[dict[str, Any]]] = {npu_id: [] for npu_id in requested}
    for row in samples:
        npu_id = int(row["npu_id"])
        if npu_id in grouped:
            grouped[npu_id].append(row)
    device_payloads: list[dict[str, Any]] = []
    facts: dict[int, dict[str, Any]] = {}
    for npu_id in requested:
        payload, device_facts = _device_window(
            npu_id, grouped[npu_id], runs_by_id, start_ns, end_ns,
        )
        device_payloads.append(payload)
        facts[npu_id] = device_facts
    coverages = [item["coverage_ratio"] for item in device_payloads]
    coverage = (
        min(coverages) if coverages and all(value is not None for value in coverages) else None
    )
    observed_flags = [item["throttling_detected"] for item in device_payloads]
    if any(flag is True for flag in observed_flags):
        throttling: bool | None = True
    elif observed_flags and all(flag is False for flag in observed_flags):
        throttling = False
    else:
        throttling = None
    interference_flags = [item["resource_interference_detected"] for item in device_payloads]
    if any(flag is True for flag in interference_flags):
        interference: bool | None = True
    elif interference_flags and all(flag is False for flag in interference_flags):
        interference = False
    else:
        interference = None
    frequency_values = [item["freq_mhz_min"] for item in device_payloads]
    power_values = [item["power_max_w"] for item in device_payloads]
    temperature_values = [item["temp_max_c"] for item in device_payloads]
    fleet_frequency = (
        min(frequency_values)
        if frequency_values and all(value is not None for value in frequency_values)
        else None
    )
    fleet_power = (
        max(power_values)
        if power_values and all(value is not None for value in power_values)
        else None
    )
    fleet_temperature = (
        max(temperature_values)
        if temperature_values and all(value is not None for value in temperature_values)
        else None
    )
    window_timestamps = [int(row.get("scheduled_ts_ns") or row["sample_ts_ns"]) for row in samples]
    first_sample_ns = min(window_timestamps, default=None)
    last_sample_ns = max(window_timestamps, default=None)
    unique_timestamps = sorted(set(window_timestamps))
    gaps_seconds = [
        (right - left) / NS_PER_SECOND for left, right in zip(unique_timestamps, unique_timestamps[1:])
    ]
    observed_interval_seconds = (
        {
            "min": _round(min(gaps_seconds)),
            "median": _round(sorted(gaps_seconds)[len(gaps_seconds) // 2]),
            "max": _round(max(gaps_seconds)),
        }
        if gaps_seconds else None
    )
    slips = [int(row["schedule_slip_ns"]) for row in samples
             if row.get("schedule_slip_ns") is not None]
    phase_drifts = []
    for row in samples:
        run_id = row.get("run_id")
        index = row.get("sample_index")
        run = runs_by_id.get(run_id) if isinstance(run_id, str) else None
        if run is None or not isinstance(index, int) or row.get("scheduled_ts_ns") is None:
            continue
        interval_ns = run.get("interval_ns")
        anchor_ns = run.get("schedule_anchor_ns")
        if isinstance(interval_ns, int) and isinstance(anchor_ns, int) and interval_ns > 0:
            expected_ts = anchor_ns + index * interval_ns
            phase_drifts.append(abs(int(row["scheduled_ts_ns"]) - expected_ts))
    phase_drift_ns_max = max(phase_drifts) if phase_drifts else None
    intervals = sorted({
        int(run["interval_ns"]) for run in runs_by_id.values()
        if isinstance(run.get("interval_ns"), int) and not isinstance(run.get("interval_ns"), bool)
        and run["interval_ns"] > 0
    })
    timestamp_sources = sorted({
        str(row["timestamp_source"]) for row in samples if row.get("timestamp_source")
    })
    clock_domains = sorted({
        str(row["clock_domain"]) for row in samples if row.get("clock_domain")
    } | {
        str(run["clock_domain"]) for run in runs_by_id.values() if run.get("clock_domain")
    })
    observation = observation_envelope(
        last_sample_ns / NS_PER_SECOND if last_sample_ns is not None else None, now,
    )
    observation["observed_at_ns"] = last_sample_ns
    observation["notice"] = (
        "Historical raw device telemetry for the requested window. Not an allocation or "
        "reservation source; verify declared schedule, timestamp, interval, and measurement "
        "limitations before attributing performance."
    )
    return {
        "source": "sqlite",
        "observation": observation,
        "server": {
            "id": server["id"], "name": server["name"], "host": server["host"],
            "port": server.get("port"), "enabled": bool(server.get("enabled")),
            "tags": server.get("tags") or [],
        },
        "window": {
            "start_ts": start_ns / NS_PER_SECOND,
            "end_ts": end_ns / NS_PER_SECOND,
            "start_ts_ns": start_ns,
            "end_ts_ns": end_ns,
            "duration_seconds": (end_ns - start_ns) / NS_PER_SECOND,
            "sample_interval_seconds": (
                _round(intervals[0] / NS_PER_SECOND)
                if len(intervals) == 1 else None
            ),
            "sample_interval_ns": intervals[0] if len(intervals) == 1 else None,
            "interval_source": "run_metadata" if intervals else "unavailable",
            "coverage_basis": (
                "declared_sample_index_phase" if schedule_available else "schedule_unavailable"
            ),
            "timestamp_sources": timestamp_sources or ["unknown"],
            "clock_domains": clock_domains or ["unknown"],
            "sampling_basis": "raw_device_samples",
            "coverage_definition": (
                "observed declared sample_index keys divided by scheduled keys derived from "
                "run interval and phase; no elapsed-bucket or tolerance estimate"
            ),
            "first_sample_ts_ns": first_sample_ns,
            "last_sample_ts_ns": last_sample_ns,
            "observed_interval_seconds": observed_interval_seconds,
            "schedule_slip_ms_max": _round(max(slips) / 1_000_000) if slips else None,
            "schedule_phase_drift_ns_max": phase_drift_ns_max,
            "scheduled_sample_count": expected_total,
            "observed_sample_count": observed_total,
            "missing_sample_count": (
                max(0, expected_total - observed_total)
                if expected_total is not None and observed_total is not None else None
            ),
            "schedule_runs": [
                {
                    "run_id": run_id,
                    "interval_ns": run.get("interval_ns"),
                    "schedule_anchor_ns": run.get("schedule_anchor_ns"),
                    "first_sample_index": run.get("first_sample_index"),
                    "last_sample_index": run.get("last_sample_index"),
                    "clock_domain": run.get("clock_domain"),
                    "stop_reason": run.get("stop_reason"),
                }
                for run_id, run in sorted(runs_by_id.items())
            ],
            "requested_devices": requested,
            "raw_sample_count": len(samples),
            "rollup_sample_count": int(rollup_sample_count),
        },
        "coverage_ratio": _ratio(coverage) if coverage is not None else None,
        "throttling_detected": throttling,
        "resource_interference_detected": interference,
        "freq_mhz_min": _round(fleet_frequency),
        "power_max_w": _round(fleet_power),
        "temp_max_c": _round(fleet_temperature),
        "measurement_limitations": _global_limitations(
            device_payloads, facts, len(samples), int(rollup_sample_count),
            schedule_available, expected_total, observed_total, phase_drift_ns_max,
            timestamp_sources, clock_domains,
        ),
        "devices": device_payloads,
    }
