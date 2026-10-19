"""[Role] Measure a repeatable public-input component workload for releases.
[Invariants] Assert business output before reporting latency; no productivity claim.
[Timeouts & Locks] Bounded iterations, no network or background services.
[Side-Effect Policy] Temporary synthetic files and an explicit result JSON only.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runpy
from npu_top.npu_smi import parse_npu_smi_info
captures = runpy.run_path(str(ROOT / "tests" / "test_npu_smi.py"))
a2, a3 = captures["SINGLE_DIE"], captures["DUAL_DIE"]
assert parse_npu_smi_info(a2)["devices"][0]["hbm"]["used_mb"] == 32768
assert [d["phy_id"] for d in parse_npu_smi_info(a3)["devices"][0]["chips"]] == [0, 1]
iterations = 1000
started = time.perf_counter()
for _ in range(iterations):
    parse_npu_smi_info(a2)
    result = parse_npu_smi_info(a3)
elapsed = time.perf_counter() - started
assert result["devices"][0]["processes"][0]["phy_id"] == 1
METRIC = "a2_a3_pair_parse_latency"

Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(json.dumps({
    "status": "pass", "metrics": [{"name": METRIC, "unit": "us/op",
    "value": elapsed * 1_000_000 / iterations, "iterations": iterations}]
}) + "\n", encoding="utf-8")
