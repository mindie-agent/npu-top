"""Task-window hardware telemetry over persisted raw device samples.

[Role] Projects declared-schedule raw device samples into a precise `[start_ts, end_ts]`
window with per-device p50/p95, peaks, scheduled/field coverage, throttling and explicit
`measurement_limitations` in a single bounded-memory streaming pass.
[Invariants] Consumes any row iterable (`sqlite3.Row` cursor batches or dicts) without
materializing full-window row lists (`<= 512 MiB` peak RSS even on 90-day windows). Uses
only observed getters finished inside the window; coverage requires declared
`sample_index`/phase and is `null` otherwise (including unusable `cards_json`); absent or
failed data stays `None` and is never replaced by temperature inference or fabricated zeros.
[Timeouts & Locks] Pure single-pass streaming aggregation; zero I/O or locks.
[Side-Effect Policy] Side-effect free; never mutates caller-supplied row mappings.
"""
from __future__ import annotations

from array import array
import json
import math
from typing import Any, Iterable, Mapping

from .agent_view import observation_envelope
from .telemetry_extract import (
    NS_PER_SECOND, extract_dcmi_metrics, extract_raw_metrics, seconds_to_ns,
)

__all__ = [
    "build_window", "extract_dcmi_metrics", "extract_raw_metrics", "percentile",
    "run_index_range", "seconds_to_ns",
]


def _percentile_sorted(ordered: list[float], fraction: float) -> float | None:
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


def percentile(values: Iterable[float], fraction: float) -> float | None:
    """Linear-interpolation percentile; returns `None` for an empty series."""
    ordered = sorted(float(value) for value in values if value is not None)
    return _percentile_sorted(ordered, fraction)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _ratio(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _col(row: Mapping[str, Any], key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


FIELD_VALUE_COLUMNS = {
    "aicore_percent": "aicore_percent", "hbm_used_mb": "hbm_used_mb",
    "temperature_c": "temperature_c", "power_w": "power_w", "freq_mhz": "freq_mhz",
    "throttling_flag": "throttling_flag", "resource_interference_flag": "resource_interference_flag",
}
FIELD_FINISH_COLUMNS = {
    "aicore_percent": "aicore_finished_ns", "hbm_used_mb": "hbm_finished_ns",
    "temperature_c": "temperature_finished_ns", "power_w": "power_finished_ns",
    "freq_mhz": "freq_finished_ns", "throttling_flag": "throttle_finished_ns",
    "resource_interference_flag": None,
}
FIELD_LABELS = {
    "aicore_percent": "AICore utilization", "hbm_used_mb": "HBM usage",
    "temperature_c": "Temperature", "power_w": "Power", "freq_mhz": "Frequency",
    "throttling_flag": "Throttling", "resource_interference_flag": "Resource interference",
}


def _ceil_div(value: int, divisor: int) -> int:
    return -((-int(value)) // int(divisor))


def run_index_range(run: Mapping[str, Any], start_ns: int, end_ns: int) -> tuple[int, int] | None:
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


def _card_filter(run: Mapping[str, Any]) -> tuple[frozenset[int] | None, str | None]:
    """Return `(card_ids, error)`; `None` with no error means run applies to all cards."""
    raw = run.get("cards_json")
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, (bytes, bytearray, str)):
        try:
            cards = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None, "cards_json_unparseable"
    elif isinstance(raw, list):
        cards = raw
    else:
        return None, "cards_json_invalid_filter"
    if not isinstance(cards, list):
        return None, "cards_json_invalid_filter"
    ids: list[int] = []
    for item in cards:
        if isinstance(item, bool) or not isinstance(item, int):
            return None, "cards_json_invalid_filter"
        ids.append(item)
    return frozenset(ids), None


def _unusable_card_runs(
    runs: dict[str, dict[str, Any]], start_ns: int, end_ns: int,
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for run_id, run in sorted(runs.items()):
        if run_index_range(run, start_ns, end_ns) is None:
            continue
        _ids, error = _card_filter(run)
        if error:
            found.append((run_id, error))
    return found


def _unusable_cards_limitation(items: list[tuple[str, str]], *, for_device: bool) -> str:
    grouped: dict[str, list[str]] = {}
    for run_id, error in items:
        grouped.setdefault(error, []).append(run_id)
    detail = "; ".join(
        f"{error} run(s) " + ", ".join(run_ids) for error, run_ids in sorted(grouped.items())
    )
    where = "this device's scheduled coverage" if for_device else "scheduled coverage"
    return (
        f"{detail} are excluded from {where} instead of matching every card; "
        "scheduled coverage is null, not full."
    )


def _run_has_card(run: Mapping[str, Any], npu_id: int) -> bool:
    ids, error = _card_filter(run)
    if error:
        return False
    return ids is None or npu_id in ids


def _field_finish_ns(row: Mapping[str, Any], field: str, sample_ts_ns: int, run_id: Any) -> int | None:
    finish_column = FIELD_FINISH_COLUMNS[field]
    if finish_column is None:
        value = _col(row, "row_finished_ts_ns") or sample_ts_ns
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    value = _col(row, finish_column)
    if value is None and run_id is None:
        return sample_ts_ns
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


_FIELD_BITS = {field: 1 << (idx + 1) for idx, field in enumerate(FIELD_VALUE_COLUMNS)}
_PACKED_IDX_LIMIT = 1 << 44


class _SchedSet:
    """Contiguous `array('Q')` (`8B/entry`) deduplicator for `(run_id, bit_idx)` masks."""

    def __init__(self) -> None:
        self._run_ord: dict[str, int] = {}
        self._entries = array("Q")
        self._overflow: dict[tuple[str, int], int] = {}
        self._prev_key = -1
        self._sorted = True

    def mark(self, run_id: str, bit_idx: int, mask_byte: int) -> None:
        r_ord = self._run_ord.get(run_id)
        if r_ord is None:
            r_ord = len(self._run_ord)
            self._run_ord[run_id] = r_ord
        if r_ord >= 4096 or bit_idx >= _PACKED_IDX_LIMIT:
            key = (run_id, bit_idx)
            self._overflow[key] = self._overflow.get(key, 0) | mask_byte
            return
        pkey = (r_ord << 44) | bit_idx
        if pkey == self._prev_key:
            self._entries[-1] |= mask_byte
            return
        if pkey < self._prev_key:
            self._sorted = False
        self._prev_key = pkey
        self._entries.append((pkey << 8) | mask_byte)

    def bit_counts(self) -> list[int]:
        out = [0] * 8
        entries = self._entries if self._sorted else sorted(self._entries)
        prev_key, merged = -1, 0
        for raw in entries:
            key = raw >> 8
            if key != prev_key:
                if merged:
                    for b in range(8):
                        if merged & (1 << b):
                            out[b] += 1
                prev_key, merged = key, raw & 0xFF
            else:
                merged |= raw & 0xFF
        if merged:
            for b in range(8):
                if merged & (1 << b):
                    out[b] += 1
        for mask in self._overflow.values():
            for b in range(8):
                if mask & (1 << b):
                    out[b] += 1
        self._entries = array("Q")
        self._overflow.clear()
        return out


class _DeviceAcc:
    """Bounded-memory streaming accumulator for one NPU card across a telemetry window."""

    def __init__(
        self, npu_id: int, runs: dict[str, dict[str, Any]], start_ns: int, end_ns: int,
    ) -> None:
        self.npu_id = npu_id
        self.run_ranges = {
            run_id: bounds for run_id, run in runs.items()
            if _run_has_card(run, npu_id)
            and (bounds := run_index_range(run, start_ns, end_ns)) is not None
        }
        self.schedule_available = bool(self.run_ranges)
        self.expected = (
            sum(max(0, high - low + 1) for low, high in self.run_ranges.values())
            if self.schedule_available else None
        )
        self.name: str | None = None
        self.sample_count = 0
        self.scheduled_row_count = 0
        self.sched = _SchedSet()
        self.field_obs_rows: dict[str, int] = {f: 0 for f in FIELD_VALUE_COLUMNS}
        self.field_late_rows: dict[str, int] = {f: 0 for f in FIELD_VALUE_COLUMNS}
        self.aicore = array("d")
        self.hbm_max: float | None = None
        self.temp_max: float | None = None
        self.power_max: float | None = None
        self.freq_min: float | None = None
        self.hbm_count = self.temp_count = self.power_count = self.freq_count = 0
        self.throttle_count = self.interf_count = 0
        self.throttle_any_one = self.throttle_any_truthy = self.interf_any_one = False
        self.getter_status: dict[str, dict[str, int]] = {}
        self.timestamp_sources: set[str] = set()
        self.clock_domains: set[str] = set()
        self.timestamps = array("q")
        self._prev_ts: int | None = None
        self._ts_sorted = True

    def add(
        self, row: Mapping[str, Any], sample_ts_ns: int, win_ts_ns: int,
        sched_offset: tuple[str, int] | None, end_ns: int,
    ) -> None:
        self.sample_count += 1
        if self.name is None:
            raw_name = _col(row, "name")
            if raw_name:
                self.name = str(raw_name)
        run_id = _col(row, "run_id")
        row_mask = 1 if sched_offset is not None else 0
        if sched_offset is not None:
            self.scheduled_row_count += 1
        for field, val_col in FIELD_VALUE_COLUMNS.items():
            val = _col(row, val_col)
            if val is None:
                continue
            fin_ns = _field_finish_ns(row, field, sample_ts_ns, run_id)
            if fin_ns is None:
                continue
            if fin_ns > end_ns:
                self.field_late_rows[field] += 1
                continue
            self.field_obs_rows[field] += 1
            if row_mask:
                row_mask |= _FIELD_BITS[field]
            if field == "aicore_percent":
                self.aicore.append(float(val))
            elif field == "hbm_used_mb":
                fval = float(val)
                self.hbm_count += 1
                self.hbm_max = fval if self.hbm_max is None or fval > self.hbm_max else self.hbm_max
            elif field == "temperature_c":
                fval = float(val)
                self.temp_count += 1
                self.temp_max = fval if self.temp_max is None or fval > self.temp_max else self.temp_max
            elif field == "power_w":
                fval = float(val)
                self.power_count += 1
                self.power_max = fval if self.power_max is None or fval > self.power_max else self.power_max
            elif field == "freq_mhz":
                fval = float(val)
                self.freq_count += 1
                self.freq_min = fval if self.freq_min is None or fval < self.freq_min else self.freq_min
            elif field == "throttling_flag":
                self.throttle_count += 1
                if val == 1:
                    self.throttle_any_one = True
                if val:
                    self.throttle_any_truthy = True
            elif field == "resource_interference_flag":
                self.interf_count += 1
                if val == 1:
                    self.interf_any_one = True
        if sched_offset is not None:
            self.sched.mark(sched_offset[0], sched_offset[1], row_mask)
        raw_gs = _col(row, "getter_status_json")
        if raw_gs:
            try:
                statuses = json.loads(raw_gs)
            except (TypeError, json.JSONDecodeError):
                statuses = None
            if isinstance(statuses, dict):
                for g_name, status in statuses.items():
                    if not isinstance(status, dict):
                        continue
                    item = self.getter_status.setdefault(g_name, {"observed": 0, "failed": 0, "skipped": 0})
                    if status.get("skipped"):
                        item["skipped"] += 1
                    elif status.get("ret") != 0 or status.get("error"):
                        item["failed"] += 1
                    else:
                        item["observed"] += 1
        ts_src = _col(row, "timestamp_source")
        if ts_src:
            self.timestamp_sources.add(str(ts_src))
        clk = _col(row, "clock_domain")
        if clk:
            self.clock_domains.add(str(clk))
        if self._prev_ts is None or win_ts_ns != self._prev_ts:
            if self._prev_ts is not None and win_ts_ns < self._prev_ts:
                self._ts_sorted = False
            self.timestamps.append(win_ts_ns)
            self._prev_ts = win_ts_ns

    def finalize(self, unusable_runs: list[tuple[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
        n_rows = self.sample_count
        unscheduled_row_count = n_rows - self.scheduled_row_count
        bit_counts = self.sched.bit_counts() if self.schedule_available else [0] * 8
        observed_scheduled = bit_counts[0] if self.schedule_available else None
        if unusable_runs:
            coverage = None
        elif self.schedule_available and self.expected:
            coverage = min(1.0, observed_scheduled / self.expected)  # type: ignore[operator]
        else:
            coverage = None
        missing = (
            max(0, self.expected - observed_scheduled)  # type: ignore[operator]
            if self.schedule_available and self.expected is not None else None
        )
        field_coverage: dict[str, dict[str, Any]] = {}
        for idx, field in enumerate(FIELD_LABELS, start=1):
            if self.schedule_available:
                obs_cnt = bit_counts[idx]
                field_expected = self.expected
                ratio = obs_cnt / self.expected if self.expected else None
                basis = "declared_sample_index_phase"
            else:
                obs_cnt = self.field_obs_rows[field]
                field_expected = None
                ratio = None
                basis = "schedule_unavailable"
            if unusable_runs:
                ratio = None
                if basis == "declared_sample_index_phase":
                    basis = "cards_json_unparseable"
            field_coverage[field] = {
                "observed": obs_cnt,
                "expected": field_expected,
                "coverage_ratio": _ratio(ratio) if ratio is not None else None,
                "basis": basis,
                "late_finish_excluded": self.field_late_rows[field],
            }
        if self.throttle_any_one:
            throttling: bool | None = True
        elif self.throttle_count and self.throttle_count == n_rows:
            throttling = False
        else:
            throttling = None
        if self.interf_any_one:
            interference: bool | None = True
        elif self.interf_count and self.interf_count == n_rows:
            interference = False
        else:
            interference = None

        ts_seq: Any = self.timestamps if self._ts_sorted else sorted(set(self.timestamps))
        max_gap_ns = max((ts_seq[i] - ts_seq[i - 1] for i in range(1, len(ts_seq))), default=0)
        first_ts = ts_seq[0] if ts_seq else None
        last_ts = ts_seq[-1] if ts_seq else None
        self.timestamps = array("q")
        ordered_aicore = sorted(self.aicore)
        aicore_p50 = _round(_percentile_sorted(ordered_aicore, 0.50))
        aicore_p95 = _round(_percentile_sorted(ordered_aicore, 0.95))
        has_aicore = bool(ordered_aicore)
        del ordered_aicore
        self.aicore = array("d")
        limitations: list[str] = []
        if unusable_runs:
            limitations.append(_unusable_cards_limitation(unusable_runs, for_device=True))
        if not n_rows:
            limitations.append("No raw sample was observed for this device in the requested window.")
        else:
            if not self.schedule_available:
                limitations.append(
                    "No declared sample_index/schedule phase is available for this device; "
                    "scheduled coverage is null, not full."
                )
            elif not self.expected:
                limitations.append(
                    "The window contains no declared scheduled sample points for this device."
                )
            elif coverage is not None and coverage < 0.999:
                limitations.append(
                    f"Scheduled coverage is {coverage:.1%} ({observed_scheduled}/{self.expected} sample points); "
                    f"{missing} declared point(s) are missing."
                )
            if self.schedule_available and unscheduled_row_count:
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
            failed = {k: v for k, v in self.getter_status.items() if v["failed"] or v["skipped"]}
            if failed:
                limitations.append(
                    "Getter failures/skips: " + ", ".join(
                        f"{name}=failed:{item['failed']}/skipped:{item['skipped']}"
                        for name, item in sorted(failed.items())
                    )
                )
            if not self.freq_count:
                limitations.append(
                    "No explicit frequency getter was observed by the window end; freq_mhz_min is null."
                )
            if not self.throttle_count:
                limitations.append(
                    "No explicit throttling flag/register was observed by the window end; "
                    "throttling_detected is null rather than false. Temperature alone is not "
                    "treated as evidence of no throttling."
                )
            elif not self.throttle_any_truthy and self.throttle_count < n_rows:
                limitations.append(
                    f"Explicit throttling flag was missing in {n_rows - self.throttle_count} of "
                    f"{n_rows} raw samples; absence of throttling cannot be concluded for them."
                )
        payload = {
            "npu_id": self.npu_id, "name": self.name, "sample_count": n_rows,
            "expected_sample_count": self.expected,
            "observed_scheduled_sample_count": observed_scheduled,
            "unscheduled_row_count": unscheduled_row_count, "missing_sample_count": missing,
            "coverage_ratio": _ratio(coverage) if coverage is not None else None,
            "field_coverage": field_coverage, "throttling_detected": throttling,
            "resource_interference_detected": interference,
            "aicore_p50": aicore_p50, "aicore_p95": aicore_p95,
            "hbm_peak_mb": _round(self.hbm_max), "temp_max_c": _round(self.temp_max),
            "power_max_w": _round(self.power_max), "freq_mhz_min": _round(self.freq_min),
            "getter_status": self.getter_status,
            "timestamp_sources": sorted(self.timestamp_sources),
            "clock_domains": sorted(self.clock_domains),
            "measurement_limitations": limitations,
        }
        facts = {
            "missing_aicore": not has_aicore, "missing_hbm": not self.hbm_count,
            "missing_temperature": not self.temp_count, "missing_power": not self.power_count,
            "missing_frequency": not self.freq_count, "missing_throttle": not self.throttle_count,
            "partial_throttle": bool(self.throttle_count) and self.throttle_count < n_rows,
            "partial_register": any(
                0 < field_coverage[field]["observed"] < n_rows for field in FIELD_VALUE_COLUMNS
            ),
            "coverage": coverage, "schedule_available": self.schedule_available,
            "expected": self.expected, "observed_scheduled": observed_scheduled,
            "missing": missing, "max_gap_ns": max_gap_ns,
            "first_sample": first_ts, "last_sample": last_ts, "has_samples": bool(n_rows),
        }
        return payload, facts


def _global_limitations(
    device_payloads: list[dict[str, Any]], facts: dict[int, dict[str, Any]],
    raw_sample_count: int, rollup_sample_count: int, schedule_available: bool,
    expected_total: int | None, observed_total: int | None, phase_drift_ns_max: int | None,
    timestamp_sources: list[str], clock_domains: list[str],
    unusable_runs: list[tuple[str, str]] | None = None,
) -> list[str]:
    limitations: list[str] = []
    if unusable_runs:
        limitations.append(_unusable_cards_limitation(unusable_runs, for_device=False))
    if schedule_available:
        limitations.append("Coverage uses declared sample_index/phase and interval from ingested run metadata; full scheduled coverage is sampling completeness, not continuous health.")
    else:
        limitations.append("No declared sample_index/schedule phase is available; scheduled coverage is null, not full, and must not be interpreted as continuous health.")
    if schedule_available and expected_total == 0:
        limitations.append("The window contains no declared scheduled sample points.")
    if schedule_available and expected_total and observed_total is not None and expected_total > observed_total:
        limitations.append(f"{expected_total - observed_total} declared scheduled sample point(s) are missing from the raw window.")
    if phase_drift_ns_max is not None:
        limitations.append(f"Scheduled wall phase is derived as wall_before_ns - schedule_slip_ns; maximum observed phase drift is {phase_drift_ns_max / 1_000_000:g} ms. Controller clock offset/skew was not measured independently.")
    if "legacy_seconds" in timestamp_sources or not timestamp_sources:
        limitations.append("Some samples have no subsecond timestamp source; legacy second-resolution timestamps are interpreted as Unix wall-clock seconds.")
    if clock_domains:
        limitations.append("Timestamp clock domain(s): " + ", ".join(clock_domains) + "; window bounds use the same host wall-clock domain.")
    else:
        limitations.append("Timestamp clock domain was not recorded; window matching assumes Unix wall-clock seconds.")
    if any(item["resource_interference_detected"] is None for item in device_payloads):
        limitations.append("Resource interference was not measured explicitly for every raw sample; resource_interference_detected is null, not false. Idle or utilization observations are not treated as interference.")
    if raw_sample_count == 0:
        limitations.append("No raw device samples exist for the requested host, devices, and window; p50/p95, exact peaks, frequency, and throttling cannot be reported.")
        if rollup_sample_count:
            limitations.append(f"The window contains {rollup_sample_count} 15-minute rollup sample(s), but rollup aggregates cannot reconstruct a raw distribution or exact maxima; they are not used for these metrics.")
        return limitations
    missing_devices = [str(item["npu_id"]) for item in device_payloads if not facts[item["npu_id"]]["has_samples"]]
    if missing_devices:
        limitations.append("No raw samples were observed for requested device(s) " + ", ".join(missing_devices) + ".")
    missing_frequency = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["missing_frequency"]]
    if missing_frequency:
        limitations.append("No explicit frequency getter was observed by the window end for device(s) " + ", ".join(missing_frequency) + "; top-level freq_mhz_min remains null.")
    missing_throttle = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["missing_throttle"]]
    if missing_throttle:
        limitations.append("No explicit throttling flag/register was observed by the window end for device(s) " + ", ".join(missing_throttle) + "; throttling_detected is null rather than false.")
    partial_throttle = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["partial_throttle"]]
    if partial_throttle:
        limitations.append("Explicit throttling flag was missing in some raw samples for device(s) " + ", ".join(partial_throttle) + "; absence of throttling cannot be concluded for them.")
    partial_register = [str(item["npu_id"]) for item in device_payloads if facts[item["npu_id"]]["partial_register"]]
    if partial_register:
        limitations.append("One or more getter fields were only partially observed for device(s) " + ", ".join(partial_register) + "; per-device field_coverage lists each gap.")
    low_coverage = [
        f"{item['npu_id']} ({facts[item['npu_id']]['coverage']:.1%})" for item in device_payloads
        if facts[item["npu_id"]]["schedule_available"] and facts[item["npu_id"]]["expected"]
        and facts[item["npu_id"]]["coverage"] is not None and facts[item["npu_id"]]["coverage"] < 0.999
    ]
    if low_coverage:
        limitations.append("Scheduled coverage is below 100% for device(s) " + ", ".join(low_coverage) + "; missing sample points reduce distribution and peak confidence.")
    return limitations


def _aggregate_tri_bool(flags: list[bool | None]) -> bool | None:
    if any(f is True for f in flags):
        return True
    if flags and all(f is False for f in flags):
        return False
    return None


def build_window(
    server: dict[str, Any], samples: Iterable[Mapping[str, Any]], *, devices: list[int],
    start_ts: int | float | str, end_ts: int | float | str,
    start_ts_ns: int | None = None, end_ts_ns: int | None = None,
    runs: list[dict[str, Any]] | None = None, rollup_sample_count: int = 0, now: int | None = None,
) -> dict[str, Any]:
    """Project declared-schedule raw samples into the C2 telemetry-window payload in one pass."""
    start_ns = int(start_ts_ns) if start_ts_ns is not None else seconds_to_ns(start_ts)
    end_ns = int(end_ts_ns) if end_ts_ns is not None else seconds_to_ns(end_ts)
    if end_ns <= start_ns:
        raise ValueError("telemetry window must have positive duration")
    runs_list = [dict(run) for run in runs or []]
    runs_by_id = {str(run["run_id"]): run for run in runs_list if run.get("run_id")}
    all_run_bounds = {r_id: b for r_id, r in runs_by_id.items() if (b := run_index_range(r, start_ns, end_ns)) is not None}
    unusable_runs = _unusable_card_runs(runs_by_id, start_ns, end_ns)
    unusable_run_ids = {run_id for run_id, _error in unusable_runs}
    requested = list(dict.fromkeys(int(device) for device in devices))
    run_ranges = {
        r_id: b for r_id, b in all_run_bounds.items()
        if r_id not in unusable_run_ids and any(_run_has_card(runs_by_id[r_id], npu_id) for npu_id in requested)
    }
    schedule_available = bool(run_ranges)
    expected_total = sum(max(0, h - l + 1) for l, h in run_ranges.values()) if schedule_available else None
    accs = {npu_id: _DeviceAcc(npu_id, runs_by_id, start_ns, end_ns) for npu_id in requested}

    raw_sample_count = 0
    global_sched = _SchedSet()
    unique_ts = array("q")
    prev_win_ts: int | None = None
    win_ts_sorted = True
    max_slip_ns: int | None = None
    phase_drift_ns_max: int | None = None
    timestamp_sources_set: set[str] = set()
    clock_domains_set: set[str] = {
        str(run["clock_domain"]) for run in runs_by_id.values() if run.get("clock_domain")
    }

    for row in samples:
        raw_ts = _col(row, "sample_ts_ns")
        if raw_ts is None:
            collected = _col(row, "collected_at")
            if collected is None:
                raise ValueError("raw sample lacks sample_ts_ns and collected_at")
            sample_ts_ns = seconds_to_ns(collected)
        else:
            sample_ts_ns = int(raw_ts)
        raw_sample_count += 1
        raw_npu = _col(row, "npu_id")
        npu_id = int(raw_npu) if not isinstance(raw_npu, bool) and isinstance(raw_npu, int) else int(raw_npu)
        run_id = _col(row, "run_id")
        index = _col(row, "sample_index")
        sched_ts = _col(row, "scheduled_ts_ns")
        win_ts = int(sched_ts if sched_ts is not None else sample_ts_ns)

        sched_offset: tuple[str, int] | None = None
        if isinstance(run_id, str) and not isinstance(index, bool) and isinstance(index, int):
            run = runs_by_id.get(run_id)
            bounds = all_run_bounds.get(run_id)
            if run is not None and bounds is not None and _run_has_card(run, npu_id):
                if bounds[0] <= index <= bounds[1]:
                    sched_offset = (run_id, index - bounds[0])
                    global_sched.mark(run_id, index - bounds[0], 1)
            if run is not None and sched_ts is not None:
                interval_ns = run.get("interval_ns")
                anchor_ns = run.get("schedule_anchor_ns")
                if isinstance(interval_ns, int) and isinstance(anchor_ns, int) and interval_ns > 0:
                    drift = abs(int(sched_ts) - (anchor_ns + index * interval_ns))
                    phase_drift_ns_max = (
                        drift if phase_drift_ns_max is None or drift > phase_drift_ns_max else phase_drift_ns_max
                    )

        slip = _col(row, "schedule_slip_ns")
        if slip is not None:
            islip = int(slip)
            max_slip_ns = islip if max_slip_ns is None or islip > max_slip_ns else max_slip_ns
        ts_src = _col(row, "timestamp_source")
        if ts_src:
            timestamp_sources_set.add(str(ts_src))
        clk = _col(row, "clock_domain")
        if clk:
            clock_domains_set.add(str(clk))
        if prev_win_ts is None or win_ts != prev_win_ts:
            if prev_win_ts is not None and win_ts < prev_win_ts:
                win_ts_sorted = False
            unique_ts.append(win_ts)
            prev_win_ts = win_ts

        acc = accs.get(npu_id)
        if acc is not None:
            acc.add(row, sample_ts_ns, win_ts, sched_offset, end_ns)

    ordered_unique_ts: Any = unique_ts if win_ts_sorted else sorted(set(unique_ts))
    first_sample_ns = ordered_unique_ts[0] if ordered_unique_ts else None
    last_sample_ns = ordered_unique_ts[-1] if ordered_unique_ts else None
    observed_total = (
        global_sched.bit_counts()[0] if schedule_available else len(ordered_unique_ts)
    )
    if len(ordered_unique_ts) >= 2:
        gaps_seconds = sorted(
            (ordered_unique_ts[i] - ordered_unique_ts[i - 1]) / NS_PER_SECOND
            for i in range(1, len(ordered_unique_ts))
        )
        observed_interval_seconds: dict[str, float | None] | None = {
            "min": _round(gaps_seconds[0]),
            "median": _round(gaps_seconds[len(gaps_seconds) // 2]),
            "max": _round(gaps_seconds[-1]),
        }
    else:
        observed_interval_seconds = None

    device_payloads: list[dict[str, Any]] = []
    facts: dict[int, dict[str, Any]] = {}
    for npu_id in requested:
        payload, device_facts = accs[npu_id].finalize(unusable_runs)
        device_payloads.append(payload)
        facts[npu_id] = device_facts

    coverages = [item["coverage_ratio"] for item in device_payloads]
    coverage = min(coverages) if coverages and all(v is not None for v in coverages) else None
    throttling = _aggregate_tri_bool([item["throttling_detected"] for item in device_payloads])
    interference = _aggregate_tri_bool([item["resource_interference_detected"] for item in device_payloads])
    frequency_values = [item["freq_mhz_min"] for item in device_payloads]
    power_values = [item["power_max_w"] for item in device_payloads]
    temperature_values = [item["temp_max_c"] for item in device_payloads]
    fleet_frequency = min(frequency_values) if frequency_values and all(v is not None for v in frequency_values) else None
    fleet_power = max(power_values) if power_values and all(v is not None for v in power_values) else None
    fleet_temperature = max(temperature_values) if temperature_values and all(v is not None for v in temperature_values) else None

    intervals = sorted({
        int(run["interval_ns"]) for run in runs_by_id.values()
        if isinstance(run.get("interval_ns"), int) and not isinstance(run.get("interval_ns"), bool)
        and run["interval_ns"] > 0
    })
    timestamp_sources = sorted(timestamp_sources_set)
    clock_domains = sorted(clock_domains_set)
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
        "source": "sqlite", "observation": observation,
        "server": {
            "id": server["id"], "name": server["name"], "host": server["host"],
            "port": server.get("port"), "enabled": bool(server.get("enabled")),
            "tags": server.get("tags") or [],
        },
        "window": {
            "start_ts": start_ns / NS_PER_SECOND, "end_ts": end_ns / NS_PER_SECOND,
            "start_ts_ns": start_ns, "end_ts_ns": end_ns,
            "duration_seconds": (end_ns - start_ns) / NS_PER_SECOND,
            "sample_interval_seconds": _round(intervals[0] / NS_PER_SECOND) if len(intervals) == 1 else None,
            "sample_interval_ns": intervals[0] if len(intervals) == 1 else None,
            "interval_source": "run_metadata" if intervals else "unavailable",
            "coverage_basis": (
                "cards_json_unparseable" if unusable_runs
                else "declared_sample_index_phase" if schedule_available
                else "schedule_unavailable"
            ),
            "timestamp_sources": timestamp_sources or ["unknown"],
            "clock_domains": clock_domains or ["unknown"],
            "sampling_basis": "raw_device_samples",
            "coverage_definition": (
                "observed declared sample_index keys divided by scheduled keys derived from "
                "run interval and phase; no elapsed-bucket or tolerance estimate"
            ),
            "first_sample_ts_ns": first_sample_ns, "last_sample_ts_ns": last_sample_ns,
            "observed_interval_seconds": observed_interval_seconds,
            "schedule_slip_ms_max": _round(max_slip_ns / 1_000_000) if max_slip_ns is not None else None,
            "schedule_phase_drift_ns_max": phase_drift_ns_max,
            "scheduled_sample_count": expected_total, "observed_sample_count": observed_total,
            "missing_sample_count": (
                max(0, expected_total - observed_total)
                if expected_total is not None and observed_total is not None else None
            ),
            "schedule_runs": [
                {
                    "run_id": r_id, "interval_ns": run.get("interval_ns"),
                    "schedule_anchor_ns": run.get("schedule_anchor_ns"),
                    "first_sample_index": run.get("first_sample_index"),
                    "last_sample_index": run.get("last_sample_index"),
                    "clock_domain": run.get("clock_domain"), "stop_reason": run.get("stop_reason"),
                }
                for r_id, run in sorted(runs_by_id.items())
            ],
            "requested_devices": requested,
            "raw_sample_count": raw_sample_count, "rollup_sample_count": int(rollup_sample_count),
        },
        "coverage_ratio": _ratio(coverage) if coverage is not None else None,
        "throttling_detected": throttling, "resource_interference_detected": interference,
        "freq_mhz_min": _round(fleet_frequency),
        "power_max_w": _round(fleet_power), "temp_max_c": _round(fleet_temperature),
        "measurement_limitations": _global_limitations(
            device_payloads, facts, raw_sample_count, int(rollup_sample_count),
            schedule_available, expected_total, observed_total, phase_drift_ns_max,
            timestamp_sources, clock_domains, unusable_runs,
        ),
        "devices": device_payloads,
    }
