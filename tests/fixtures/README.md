# npu-smi captured fixtures

Captured on 2026-09-14 using the monitor's SSH adapter and environment setup. `npu-smi-captures.json` records source hosts, versions, commands and SHA256 checksums. Files retain the raw command text after removing the monitor's section framing; they do not contain SSH credentials or process command lines.

- A3: `80.5.17.106`, npu-smi `26.1.0.b087`, `a3-106.txt` and `a3-usages.txt`. Eight Ascend910 devices, two chips each, 16 physical IDs and 16 VLLM process records. Tests assert chip-to-device mapping, HBM aggregation, host PID attribution, health, power and temperature.
- A5: `141.61.33.23`, npu-smi `26.2.0.b007`, `a5-23.txt` and `a5-usages.txt`. Eight Ascend950DT devices, NA bus IDs and one chipless process record. Additional `a5-13/21/22.txt` captures cover empty process tables and Warning health.
- Both global `npu-smi info -t usages` commands returned `This command must input card id.` followed by help. Keep this actual response to ensure help text does not overwrite overview metrics.

Run `python -m pytest -q tests` from the repository root. Tests use the bundled parser and captured files.
