# Ascend 950 (A5) NPU probing

The monitor parses the single-ID four-column `npu-smi info` table with its bundled parser. Captured `26.2.0.b007` outputs from four Ascend950DT hosts are regression fixtures under `tests/fixtures/a5-*.txt`.

The first row supplies device ID, name, health, power and temperature. The continuation row supplies bus ID (which may be `NA`), NPU utilization, memory and HBM. Process rows contain a device ID and host PID, with an optional container PID; no chip ID is synthesized.

A5's `npu-smi info -t usages` requires a card ID. The complete overview supplies utilization directly, so its unsupported global usages response is not used to override A5 metrics. Legacy layouts continue through the existing bundled parser.

Empty unrecognized output and incomplete A5 metric rows fail collection instead of producing a successful zero-device snapshot. Host connectivity and per-device health are separate: an online host can contain devices reporting `Warning`.

Validation: `python -m pytest -q tests`. Captured fixtures cover eight devices per host, 98304 MiB HBM per device, Warning health, power/temperature, a host process and its 341 MiB allocation. Additional tests cover nonzero utilization and malformed output. No workload is needed for these parser tests.
