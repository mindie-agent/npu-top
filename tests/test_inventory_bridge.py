from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from npu_top import cli as CLI
from npu_top.db import Database
from npu_top.inventory_bridge import (
    InventoryBridgeError, _fsync_directory, import_from_coordinator, inventory_lock,
    merge_observations, observed_machine_records, read_machines, write_machines,
)


def machine_document() -> dict:
    return {
        "version": 7,
        "default_alias": "a3-165",
        "machines": [{
            "alias": "a3-165",
            "host": {
                "ip": "192.168.13.165", "port": 22, "user": "root",
                "identity_file": "/home/operator/.ssh/id_ed25519",
            },
            "container": {"name": "mindie-dev", "ssh_port": 32222, "user": "root"},
            "ssh": {"extra": "coordinator-owned"},
            "tags": ["coordinator"],
            "hardware": {"chip_summary": "910C", "total_npus": 16},
            "mounts": [{"target": "/data/models", "weight_candidate": False, "note": "keep"}],
            "unknown": {"preserve": True},
        }],
        "unrelated": {"preserve": True},
    }


def observation() -> dict:
    return {
        "alias": "a3-165", "host": "192.168.13.165", "port": 22, "username": "root",
        "hardware": {"soc": "Ascend910C", "npu_count": 16, "dies_per_package": 2},
        "tags": ["A3", "910C", "dual-die"],
        "mounts": [
            {"target": "/data/models", "weight_candidate": True},
            {"target": "/data/weights", "weight_candidate": True},
        ],
    }


class InventoryBridgeTests(unittest.TestCase):
    def test_merge_updates_observed_fields_and_preserves_coordinator_metadata(self) -> None:
        merged, counts = merge_observations(machine_document(), [observation()])
        row = merged["machines"][0]
        self.assertEqual(row["host"]["identity_file"], "/home/operator/.ssh/id_ed25519")
        self.assertEqual(row["container"], {"name": "mindie-dev", "ssh_port": 32222, "user": "root"})
        self.assertEqual(row["ssh"], {"extra": "coordinator-owned"})
        self.assertEqual(row["unknown"], {"preserve": True})
        self.assertEqual(merged["unrelated"], {"preserve": True})
        self.assertEqual(merged["default_alias"], "a3-165")
        self.assertEqual(row["hardware"]["chip_summary"], "910C")
        self.assertEqual(row["hardware"]["npu_count"], 16)
        self.assertEqual(row["hardware"]["dies_per_package"], 2)
        self.assertEqual(row["tags"], ["coordinator", "A3", "910C", "dual-die"])
        mounts = {mount["target"]: mount for mount in row["mounts"]}
        self.assertTrue(mounts["/data/models"]["weight_candidate"])
        self.assertEqual(mounts["/data/models"]["note"], "keep")
        self.assertTrue(mounts["/data/weights"]["weight_candidate"])
        self.assertEqual(counts["matched"], 1)
        self.assertEqual(counts["hardware"], 1)
        self.assertEqual(counts["tags"], 1)

    def test_merge_is_idempotent(self) -> None:
        once, _ = merge_observations(machine_document(), [observation()])
        twice, counts = merge_observations(once, [observation()])
        self.assertEqual(once, twice)
        self.assertEqual(counts["tags"], 0)

    def test_invalid_document_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            path.write_text('{"machines": {}}', encoding="utf-8")
            with self.assertRaises(InventoryBridgeError):
                read_machines(path)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"machines": {}}')

    def test_failed_atomic_replace_leaves_original_intact(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            original = json.dumps(machine_document(), indent=2) + "\n"
            path.write_text(original, encoding="utf-8")
            with mock.patch("npu_top.inventory_bridge.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_machines(path, {**machine_document(), "version": 8})
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob(".machines.json.*.tmp")), [])

    def test_adjacent_lock_blocks_a_second_writer_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            path.write_text(json.dumps(machine_document()), encoding="utf-8")
            started = threading.Event()
            errors: list[BaseException] = []

            def write() -> None:
                started.set()
                try:
                    write_machines(path, {**machine_document(), "version": 8})
                except BaseException as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            with inventory_lock(path):
                thread = threading.Thread(target=write)
                thread.start()
                self.assertTrue(started.wait(timeout=1))
                time.sleep(0.1)
                self.assertTrue(thread.is_alive(), "writer bypassed the adjacent lock")
                self.assertEqual(read_machines(path)["version"], 7)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(read_machines(path)["version"], 8)
            self.assertTrue((Path(root) / "machines.json.lock").is_file())

    def test_directory_fsync_errors_are_not_silently_swallowed(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows has no portable directory fsync")
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("npu_top.inventory_bridge.os.fsync", side_effect=OSError("durability")):
                with self.assertRaises(InventoryBridgeError):
                    _fsync_directory(Path(root))

    def test_write_round_trip_preserves_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            path.write_text(json.dumps(machine_document()), encoding="utf-8")
            os.chmod(path, 0o640)
            write_machines(path, machine_document())
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(read_machines(path), machine_document())

    def test_observed_record_derives_c1_hardware_tags_and_weight_mounts(self) -> None:
        server = {
            "id": "s1", "name": "a3-165", "host": "192.168.13.165",
            "port": 22, "username": "root", "tags": ["A3"],
        }
        snapshot = {
            "status": "online", "collected_at": 990,
            "summary": {"npu_count": 8, "busy_npu_count": 0},
            "devices": [{"npu_id": index, "name": "Ascend910"} for index in range(8)],
            "mounts": [{"target": "/data/models", "fstype": "nfs4"}],
            "disks": [{"mount": "/data/models", "available_bytes": 100 * 1024 ** 3}],
        }
        records = observed_machine_records([server], {"s1": snapshot})
        self.assertEqual(records[0]["hardware"], {
            "soc": "Ascend910C", "dies_per_package": 2, "npu_count": 16,
        })
        self.assertEqual(records[0]["tags"], ["A3", "910C", "dual-die"])
        self.assertEqual(records[0]["mounts"], [{"target": "/data/models", "weight_candidate": True}])

    def test_projected_per_die_rows_detect_a3_without_preexisting_tags(self) -> None:
        server = {
            "id": "s1", "name": "unknown-host", "host": "192.168.13.165",
            "port": 22, "username": "root", "tags": [],
        }
        snapshot = {
            "status": "online", "collected_at": 990,
            "summary": {"npu_count": 8, "busy_npu_count": 0},
            "devices": [
                {"npu_id": index, "name": "Ascend910", "chips": [
                    {"chip_id": 0, "phy_id": 2 * index},
                    {"chip_id": 1, "phy_id": 2 * index + 1},
                ]}
                for index in range(8)
            ],
        }
        record = observed_machine_records([server], {"s1": snapshot})[0]
        self.assertEqual(record["hardware"]["soc"], "Ascend910C")
        self.assertEqual(record["hardware"]["npu_count"], 16)
        self.assertEqual(record["tags"], ["A3", "910C", "dual-die"])

    def test_import_from_coordinator_uses_existing_server_registry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            path.write_text(json.dumps(machine_document()), encoding="utf-8")
            before = path.read_bytes()
            db = Database(Path(root) / "monitor.sqlite3")
            db.initialize()
            try:
                first = import_from_coordinator(path, db)
                second = import_from_coordinator(path, db)
                servers = db.list_servers()
            finally:
                db.close()
            self.assertEqual(first["registered"], 1)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["host"], "192.168.13.165")
            self.assertEqual(servers[0]["username"], "root")
            self.assertEqual(servers[0]["tags"], ["coordinator"])
            self.assertEqual(path.read_bytes(), before)

    def test_cli_from_coordinator_uses_explicit_file_and_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            path.write_text(json.dumps(machine_document()), encoding="utf-8")
            env = {"NFM_STATE_DIR": str(Path(root) / "state")}
            output = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(output):
                code = CLI.main([
                    "--json", "inventory", "sync", "--from-coordinator", "--machines", str(path),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["from_coordinator"]["registered"], 1)
            db = Database(Path(root) / "state" / "monitor.sqlite3")
            db.initialize()
            try:
                self.assertEqual(len(db.list_servers()), 1)
            finally:
                db.close()

    def test_cli_to_coordinator_merges_cached_observations(self) -> None:
        class FakeClient:
            def __init__(self, url=None):
                pass

            def servers(self):
                return {"servers": [{
                    "id": "s1", "name": "a3-165", "host": "192.168.13.165",
                    "port": 22, "username": "root", "tags": ["A3"],
                }]}

            def server(self, host, **kwargs):
                return {
                    "status": "online", "collected_at": 990,
                    "summary": {"npu_count": 8, "busy_npu_count": 0},
                    "devices": [{"npu_id": index, "name": "Ascend910"} for index in range(8)],
                    "mounts": [{"target": "/data/models", "fstype": "nfs4"}],
                    "disks": [{"mount": "/data/models", "available_bytes": 100 * 1024 ** 3}],
                }

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "machines.json"
            path.write_text(json.dumps(machine_document()), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(CLI, "VawsTopClient", FakeClient), redirect_stdout(output):
                code = CLI.main([
                    "--json", "inventory", "sync", "--to-coordinator", "--machines", str(path),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["to_coordinator"]["tags"], 1)
            row = read_machines(path)["machines"][0]
            self.assertEqual(row["hardware"]["npu_count"], 16)
            self.assertTrue(any(mount["target"] == "/data/models" and mount["weight_candidate"]
                                for mount in row["mounts"]))


if __name__ == "__main__":
    unittest.main()
