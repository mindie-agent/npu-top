# Ascend 950 (A5) Probing & Parsing Guide

[中文](./a5-probing.zh.md) · **English** · [Back to Documentation Index](./README.en.md)

The `npu-smi info` table layout on **Ascend 950 (A5, such as Ascend950DT)** differs from earlier 910B (single-die) and A3 / 910C (dual-die) generations. `npu-top` includes native support for the A5 four-column table in its bundled parser, backed by real `26.2.0.b007` outputs captured from four Ascend950DT servers under `tests/fixtures/a5-*.txt`.

---

## How A5 Output Differs & How `npu-top` Parses It

1. **Four-Column Two-Row Device Entries**:
   - **Primary Row**: Reports the single device ID, chip name (e.g., `Ascend950DT`), health status (`OK` / `Warning`), power draw (W), and temperature (℃).
   - **Continuation Row**: Reports the PCIe Bus ID (which may be `NA`), NPU utilization (`%`), memory usage, and HBM usage (e.g., `98304 MiB` per card).
2. **Process Rows Without a Synthetic Chip ID**:
   Process table rows on A5 report the device ID, host PID (plus optional container PID), and HBM allocation directly — `npu-top` maps them straight to the device without synthesizing a fake chip column.
3. **No Global `-t usages` Override**:
   On A5, running `npu-smi info -t usages` without a card ID is unsupported because the main overview table already provides complete utilization metrics. `npu-top` uses the overview metrics directly for A5 while keeping the existing parser path for 910B and A3 hosts.
4. **Strict Failure on Malformed Output (Never Fakes "0 Devices")**:
   Unrecognized empty output or truncated A5 metric rows fail the probe visibly rather than recording a fake "online host with 0 NPUs" snapshot. Host connectivity and per-card health remain independent, so an online server can accurately display individual cards in a `Warning` state.

---

## Running the A5 Parser Regression Tests Locally

You can run the full A5 regression test suite locally without any physical NPU hardware (covering 8 cards per host, 98,304 MiB HBM per card, `Warning` health states, power/temperature readings, active host processes, non-zero utilization, and malformed output rejection):

```bash
python3 -m pytest -q tests
```
