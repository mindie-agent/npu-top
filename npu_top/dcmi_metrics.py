"""Read-only, initialize-once DCMI metrics sampler for Ascend A2.

[Role] Samples power, temperature, current/rated AICore frequency, utilization,
HBM info, and AICORE_FREQREDUC_CAUSE through the installed `libdcmi.so`.
[Invariants] One header parse, one CDLL load, one `dcmi_init`; every getter
records API name, return code, raw value, unit, and timing. `ret != 0` leaves
the value `null` (never 0/false). Bit 0 of the cause mask is IDLE only, not a
thermal throttle; undocumented cause bits stay in `unknown_bits`. A missing or
unparseable header aborts before any getter instead of guessing enums; a
nonzero `dcmi_init` aborts before any getter. The cause decoder is imported from
`dcmi_telemetry` (or `--cause-module`); no copied fallback exists here.
[Timeouts & Locks] One foreground process bounded by `--duration` and an
optional `--stop-file`; no daemon. Summary statistics retain only a 10,000-entry
timing tail, so memory stays O(1) across long runs. The SSH caller must use an
outer timeout of at least `duration + 5 s` cleanup.
[Side-Effect Policy] Read-only getters only (`dcmi_init`, frequency, power,
temperature, utilization, HBM info, device info); no setter, no compute.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_HEADER_PATH = "/usr/local/dcmi/dcmi_interface_api.h"
DEFAULT_LIBRARY_PATH = "/usr/local/dcmi/libdcmi.so"
POWER_UNIT = "W"
POWER_SCALE = 0.1          # A2 doc dcmi_023: power unit is 0.1 W
FREQUENCY_UNIT = "MHz"     # A2 doc dcmi_049: frequency unit is MHz
TEMPERATURE_UNIT = "C"     # A2 doc dcmi_043: temperature unit is Celsius
UTILIZATION_UNIT = "%"     # A2 doc dcmi_055: utilization unit is %
HBM_STRUCT_SIZE = 32
CALLER_CLEANUP_SECONDS = 5


class StartupError(RuntimeError):
    def __init__(self, stage: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.detail = detail


class HbmInfo(ctypes.Structure):
    _fields_ = [
        ("memory_size", ctypes.c_ulonglong),
        ("freq", ctypes.c_uint),
        ("memory_usage", ctypes.c_ulonglong),
        ("temp", ctypes.c_int),
        ("bandwith_util_rate", ctypes.c_uint),
    ]


def _iso_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1e9, timezone.utc).isoformat(timespec="microseconds")


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        return Path(path).read_bytes()
    except TimeoutError:
        raise
    except OSError:
        return None


def _sha256(raw: Optional[bytes]) -> Optional[str]:
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def load_cause_module(path: Optional[str]) -> Tuple[Any, Optional[str], Optional[str]]:
    """Import the existing cause decoder; never duplicate its fallback logic."""
    if path:
        spec = importlib.util.spec_from_file_location("npu_top_dcmi_cause", path)
        if spec is None or spec.loader is None:
            raise StartupError("cause_module", f"cannot load cause module: {path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise StartupError("cause_module", f"cannot execute cause module: {exc}") from exc
        module_path = path
    else:
        try:
            import dcmi_telemetry as module  # type: ignore[import-not-found]
        except ImportError as exc:
            raise StartupError(
                "cause_module",
                "dcmi_telemetry is not importable; pass --cause-module /path/to/dcmi_telemetry.py",
                error=str(exc),
            ) from exc
        module_path = getattr(module, "__file__", None)
    for name in ("resolve_enum_values", "parse_enum_values", "decode_cause_mask"):
        if not hasattr(module, name):
            raise StartupError("cause_module", f"cause module lacks {name}")
    try:
        guessed_main, guessed_sub, _, _ = module.resolve_enum_values(None)
    except Exception as exc:
        raise StartupError("cause_module", f"resolve_enum_values(None) failed: {exc}") from exc
    if guessed_main is not None or guessed_sub is not None:
        raise StartupError(
            "cause_module",
            "cause module returns enums without an installed header; refusing guessed constants",
        )
    return module, module_path, _sha256(_read_bytes(module_path or ""))


def parse_utilization_macros(header_text: str) -> Dict[str, int]:
    import re
    values: Dict[str, int] = {}
    for name, raw in re.findall(
        r"^\s*#define\s+(DCMI_UTILIZATION_RATE_\w+)\s+(\d+)\s*$", header_text, re.M,
    ):
        values[name] = int(raw)
    return values


def resolve_abi(header_text: str, cause_module: Any) -> Dict[str, Any]:
    """Resolve every enum/macro needed by the getters from the installed header."""
    try:
        main_cmd, lp_sub, enum_source, enum_error = cause_module.resolve_enum_values(header_text)
    except AttributeError as exc:
        raise StartupError("cause_module", f"cause module lacks required helper: {exc}") from exc
    if main_cmd is None or lp_sub is None:
        raise StartupError("header", enum_error or "DCMI LP enums unavailable", enum_source=enum_source)
    try:
        freq = cause_module.parse_enum_values(header_text, "dcmi_freq_type")
    except (AttributeError, KeyError, ValueError) as exc:
        raise StartupError("header", f"dcmi_freq_type unavailable: {exc}") from exc
    util = parse_utilization_macros(header_text)
    missing = [
        name for name, values in (
            ("DCMI_FREQ_AICORE_CURRENT_", freq),
            ("DCMI_FREQ_AICORE_MAX", freq),
        ) if name not in values
    ]
    if missing:
        raise StartupError("header", "frequency enum missing from installed header", missing=missing)
    return {
        "main_cmd_lp": int(main_cmd),
        "lp_sub_cmd_aicore_freqreduc_cause": int(lp_sub),
        "freq_type_aicore_current": int(freq["DCMI_FREQ_AICORE_CURRENT_"]),
        "freq_type_aicore_max": int(freq["DCMI_FREQ_AICORE_MAX"]),
        "utilization_types": util,
        "utilization_available": all(
            name in util for name in (
                "DCMI_UTILIZATION_RATE_AICORE",
                "DCMI_UTILIZATION_RATE_HBM",
                "DCMI_UTILIZATION_RATE_NPU",
            )
        ),
        "enum_source": enum_source,
    }


def configure_library(lib_path: str) -> Tuple[Any, int]:
    """Load and bind once, then call `dcmi_init` once."""
    try:
        lib = ctypes.CDLL(lib_path)
    except TimeoutError:
        raise
    except OSError as exc:
        raise StartupError("library", f"cannot load {lib_path}: {exc}") from exc
    try:
        lib.dcmi_init.argtypes = []
        lib.dcmi_init.restype = ctypes.c_int
        lib.dcmi_get_device_frequency.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_frequency.restype = ctypes.c_int
        lib.dcmi_get_device_power_info.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
        ]
        lib.dcmi_get_device_power_info.restype = ctypes.c_int
        lib.dcmi_get_device_temperature.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
        ]
        lib.dcmi_get_device_temperature.restype = ctypes.c_int
        lib.dcmi_get_device_utilization_rate.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_utilization_rate.restype = ctypes.c_int
        lib.dcmi_get_device_hbm_info.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(HbmInfo),
        ]
        lib.dcmi_get_device_hbm_info.restype = ctypes.c_int
        lib.dcmi_get_device_info.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_info.restype = ctypes.c_int
        init_ret = int(lib.dcmi_init())
    except AttributeError as exc:
        raise StartupError("library", f"required DCMI symbol missing: {exc}") from exc
    if init_ret != 0:
        raise StartupError("init", f"dcmi_init returned {init_ret}", ret=init_ret)
    return lib, init_ret


def _timed_getter(
    api: str, unit: str, call: Callable[[], Tuple[int, Any]],
    translate: Optional[Callable[[Any], Any]] = None, **metadata: Any,
) -> Dict[str, Any]:
    started_wall_ns = time.time_ns()
    started_perf_ns = time.perf_counter_ns()
    started_at = _iso_from_ns(started_wall_ns)
    ret: Optional[int] = None
    raw: Any = None
    error: Optional[str] = None
    try:
        ret, raw = call()
        if ret != 0:
            error = f"DCMI ret {ret}"
            raw = None
    except Exception as exc:  # ctypes call failure must stay observable
        error = f"{type(exc).__name__}: {exc}"
    finished_wall_ns = time.time_ns()
    finished_perf_ns = time.perf_counter_ns()
    value = None
    if ret == 0 and raw is not None:
        value = translate(raw) if translate is not None else raw
    return {
        "api": api, "unit": unit, "ret": ret, "raw": raw, "value": value,
        "started_at": started_at, "finished_at": _iso_from_ns(finished_wall_ns),
        "started_at_ns": started_wall_ns, "finished_at_ns": finished_wall_ns,
        "duration_ns": finished_perf_ns - started_perf_ns, "error": error,
        **metadata,
    }


def _power_value(raw: int) -> float:
    return raw * POWER_SCALE


def _hbm_fields(raw: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    return {
        "memory_size": {"raw": raw["memory_size"], "unit": "MB", "value": raw["memory_size"]},
        "freq": {"raw": raw["freq"], "unit": "MHz", "value": raw["freq"]},
        # The installed header/doc does not state units for these three fields.
        "memory_usage": {"raw": raw["memory_usage"], "unit": None, "value": None},
        "temp": {"raw": raw["temp"], "unit": None, "value": None},
        "bandwith_util_rate": {"raw": raw["bandwith_util_rate"], "unit": None, "value": None},
    }


def sample_card(
    lib: Any, cause_module: Any, card: int, device: int, abi: Dict[str, Any],
    include_slow_utilization: bool = False,
) -> Dict[str, Any]:
    getters: Dict[str, Dict[str, Any]] = {}

    def freq_call(freq_type: int) -> Callable[[], Tuple[int, int]]:
        def call() -> Tuple[int, int]:
            out = ctypes.c_uint(0)
            ret = int(lib.dcmi_get_device_frequency(card, device, freq_type, ctypes.byref(out)))
            return ret, int(out.value)
        return call

    getters["current_freq"] = _timed_getter(
        "dcmi_get_device_frequency", FREQUENCY_UNIT,
        freq_call(abi["freq_type_aicore_current"]), freq_type=abi["freq_type_aicore_current"],
        meaning="current AICore frequency",
    )
    getters["rated_freq"] = _timed_getter(
        "dcmi_get_device_frequency", FREQUENCY_UNIT,
        freq_call(abi["freq_type_aicore_max"]), freq_type=abi["freq_type_aicore_max"],
        meaning="rated AICore frequency; never substituted for current",
    )

    def power_call() -> Tuple[int, int]:
        out = ctypes.c_int(0)
        ret = int(lib.dcmi_get_device_power_info(card, device, ctypes.byref(out)))
        return ret, int(out.value)

    getters["power"] = _timed_getter(
        "dcmi_get_device_power_info", POWER_UNIT, power_call,
        translate=_power_value, raw_unit="0.1W", scale=POWER_SCALE,
    )

    def temperature_call() -> Tuple[int, int]:
        out = ctypes.c_int(0)
        ret = int(lib.dcmi_get_device_temperature(card, device, ctypes.byref(out)))
        return ret, int(out.value)

    getters["temperature"] = _timed_getter(
        "dcmi_get_device_temperature", TEMPERATURE_UNIT, temperature_call,
    )

    for key, macro, meaning in (
        ("aicore_utilization", "DCMI_UTILIZATION_RATE_AICORE", "AI Core utilization"),
        ("hbm_utilization", "DCMI_UTILIZATION_RATE_HBM", "HBM utilization"),
        ("npu_utilization", "DCMI_UTILIZATION_RATE_NPU", "NPU overall utilization"),
    ):
        if key in ("aicore_utilization", "npu_utilization") and not include_slow_utilization:
            getters[key] = {
                "api": "dcmi_get_device_utilization_rate", "unit": UTILIZATION_UNIT,
                "ret": None, "raw": None, "value": None,
                "started_at": None, "finished_at": None, "duration_ns": None,
                "error": "not sampled: slow getter disabled by default; pass --include-slow-utilization",
                "skipped": True, "meaning": meaning,
            }
            continue
        if macro not in abi["utilization_types"]:
            getters[key] = {
                "api": "dcmi_get_device_utilization_rate", "unit": UTILIZATION_UNIT,
                "ret": None, "raw": None, "value": None, "error": f"{macro} missing from header",
                "started_at": None, "finished_at": None, "meaning": meaning,
            }
            continue
        util_type = abi["utilization_types"][macro]

        def util_call(input_type: int = util_type) -> Tuple[int, int]:
            out = ctypes.c_uint(0)
            ret = int(lib.dcmi_get_device_utilization_rate(
                card, device, input_type, ctypes.byref(out),
            ))
            return ret, int(out.value)

        getters[key] = _timed_getter(
            "dcmi_get_device_utilization_rate", UTILIZATION_UNIT, util_call,
            input_type=util_type, meaning=meaning,
        )

    def hbm_call() -> Tuple[int, Dict[str, int]]:
        info = HbmInfo()
        ret = int(lib.dcmi_get_device_hbm_info(card, device, ctypes.byref(info)))
        return ret, {
            "memory_size": int(info.memory_size), "freq": int(info.freq),
            "memory_usage": int(info.memory_usage), "temp": int(info.temp),
            "bandwith_util_rate": int(info.bandwith_util_rate),
        }

    hbm = _timed_getter(
        "dcmi_get_device_hbm_info", "struct", hbm_call,
        translate=_hbm_fields, struct_size=HBM_STRUCT_SIZE,
    )
    getters["hbm_info"] = hbm

    def cause_call() -> Tuple[int, Dict[str, int]]:
        buffer = ctypes.c_uint64(0)
        size = ctypes.c_uint(8)
        ret = int(lib.dcmi_get_device_info(
            card, device, abi["main_cmd_lp"], abi["lp_sub_cmd_aicore_freqreduc_cause"],
            ctypes.byref(buffer), ctypes.byref(size),
        ))
        return ret, {"mask": int(buffer.value), "size": int(size.value)}

    cause = _timed_getter(
        "dcmi_get_device_info", "bitmask_u64", cause_call,
        main_cmd_lp=abi["main_cmd_lp"],
        lp_sub_cmd_aicore_freqreduc_cause=abi["lp_sub_cmd_aicore_freqreduc_cause"],
    )
    if cause["ret"] == 0:
        cause["decoded"] = cause_module.decode_cause_mask(
            cause["raw"]["mask"], ret=0, size_bytes=cause["raw"]["size"],
        )
        cause["value"] = cause["decoded"]
    else:
        cause["decoded"] = cause_module.decode_cause_mask(0, ret=cause["ret"] or -1, size_bytes=0)
    getters["freq_reduc_cause"] = cause

    errors = {
        name: getter["error"] for name, getter in getters.items()
        if getter.get("error") and not getter.get("skipped")
    }
    skipped = [name for name, getter in getters.items() if getter.get("skipped")]
    return {
        "card_id": card, "device_id": device, "getters": getters,
        "errors": errors, "skipped": skipped,
    }


def _interval_summary(
    starts: Iterable[int], sample_count: Optional[int] = None,
) -> Dict[str, Optional[float]]:
    retained = list(starts)
    if len(retained) < 2:
        return {"count": 0, "min_ms": None, "median_ms": None, "p95_ms": None, "max_ms": None}
    intervals = [(right - left) / 1e6 for left, right in zip(retained, retained[1:])]
    ordered = sorted(intervals)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    count = len(intervals) if sample_count is None else max(0, sample_count - 1)
    return {
        "count": count, "min_ms": round(min(intervals), 3),
        "median_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(p95, 3), "max_ms": round(max(intervals), 3),
    }


def _getter_summary(
    durations: Dict[str, Iterable[int]], counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Dict[str, Optional[float]]]:
    summary: Dict[str, Dict[str, Optional[float]]] = {}
    for name, values in sorted(durations.items()):
        ordered = sorted(values)
        if not ordered:
            continue
        summary[name] = {
            "count": len(ordered) if counts is None else counts.get(name, len(ordered)),
            "min_us": round(min(ordered) / 1000, 3),
            "median_us": round(ordered[len(ordered) // 2] / 1000, 3),
            "max_us": round(max(ordered) / 1000, 3),
        }
    return summary


def _parse_cards(raw: str) -> List[int]:
    cards: List[int] = []
    for part in raw.split(","):
        if "-" in part:
            first, last = part.split("-", 1)
            cards.extend(range(int(first), int(last) + 1))
        elif part.strip():
            cards.append(int(part))
    return sorted(set(cards))


def run_sampler(args: argparse.Namespace) -> int:
    header_raw = _read_bytes(args.header)
    if header_raw is None:
        raise StartupError("header", f"cannot read installed header: {args.header}")
    header_text = header_raw.decode("utf-8", errors="replace")
    cause_module, cause_path, cause_sha = load_cause_module(args.cause_module)
    abi = resolve_abi(header_text, cause_module)
    lib_raw = _read_bytes(args.library)
    lib, init_ret = configure_library(args.library)
    run_id = args.run_id
    cards = _parse_cards(args.cards)
    if not cards:
        raise StartupError("args", "at least one card id is required")
    if args.duration <= 0 or args.interval <= 0:
        raise StartupError("args", "--duration and --interval must be positive")

    started_wall_ns = time.time_ns()
    meta = {
        "type": "meta", "run_id": run_id, "cards": cards, "device_id": args.device,
        "duration_s": args.duration, "interval_s": args.interval,
        "stop_file": args.stop_file,
        "slow_utilization_enabled": bool(args.include_slow_utilization),
        "started_at": _iso_from_ns(started_wall_ns), "started_at_ns": started_wall_ns,
        "header": args.header, "header_sha256": _sha256(header_raw),
        "library": args.library, "library_sha256": _sha256(lib_raw),
        "cause_module": cause_path, "cause_module_sha256": cause_sha,
        "init_ret": init_ret,
        "enum": {
            "source": abi["enum_source"],
            "main_cmd_lp": abi["main_cmd_lp"],
            "lp_sub_cmd_aicore_freqreduc_cause": abi["lp_sub_cmd_aicore_freqreduc_cause"],
            "freq_type_aicore_current": abi["freq_type_aicore_current"],
            "freq_type_aicore_max": abi["freq_type_aicore_max"],
            "utilization_types": abi["utilization_types"],
        },
        "units": {
            "current_freq": FREQUENCY_UNIT, "rated_freq": FREQUENCY_UNIT,
            "power": POWER_UNIT, "temperature": TEMPERATURE_UNIT,
            "utilization": UTILIZATION_UNIT, "cause": "bitmask_u64",
            "hbm_memory_size": "MB", "hbm_freq": FREQUENCY_UNIT,
            "hbm_memory_usage": None, "hbm_temp": None, "hbm_bandwidth": None,
        },
        "caller_timeout_guidance_s": round(args.duration + CALLER_CLEANUP_SECONDS, 3),
        "dcmi_telemetry_sha256": cause_sha,
    }
    print(json.dumps(meta, sort_keys=True), flush=True)

    interval_ns = int(args.interval * 1e9)
    schedule_start_ns = time.monotonic_ns()
    deadline_ns = schedule_start_ns + int(args.duration * 1e9)
    # Keep only a bounded tail of timing inputs so long runs stay O(1) in memory.
    starts: deque[int] = deque(maxlen=10000)
    getter_durations: Dict[str, deque[int]] = {}
    getter_counts: Dict[str, int] = {}
    sample_count = 0
    stop_reason = "duration"
    index = 0
    while True:
        if args.stop_file and os.path.exists(args.stop_file):
            stop_reason = "stop_file"
            break
        now_ns = time.monotonic_ns()
        if now_ns >= deadline_ns:
            break
        target_ns = schedule_start_ns + index * interval_ns
        wait_started_ns = time.monotonic_ns()
        while time.monotonic_ns() < target_ns:
            if args.stop_file and os.path.exists(args.stop_file):
                stop_reason = "stop_file"
                break
            remaining_s = (target_ns - time.monotonic_ns()) / 1e9
            if remaining_s > 0:
                time.sleep(min(remaining_s, 0.05))
        if stop_reason == "stop_file":
            break
        sleep_ns = time.monotonic_ns() - wait_started_ns
        sample_start_wall_ns = time.time_ns()
        sample_start_perf_ns = time.perf_counter_ns()
        sample_start_mono_ns = time.monotonic_ns()
        card_records = {
            str(card): sample_card(
                lib, cause_module, card, args.device, abi,
                include_slow_utilization=args.include_slow_utilization,
            )
            for card in cards
        }
        sample_end_wall_ns = time.time_ns()
        sample_end_perf_ns = time.perf_counter_ns()
        sample_end_mono_ns = time.monotonic_ns()
        sample = {
            "type": "sample", "run_id": run_id, "sample_index": index,
            "wall_before_ns": sample_start_wall_ns,
            "perf_counter_ns": sample_start_perf_ns,
            "monotonic_ns": sample_start_mono_ns,
            "wall_after_ns": sample_end_wall_ns,
            "perf_counter_after_ns": sample_end_perf_ns,
            "monotonic_after_ns": sample_end_mono_ns,
            "started_at": _iso_from_ns(sample_start_wall_ns),
            "finished_at": _iso_from_ns(sample_end_wall_ns),
            "duration_ns": sample_end_wall_ns - sample_start_wall_ns,
            "sleep_ns": sleep_ns,
            "schedule_slip_ns": sample_start_mono_ns - int(target_ns),
            "cards": card_records,
        }
        print(json.dumps(sample, sort_keys=True), flush=True)
        starts.append(sample_start_wall_ns)
        for card in card_records.values():
            for name, getter in card["getters"].items():
                duration_ns = getter.get("duration_ns")
                if duration_ns is None:
                    continue
                getter_durations.setdefault(
                    name, deque(maxlen=10000),
                ).append(int(duration_ns))
                getter_counts[name] = getter_counts.get(name, 0) + 1
        sample_count += 1
        index += 1
    summary = {
        "type": "summary", "run_id": run_id, "stop_reason": stop_reason,
        "sample_count": sample_count,
        "interval_ms": _interval_summary(starts, sample_count),
        "getter_latency_us": _getter_summary(getter_durations, getter_counts),
        "finished_at": _iso_from_ns(time.time_ns()),
        "caller_timeout_guidance_s": round(args.duration + CALLER_CLEANUP_SECONDS, 3),
    }
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default="0", help="e.g. 0 or 0,1 or 0-3")
    parser.add_argument("--device", type=int, default=0, help="0=NPU die, 1=MCU")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--stop-file", default=None)
    parser.add_argument("--header", default=DEFAULT_HEADER_PATH)
    parser.add_argument("--library", default=DEFAULT_LIBRARY_PATH)
    parser.add_argument("--cause-module", default=None,
                        help="path to the uploaded dcmi_telemetry.py")
    parser.add_argument(
        "--include-slow-utilization", action="store_true",
        help="also sample AI Core / NPU utilization (each costs ~60 ms on A2)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run_sampler(args)
    except StartupError as exc:
        print(json.dumps({
            "type": "error", "run_id": args.run_id, "stage": exc.stage,
            "error": exc.message, **exc.detail,
        }, sort_keys=True), flush=True)
        return 2 if exc.stage in ("header", "library") else 3 if exc.stage == "init" else 4
    except Exception as exc:  # keep runtime failures structured for the caller
        print(json.dumps({
            "type": "error", "run_id": args.run_id, "stage": "runtime",
            "error_type": type(exc).__name__, "error": str(exc),
        }, sort_keys=True), flush=True)
        return 5


if __name__ == "__main__":
    sys.exit(main())
