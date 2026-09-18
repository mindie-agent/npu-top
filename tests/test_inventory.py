from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from npu_top.inventory import (
    LOW_PRIORITY_TAG, ExternalKeyBootstrap, HostPoolFile, MachineInventoryFile, merge_sources,
)
from npu_top.settings import Settings


class InventoryTests(unittest.TestCase):
    def test_machine_inventory_reads_only_host_endpoint_fields(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "inventory.json"
            path.write_text(json.dumps({"machines": [
                {
                    "alias": "example-a3",
                    "host": {"ip": "198.51.100.10", "port": 22, "user": "root", "machine_type": "A3", "password": "nope"},
                    "container": {"name": "ignored", "ssh_port": 30022},
                },
                {"alias": "bad", "host": {"ip": "-oProxyCommand=x", "port": 22, "user": "root"}},
                "not-a-machine",
            ]}), encoding="utf-8")
            servers = MachineInventoryFile(path).servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "example-a3")
        self.assertEqual(servers[0]["tags"], ["A3"])
        self.assertTrue(servers[0]["workspace_enabled"])
        self.assertNotIn("nope", json.dumps(servers))
        self.assertNotIn("30022", json.dumps(servers))

    def test_host_pool_reads_first_field_only_and_marks_low_priority(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hosts.txt"
            path.write_text("# comment\n198.51.100.10 secret-column\n\n198.51.100.11\n", encoding="utf-8")
            servers = HostPoolFile(path).servers()
        self.assertEqual([server["host"] for server in servers], ["198.51.100.10", "198.51.100.11"])
        self.assertEqual(servers[0]["tags"], [LOW_PRIORITY_TAG])
        self.assertFalse(servers[0]["workspace_enabled"])
        self.assertNotIn("secret-column", json.dumps(servers))

    def test_merge_keeps_first_source_per_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inventory = Path(root) / "inventory.json"
            inventory.write_text(json.dumps({"machines": [
                {"alias": "active", "host": {"ip": "198.51.100.10", "port": 22, "user": "root", "machine_type": "A3"}},
            ]}), encoding="utf-8")
            pool = Path(root) / "hosts.txt"
            pool.write_text("198.51.100.10\n198.51.100.11\n", encoding="utf-8")
            servers = merge_sources([MachineInventoryFile(inventory), HostPoolFile(pool)])
        self.assertEqual(len(servers), 2)
        active = next(server for server in servers if server["host"] == "198.51.100.10")
        extra = next(server for server in servers if server["host"] == "198.51.100.11")
        self.assertEqual(active["tags"], ["A3"])
        self.assertTrue(active["workspace_enabled"])
        self.assertEqual(extra["tags"], [LOW_PRIORITY_TAG])
        self.assertFalse(extra["workspace_enabled"])

    def test_missing_files_are_empty_sources(self) -> None:
        missing = Path("/nonexistent/inventory.json")
        self.assertEqual(MachineInventoryFile(missing).servers(), [])
        self.assertEqual(HostPoolFile(missing).servers(), [])

    def test_settings_parse_path_lists_and_bootstrap_command(self) -> None:
        env = {
            "NFM_INVENTORY_FILES": os.pathsep.join(["/a/inventory.json", "", "/b/inventory.json"]),
            "NFM_HOST_POOL_FILES": "/a/hosts.txt",
            "NFM_BOOTSTRAP_COMMAND": "  ",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            settings = Settings.load()
        self.assertEqual(settings.inventory_files, (Path("/a/inventory.json"), Path("/b/inventory.json")))
        self.assertEqual(settings.host_pool_files, (Path("/a/hosts.txt"),))
        self.assertIsNone(settings.bootstrap_command)

    def test_bootstrap_template_rejects_unknown_placeholders(self) -> None:
        bootstrap = ExternalKeyBootstrap("tool --host {host} --flag {unknown}")
        with self.assertRaises(ValueError):
            bootstrap.render({"host": "198.51.100.10", "port": 22, "username": "root"}, Path("/k.pub"))
        with self.assertRaises(ValueError):
            ExternalKeyBootstrap("   ")


if __name__ == "__main__":
    unittest.main()
