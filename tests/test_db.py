from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from npu_top.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_server_sample_and_history(self) -> None:
        server = self.db.upsert_server({"id":"s1","name":"a3","host":"198.51.100.1","port":22,"username":"root","tags":["A3"]})
        now = int(time.time())
        snapshot = {
            "collected_at": now, "duration_ms": 12,
            "summary": {"cpu_percent":25,"load1":1,"load5":2,"load15":3,"memory_used_bytes":50,"memory_total_bytes":100,"swap_used_bytes":0,"swap_total_bytes":0,"npu_util_percent":75,"hbm_used_mb":50,"hbm_total_mb":100,"npu_count":8,"busy_npu_count":4,"docker_running":3,"disk_max_percent":80},
            "devices": [
                {"npu_id": 0, "name": "Ascend 910", "aicore_percent": 75, "busy": True, "hbm": {"used_mb": 50, "total_mb": 100}},
                {"npu_id": 1, "name": "Ascend 910", "aicore_percent": 0, "busy": False, "hbm": {"used_mb": 10, "total_mb": 100}},
            ],
        }
        self.db.record_success(server["id"], snapshot, True)
        points = self.db.history(server["id"], now - 5, 60)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["npu_util_percent"], 75)
        self.assertEqual(points[0]["hbm_percent"], 50)

        heatmap = self.db.history_heatmap(server["id"], now - 5, 7200, 28800)
        self.assertEqual(len(heatmap), 1)
        self.assertEqual(heatmap[0]["cpu_percent"], 25)
        self.assertEqual(heatmap[0]["devices"][0]["utilization_percent"], 75)
        self.assertEqual(heatmap[0]["devices"][1]["hbm_percent"], 10)

        self.assertTrue(self.db.update_server(server["id"], tags=["A3", "训练"]))
        updated = self.db.get_server(server["id"])
        self.assertEqual(updated["tags"], ["A3", "训练"])
        self.assertTrue(updated["enabled"])
        self.assertTrue(self.db.update_server(server["id"], enabled=False))
        disabled = self.db.get_server(server["id"])
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["tags"], ["A3", "训练"])


if __name__ == "__main__":
    unittest.main()
