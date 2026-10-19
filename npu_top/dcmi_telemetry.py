"""Read-only DCMI AICORE_FREQREDUC_CAUSE telemetry for Ascend A2.

[Role] Decodes the 8-byte DCMI low-power frequency-reduction cause mask and can
run a bounded, read-only probe against the installed `libdcmi.so`.
[Invariants] The raw mask is always preserved. Bit 0 (IDLE) is reported only as
`idle` and is never treated as thermal/power throttling. Undocumented bits are
kept as `UNKNOWN` reasons. A failed/incomplete read leaves every decoded field
`None`, never `False`. Rated-frequency (mask 0) is an observed value, not a
missing one.
[Timeouts & Locks] Probe mode performs one `dcmi_init` and one
`dcmi_get_device_info` per card; the caller bounds the SSH/process timeout.
[Side-Effect Policy] Read-only. Only `dcmi_init` and `dcmi_get_device_info` are
called; no setter, no frequency/power change, and no NPU computation.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Verified against the installed A2 header:
# /usr/local/dcmi/dcmi_interface_api.h sha256
# fadcdbe072d74efd0538e8fdcb58f42ae5a929b82513a36a7623c23e63ccea19
# enum dcmi_main_cmd: DCMI_MAIN_CMD_LP follows DCMI_MAIN_CMD_OS_POWER.
# typedef enum DCMI_LP_SUB_CMD: AICORE_FREQREDUC_CAUSE is item 9.
DCMI_MAIN_CMD_LP = 8
DCMI_LP_SUB_CMD_AICORE_FREQREDUC_CAUSE = 9
DEFAULT_HEADER_PATH = "/usr/local/dcmi/dcmi_interface_api.h"
DEFAULT_LIBRARY_PATH = "/usr/local/dcmi/libdcmi.so"

# Official A2 DCMI_MAIN_CMD_LP / AICORE_FREQREDUC_CAUSE bit definitions
# (HDK 2610 A2DCMI dcmi_077). Names are kept verbatim for traceability.
REASON_BITS: Dict[int, Dict[str, str]] = {
    0: {"name": "IDLE", "category": "idle",
        "description": "AI Core is idle and the clock is lowered to the idle operating point."},
    2: {"name": "THERMAL", "category": "thermal",
        "description": "Die temperature exceeded the allowed range; the clock is limited."},
    3: {"name": "SW_EDP", "category": "power_current",
        "description": "AI Core current exceeded the software EDP limit; the clock is limited."},
    4: {"name": "HW_EDP", "category": "power_current",
        "description": "Transient AI Core current exceeded the hardware EDP limit; fast clock reduction."},
    5: {"name": "POWER_BREAK", "category": "power",
        "description": "Board power exceeded the maximum; fast clock reduction to stabilize supply."},
    8: {"name": "SVFD", "category": "power_integrity",
        "description": "AI Core supply-noise monitor triggered a fast clock reduction."},
    10: {"name": "POWERCAPPING", "category": "power_cap",
         "description": "Out-of-band iBMC power capping is limiting the clock."},
}
IDLE_BITS = frozenset({0})
THERMAL_LIMIT_BITS = frozenset({2})
POWER_LIMIT_BITS = frozenset({3, 4, 5, 10})
SVFD_BITS = frozenset({8})


def _braced_body(text: str, open_brace: int) -> Tuple[str, int]:
    depth = 1
    index = open_brace
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    if depth:
        raise ValueError("unbalanced enum body")
    return text[open_brace:index - 1], index


def parse_enum_values(text: str, enum_name: str) -> Dict[str, int]:
    """Parse a named or typedef C enum from the installed DCMI header."""
    body = None
    for match in re.finditer(r"typedef\s+enum\s*\{", text):
        candidate, end = _braced_body(text, match.end())
        tail = re.match(r"\s*([A-Za-z_]\w*)\s*;", text[end:end + 200])
        if tail and tail.group(1) == enum_name:
            body = candidate
            break
    if body is None:
        match = re.search(rf"\benum\s+{re.escape(enum_name)}\s*\{{", text)
        if match:
            body, _ = _braced_body(text, match.end())
    if body is None:
        raise ValueError(f"enum {enum_name} not found")
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    values: Dict[str, int] = {}
    current = -1
    for item in body.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, raw = item.split("=", 1)
            current = int(raw.strip(), 0)
        else:
            name = item
            current += 1
        values[name.strip()] = current
    return values


def resolve_enum_values(
    header_text: Optional[str],
) -> Tuple[Optional[int], Optional[int], str, Optional[str]]:
    """Return `(main_cmd_lp, lp_sub_cmd, source, error)`.

    The installed header binds the call to this host's ABI. A missing or
    unparseable header leaves the probe unavailable instead of guessing enums.
    """
    if header_text:
        try:
            main_cmd = parse_enum_values(header_text, "dcmi_main_cmd")["DCMI_MAIN_CMD_LP"]
            lp_sub = parse_enum_values(header_text, "DCMI_LP_SUB_CMD")[
                "DCMI_LP_SUB_CMD_AICORE_FREQREDUC_CAUSE"
            ]
            return main_cmd, lp_sub, "header", None
        except (KeyError, ValueError) as exc:
            return None, None, "unavailable", str(exc)
    return None, None, "unavailable", "installed DCMI header is unavailable"


def _unavailable_record(
    *, ret: Optional[int] = None, size_bytes: Optional[int] = None,
    error_type: Optional[str] = None, error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "state": "unavailable",
        "raw_mask": None,
        "raw_mask_hex": None,
        "size_bytes": size_bytes,
        "ret": ret,
        "bits_set": [],
        "reasons": [],
        "unknown_bits": [],
        "idle": None,
        "thermal_limit": None,
        "power_limit": None,
        "svfd": None,
        "error_type": error_type,
        "error": error,
    }


def decode_cause_mask(
    mask: int, *, ret: int = 0, size_bytes: int = 8,
) -> Dict[str, Any]:
    """Decode one AICORE_FREQREDUC_CAUSE value without inferring from other data."""
    if ret != 0:
        return _unavailable_record(ret=ret, size_bytes=size_bytes, error_type="DCMI_ERROR")
    if not isinstance(mask, int) or isinstance(mask, bool) or mask < 0 or size_bytes != 8:
        return _unavailable_record(
            ret=ret, size_bytes=size_bytes, error_type="INVALID_MASK_BUFFER",
        )
    bits = [bit for bit in range(64) if mask & (1 << bit)]
    reasons: List[Dict[str, Any]] = []
    unknown: List[int] = []
    for bit in bits:
        definition = REASON_BITS.get(bit)
        if definition is None:
            unknown.append(bit)
            reasons.append({
                "bit": bit, "name": "UNKNOWN", "category": "unknown",
                "description": "Undocumented bit preserved from the raw DCMI mask.",
            })
        else:
            reasons.append({"bit": bit, **definition})
    return {
        "state": "observed",
        "raw_mask": mask,
        "raw_mask_hex": f"0x{mask:016x}",
        "size_bytes": size_bytes,
        "ret": ret,
        "bits_set": bits,
        "reasons": reasons,
        "unknown_bits": unknown,
        "idle": bool(mask & sum(1 << bit for bit in IDLE_BITS)),
        "thermal_limit": bool(mask & sum(1 << bit for bit in THERMAL_LIMIT_BITS)),
        "power_limit": bool(mask & sum(1 << bit for bit in POWER_LIMIT_BITS)),
        "svfd": bool(mask & sum(1 << bit for bit in SVFD_BITS)),
        "error_type": None,
        "error": None,
    }


def aggregate_cards(records: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-card records; fleet flags stay None when any card is unknown."""
    by_card = {str(card): record for card, record in records.items()}
    observed = [record for record in by_card.values() if record.get("state") == "observed"]
    all_observed = len(observed) == len(by_card) and bool(by_card)
    any_thermal = any(record["thermal_limit"] for record in observed)
    any_power = any(record["power_limit"] for record in observed)
    any_non_idle = any(record["idle"] is False for record in observed)
    unknown_bits = sorted({bit for record in observed for bit in record["unknown_bits"]})
    if all_observed:
        state = "observed"
    elif observed:
        state = "partial"
    else:
        state = "unavailable"
    if any_thermal:
        thermal_limit: Optional[bool] = True
    elif all_observed:
        thermal_limit = False
    else:
        thermal_limit = None
    if any_power:
        power_limit: Optional[bool] = True
    elif all_observed:
        power_limit = False
    else:
        power_limit = None
    if any_non_idle:
        idle: Optional[bool] = False
    elif all_observed:
        idle = True
    else:
        idle = None
    return {
        "state": state,
        "cards": by_card,
        "thermal_limit": thermal_limit,
        "power_limit": power_limit,
        "idle": idle,
        "unknown_bits": unknown_bits,
        "observed_card_count": len(observed),
        "requested_card_count": len(by_card),
    }


def run_probe(
    cards: List[int], *, device_id: int = 0,
    header_path: str = DEFAULT_HEADER_PATH,
    library_path: str = DEFAULT_LIBRARY_PATH,
) -> Dict[str, Any]:
    """Read the cause mask for each card using only DCMI getters."""
    header_text: Optional[str] = None
    header_sha256: Optional[str] = None
    header_error: Optional[str] = None
    try:
        raw = Path(header_path).read_bytes()
        header_text = raw.decode("utf-8", errors="replace")
        header_sha256 = hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        header_error = f"{type(exc).__name__}: {exc}"
    main_cmd, lp_sub, enum_source, enum_error = resolve_enum_values(header_text)

    payload: Dict[str, Any] = {
        "source": "dcmi",
        "stage": "dcmi",
        "library": library_path,
        "header": header_path,
        "header_sha256": header_sha256,
        "header_error": header_error,
        "enum_source": enum_source,
        "enum_error": enum_error,
        "main_cmd_lp": main_cmd,
        "lp_sub_cmd_aicore_freqreduc_cause": lp_sub,
        "device_id": int(device_id),
        "observed_at": int(time.time()),
        "observed_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if main_cmd is None or lp_sub is None:
        records = {str(card): _unavailable_record(
            error_type="DCMI_ABI_UNAVAILABLE", error=enum_error or header_error,
        ) for card in cards}
        payload.update(aggregate_cards(records))
        payload["init_ret"] = None
        return payload

    try:
        lib = ctypes.CDLL(library_path)
        lib.dcmi_init.argtypes = []
        lib.dcmi_init.restype = ctypes.c_int
        lib.dcmi_get_device_info.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_info.restype = ctypes.c_int
        init_ret = int(lib.dcmi_init())
    except (OSError, AttributeError) as exc:
        records = {
            str(card): _unavailable_record(
                error_type=type(exc).__name__, error=str(exc),
            ) for card in cards
        }
        payload.update(aggregate_cards(records))
        payload.update({
            "state": "unsupported", "init_ret": None,
            "error_type": type(exc).__name__, "error": str(exc),
        })
        return payload

    payload["init_ret"] = init_ret
    if init_ret != 0:
        records = {str(card): _unavailable_record(
            ret=init_ret, error_type="DCMI_INIT_ERROR",
        ) for card in cards}
        payload.update(aggregate_cards(records))
        return payload
    records: Dict[str, Dict[str, Any]] = {}
    for card in cards:
        buffer = ctypes.c_uint64(0)
        size = ctypes.c_uint(8)
        try:
            ret = int(lib.dcmi_get_device_info(
                int(card), int(device_id), int(main_cmd), int(lp_sub),
                ctypes.byref(buffer), ctypes.byref(size),
            ))
        except Exception as exc:  # ctypes call failure must stay observable
            records[str(card)] = _unavailable_record(
                size_bytes=8, error_type=type(exc).__name__, error=str(exc),
            )
            continue
        record = decode_cause_mask(int(buffer.value), ret=ret, size_bytes=int(size.value))
        record["card_id"] = int(card)
        records[str(card)] = record
    payload.update(aggregate_cards(records))
    payload["observed_at"] = int(time.time())
    return payload


def _parse_cards(raw: str) -> List[int]:
    cards: List[int] = []
    for part in raw.split(","):
        if "-" in part:
            first, last = part.split("-", 1)
            cards.extend(range(int(first), int(last) + 1))
        elif part.strip():
            cards.append(int(part))
    return sorted(set(cards))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default="0-7", help="e.g. 0,1,2 or 0-7")
    parser.add_argument("--device", type=int, default=0, help="0=NPU die, 1=MCU")
    parser.add_argument("--header", default=DEFAULT_HEADER_PATH)
    parser.add_argument("--library", default=DEFAULT_LIBRARY_PATH)
    args = parser.parse_args(argv)
    payload = run_probe(
        _parse_cards(args.cards), device_id=args.device,
        header_path=args.header, library_path=args.library,
    )
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
