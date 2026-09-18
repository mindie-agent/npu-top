from __future__ import annotations

import unittest

from npu_top.npu_smi import apply_usage_overrides, parse_npu, parse_npu_smi_info, parse_npu_smi_usages


SINGLE_DIE = """
+------------------------------------------------------------------------------------------------+
| npu-smi 24.1.rc1                 Version: 24.1.rc1                                             |
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip                      | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     910B4               | OK            | 91.8        41                0    / 0             |
| 0                         | 0000:C1:00.0  | 87          1024 / 2048       32768 / 65536        |
+===========================+===============+====================================================+
| 1     910B4               | OK            | 60.2        38                0    / 0             |
| 0                         | 0000:C2:00.0  | 0           0    / 2048       3306  / 65536        |
+===========================+===============+====================================================+
+---------------------------+---------------+----------------------------------------------------+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| 0       0                 | 4321          | python3                  | 30000                   |
+===========================+===============+====================================================+
| No running processes found in NPU 1                                                            |
+===========================+===============+====================================================+
"""

DUAL_DIE = """
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip  Phy-ID              | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     Ascend910           | OK            | 176.9       46                0    / 0             |
| 0     0                   | 0000:9C:00.0  | 0           0    / 0          3343  / 65536        |
| 1     1                   | 0000:9D:00.0  | 95          0    / 0          40000 / 65536        |
+===========================+===============+====================================================+
| 1     Ascend910           | OK            | 150.0       44                0    / 0             |
| 0     2                   | 0000:9E:00.0  | 0           0    / 0          3343  / 65536        |
| 1     3                   | 0000:9F:00.0  | 0           0    / 0          3343  / 65536        |
+===========================+===============+====================================================+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| 0       1                 | 777           | python                   | 36000                   |
+===========================+===============+====================================================+
"""

FLAT_PROCESS_TABLE = """
| 0     310P3               | OK            | 17.0        50                0    / 0             |
| 0                         | 0000:01:00.0  | 12          512  / 21527      0     / 0            |
| NPU                       | Process id    | Process name             | Process memory(MB)      |
| 0                         | 99            | infer                    | 256                     |
"""


class NpuSmiParserTests(unittest.TestCase):
    def test_single_die_devices_with_processes(self) -> None:
        parsed = parse_npu_smi_info(SINGLE_DIE)
        devices = parsed["devices"]
        self.assertEqual([device["npu_id"] for device in devices], [0, 1])
        first = devices[0]
        self.assertEqual(first["name"], "910B4")
        self.assertEqual(first["health"], "OK")
        self.assertEqual(first["power_w"], 91.8)
        self.assertEqual(first["temperature_c"], 41)
        self.assertEqual(first["bus_id"], "0000:C1:00.0")
        self.assertEqual(first["aicore_percent"], 87)
        self.assertEqual(first["hbm"], {"used_mb": 32768, "total_mb": 65536})
        self.assertEqual(first["memory"], {"used_mb": 1024, "total_mb": 2048})
        self.assertNotIn("chips", first)
        self.assertEqual(first["processes"], [{"npu_id": 0, "pid": 4321, "npu_process_name": "python3", "npu_memory_mb": 30000, "chip_id": 0}])
        self.assertEqual(devices[1]["processes"], [])
        self.assertEqual(len(parsed["process_records"]), 1)

    def test_dual_die_devices_expose_physical_dies_and_aggregate(self) -> None:
        devices = parse_npu_smi_info(DUAL_DIE)["devices"]
        self.assertEqual(len(devices), 2)
        card = devices[0]
        self.assertEqual([chip["phy_id"] for chip in card["chips"]], [0, 1])
        self.assertEqual(card["chips"][1]["aicore_percent"], 95)
        self.assertEqual(card["aicore_percent"], 95)
        self.assertEqual(card["hbm"], {"used_mb": 43343, "total_mb": 131072})
        process = card["processes"][0]
        self.assertEqual((process["pid"], process["chip_id"], process["phy_id"]), (777, 1, 1))
        self.assertEqual([chip["phy_id"] for chip in devices[1]["chips"]], [2, 3])
        self.assertEqual(devices[1]["processes"], [])

    def test_flat_process_table_layout(self) -> None:
        devices = parse_npu_smi_info(FLAT_PROCESS_TABLE)["devices"]
        self.assertEqual(devices[0]["processes"][0]["pid"], 99)
        self.assertNotIn("chip_id", devices[0]["processes"][0])

    def test_garbage_and_errors_produce_no_devices(self) -> None:
        self.assertEqual(parse_npu_smi_info("npu-smi: command not found")["devices"], [])
        self.assertEqual(parse_npu_smi_info("")["process_records"], [])

    def test_usages_override_aicore_per_device_and_per_chip(self) -> None:
        usages = parse_npu_smi_usages(
            "\tNPU ID                         : 0\n"
            "\tChip Count                     : 1\n"
            "\tHBM Capacity(MB)               : 65536\n"
            "\tHBM Usage Rate(%)              : 50\n"
            "\tAicore Usage Rate(%)           : 55\n"
            "\tNPU ID                         : 1\n"
            "\tChip ID                        : 0\n"
            "\tAicore Usage Rate(%)           : 10\n"
            "\tChip ID                        : 1\n"
            "\tAicore Usage Rate(%)           : 70\n"
        )
        self.assertEqual(usages[0]["aicore_percent"], 55)
        self.assertEqual(usages[0]["hbm_percent"], 50)
        self.assertEqual(usages[1]["chips"], {0: {"aicore_percent": 10}, 1: {"aicore_percent": 70}})
        devices = [
            {"npu_id": 0, "aicore_percent": 87},
            {"npu_id": 1, "aicore_percent": 0, "chips": [
                {"chip_id": 0, "aicore_percent": 0}, {"chip_id": 1, "aicore_percent": 0},
            ]},
        ]
        apply_usage_overrides(devices, usages)
        self.assertEqual(devices[0]["aicore_percent"], 55)
        self.assertEqual(devices[1]["aicore_percent"], 70)
        self.assertEqual(devices[1]["chips"][0]["aicore_percent"], 10)

    def test_usages_error_text_is_ignored(self) -> None:
        parsed = parse_npu(SINGLE_DIE, "Please input the correct parameters. -i is required.")
        self.assertEqual(parsed["devices"][0]["aicore_percent"], 87)


if __name__ == "__main__":
    unittest.main()
