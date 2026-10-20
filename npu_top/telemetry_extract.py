"""Raw probe and DCMI register extraction with explicit timestamp/status provenance.

[Role] Converts `npu-smi` probe devices and DCMI sampler cards into persisted raw
columns plus per-field finish timestamps, getter status, and explicit unknown values.
[Invariants] A getter value is used only for `ret == 0`, no error, and not skipped;
DCMI frequency-reduction flags require an observed decoded cause; HBM `memory_usage`
raw is promoted only when the stream declares `MB`; absent/failed data stays `None`.
[Timeouts & Locks] Pure in-memory conversion; zero I/O or locks.
[Side-Effect Policy] Side-effect free.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


NS_PER_SECOND = 1_000_000_000
FREQUENCY_KEYS = ("freq_mhz", "frequency_mhz", "aicore_freq_mhz", "aicore_frequency_mhz")
THROTTLE_KEYS = ("throttling_detected", "throttled", "throttling", "throttle")
THROTTLE_COMPONENT_KEYS = (
    "thermal_throttled", "power_throttled", "frequency_throttled", "hw_throttled",
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "throttled", "active"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "normal", "none", "inactive"})


def seconds_to_ns(value: Any) -> int:
    """Convert integer, float, decimal, or string Unix seconds to exact nanoseconds."""
    if isinstance(value, bool) or value is None:
        raise ValueError("timestamp must be a finite number of seconds")
    if isinstance(value, Decimal):
        decimal = value
    elif isinstance(value, int):
        decimal = Decimal(value)
    elif isinstance(value, float):
        decimal = Decimal(str(value))
    elif isinstance(value, str):
        try:
            decimal = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
    else:
        raise ValueError(f"invalid timestamp type: {type(value).__name__}")
    if not decimal.is_finite():
        raise ValueError("timestamp must be finite")
    return int((decimal * NS_PER_SECOND).to_integral_value(rounding=ROUND_HALF_UP))


def dcmi_timestamp_ns(sample: dict[str, Any]) -> tuple[int, str]:
    """Return the DCMI sample's Unix-wall-clock timestamp and explicit source."""
    for key, source in (
        ("started_at_ns", "dcmi.started_at_ns"),
        ("wall_before_ns", "dcmi.wall_before_ns"),
    ):
        value = sample.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value), source
    started = sample.get("started_at")
    if isinstance(started, str) and started.strip():
        try:
            parsed = datetime.fromisoformat(started.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid DCMI started_at: {started!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        ns = (delta.days * 86400 + delta.seconds) * NS_PER_SECOND + delta.microseconds * 1000
        return ns, "dcmi.started_at_iso"
    raise ValueError("DCMI sample lacks started_at_ns, wall_before_ns, or started_at")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _flag(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        number = _finite(value)
        return None if number is None else int(number != 0)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE_VALUES:
            return 1
        if text in _FALSE_VALUES:
            return 0
    return None


def _first_number(device: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        number = _finite(device.get(key))
        if number is not None:
            return number
    return None


def _throttle_flag(device: dict[str, Any]) -> int | None:
    """Return an explicit OR of observed throttle registers, else `None`."""
    dcmi = device.get("dcmi_freq_reduc")
    if isinstance(dcmi, dict):
        if dcmi.get("state") != "observed":
            return None
        causes = [dcmi.get(key) for key in ("thermal_limit", "power_limit", "svfd")]
        if any(value is True for value in causes):
            return 1
        if all(value is False for value in causes) and not dcmi.get("unknown_bits"):
            return 0
        return None
    values: list[int] = []
    for key in THROTTLE_KEYS + THROTTLE_COMPONENT_KEYS:
        if key not in device:
            continue
        value = _flag(device.get(key))
        if value is not None:
            values.append(value)
    return int(any(values)) if values else None


def extract_raw_metrics(device: dict[str, Any]) -> dict[str, Any]:
    """Return the raw telemetry columns available on one probe device."""
    hbm = device.get("hbm") or {}
    hbm_used = hbm.get("used_mb")
    if hbm_used is None:
        hbm_used = device.get("hbm_used_mb")
    temperature = device.get("temperature_c")
    if temperature is None:
        temperature = device.get("temp_c")
    power = device.get("power_w")
    if power is None:
        power = device.get("power_watt")
    name = device.get("name")
    return {
        "name": str(name)[:120] if name else None,
        "aicore_percent": _finite(device.get("aicore_percent")),
        "hbm_used_mb": _finite(hbm_used),
        "temperature_c": _finite(temperature),
        "power_w": _finite(power),
        "freq_mhz": _first_number(device, FREQUENCY_KEYS),
        "throttling_flag": _throttle_flag(device),
        "resource_interference_flag": _flag(device.get("resource_interference_detected")),
    }


def _getter_observed(getter: Any) -> bool:
    return (
        isinstance(getter, dict)
        and getter.get("ret") == 0
        and not getter.get("error")
        and not getter.get("skipped")
    )


def _getter_finished_ns(getter: Any) -> int | None:
    if not isinstance(getter, dict):
        return None
    value = getter.get("finished_at_ns")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _getter_status(getters: dict[str, Any]) -> dict[str, dict[str, Any]]:
    status = {}
    for name, getter in getters.items():
        if not isinstance(getter, dict):
            continue
        status[name] = {
            "ret": getter.get("ret"),
            "error": getter.get("error"),
            "skipped": bool(getter.get("skipped")),
            "started_at_ns": getter.get("started_at_ns"),
            "finished_at_ns": getter.get("finished_at_ns"),
        }
    return status


def extract_dcmi_metrics(
    card: dict[str, Any], *, hbm_usage_unit: str | None = None,
) -> dict[str, Any]:
    """Map one DCMI sampler card without using failed, skipped, or unsupported getters."""
    getters = card.get("getters") if isinstance(card.get("getters"), dict) else {}

    def getter_value(name: str) -> float | None:
        getter = getters.get(name)
        return _finite(getter.get("value")) if _getter_observed(getter) else None

    hbm_info = getters.get("hbm_info")
    hbm_observed = _getter_observed(hbm_info)
    hbm_value = hbm_info.get("value") if hbm_observed else {}
    hbm_value = hbm_value if isinstance(hbm_value, dict) else {}
    hbm_usage = hbm_value.get("memory_usage") if isinstance(hbm_value.get("memory_usage"), dict) else {}
    hbm_used = _finite(hbm_usage.get("value"))
    if hbm_used is None and str(hbm_usage_unit or "").casefold() == "mb":
        hbm_used = _finite(hbm_usage.get("raw"))
    cause_getter = getters.get("freq_reduc_cause")
    cause = cause_getter.get("decoded") if _getter_observed(cause_getter) else None
    cause = cause if isinstance(cause, dict) else {}
    if cause.get("state") != "observed":
        throttling_flag = None
    else:
        limits = [cause.get(key) for key in ("thermal_limit", "power_limit", "svfd")]
        if any(value is True for value in limits):
            throttling_flag = 1
        elif all(value is False for value in limits) and not cause.get("unknown_bits"):
            throttling_flag = 0
        else:
            throttling_flag = None
    return {
        "name": None,
        "aicore_percent": getter_value("aicore_utilization"),
        "hbm_used_mb": hbm_used,
        "temperature_c": getter_value("temperature"),
        "power_w": getter_value("power"),
        "freq_mhz": getter_value("current_freq"),
        "throttling_flag": throttling_flag,
        "resource_interference_flag": None,
        "field_finished_ns": {
            "aicore_percent": _getter_finished_ns(getters.get("aicore_utilization")),
            "hbm_used_mb": _getter_finished_ns(hbm_info),
            "temperature_c": _getter_finished_ns(getters.get("temperature")),
            "power_w": _getter_finished_ns(getters.get("power")),
            "freq_mhz": _getter_finished_ns(getters.get("current_freq")),
            "throttling_flag": _getter_finished_ns(cause_getter),
        },
        "getter_status": _getter_status(getters),
    }
