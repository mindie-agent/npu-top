from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import npu_top
from npu_top import cli as CLI
from npu_top import mcp as MCP
from npu_top.client import ClientError, VawsTopClient, format_mounts, format_npu, format_server
from npu_top.settings import Settings


PAYLOAD = {
    "source": "cache",
    "server": {"id": "s1", "name": "a3", "host": "198.51.100.8", "enabled": True, "tags": [], "status": "online"},
    "collected_at": 10, "age_seconds": 2,
    "summary": {"npu_count": 1, "busy_npu_count": 1, "idle_npu_count": 0, "aicore_percent": 72, "hbm_used_mb": 32768, "hbm_total_mb": 65536},
    "devices": [{"id": 0, "aicore_percent": 72, "hbm_used_mb": 32768, "hbm_total_mb": 65536, "busy": True, "process_count": 1, "containers": ["vllm"], "owners": ["xyz"]}],
}


class FakeClient:
    def __init__(self) -> None:
        self.telemetry_arguments = None
        self.capacity_arguments = None

    def npu(self, host, include_processes=False, detailed_processes=False, mode="cache", timeout=30):
        if host == "missing":
            raise ClientError("server not found: missing")
        return PAYLOAD

    def servers(self):
        return {"source": "cache", "servers": []}

    def server(self, host, mode="cache", include_processes=True, detailed_processes=False, timeout=30):
        return {**PAYLOAD, "system": {}, "storage": {"mounts": []}, "docker": {"containers": []}}

    def capacity(
        self, min_idle_npus=1, max_age_seconds=300, tags=None, include_disabled=False,
        *, min_weight_mount_avail_gb=0.0, prefer_same_package=True, exclude_throttled=True,
    ):
        self.capacity_arguments = (
            min_idle_npus, max_age_seconds, tags, include_disabled, min_weight_mount_avail_gb,
            prefer_same_package, exclude_throttled,
        )
        return {"source": "cache", "requirements": {}, "candidates": []}

    def telemetry_window(self, host, devices, start_ts, end_ts):
        self.telemetry_arguments = (host, devices, start_ts, end_ts)
        return {
            "source": "sqlite",
            "server": {"host": host},
            "window": {"start_ts": start_ts, "end_ts": end_ts},
            "coverage_ratio": 0.5,
            "throttling_detected": None,
            "resource_interference_detected": None,
            "freq_mhz_min": None,
            "power_max_w": 220.0,
            "temp_max_c": 60.0,
            "measurement_limitations": [
                "Explicit throttling flag/register is unavailable; throttling_detected is null.",
                "Resource interference was not measured explicitly; resource_interference_detected is null.",
            ],
            "devices": [{"npu_id": 0, "sample_count": 1, "coverage_ratio": 0.5}],
        }


class AgentCliMcpTests(unittest.TestCase):
    def test_status_defaults_to_live_with_explicit_cache_override(self) -> None:
        self.assertEqual(CLI.parser().parse_args(["status", "198.51.100.8"]).mode, "live")
        self.assertEqual(CLI.parser().parse_args(["status", "198.51.100.8", "--live"]).mode, "live")
        self.assertEqual(CLI.parser().parse_args(["status", "198.51.100.8", "--cache"]).mode, "cache")

    def test_default_formatter_is_compact_and_decision_focused(self) -> None:
        output = format_npu(PAYLOAD)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("198.51.100.8 online age=2s npu=1 busy=1", output)
        self.assertIn("0 busy util=72% hbm=32.0G/64.0G proc=1 owner=xyz", output)

    def test_mount_formatter_hides_virtual_and_container_filesystems(self) -> None:
        output = format_mounts({"storage": {"mounts": [
            {"target": "/", "source": "/dev/root", "fstype": "ext4", "used_percent": 50, "available_bytes": 1024 ** 3, "total_bytes": 2 * 1024 ** 3},
            {"target": "/data/weights", "source": "nfs:/models", "fstype": "nfs4", "weight_candidate": True},
            {"target": "/proc", "source": "proc", "fstype": "proc"},
            {"target": "/boot", "source": "/dev/sda2", "fstype": "ext4"},
            {"target": "/boot/efi", "source": "/dev/sda3", "fstype": "vfat"},
            {"target": "/var/lib/docker/overlay/merged", "source": "overlay", "fstype": "overlay"},
        ]}})
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("/data/weights", output)
        self.assertNotIn("/proc", output)

    def test_server_formatter_groups_same_owner_container_processes(self) -> None:
        process = {"pid": 11, "name": "Worker", "container": "vllm", "owners": ["x01234567"], "npu_memory_mb": 1024}
        payload = {
            **PAYLOAD, "system": {}, "storage": {"mounts": []},
            "devices": [
                {**PAYLOAD["devices"][0], "processes": [process]},
                {**PAYLOAD["devices"][0], "processes": [{**process, "pid": 12}]},
            ],
        }
        output = format_server(payload)
        self.assertIn("proc x2 npu_mem=2.0G container=vllm owner=x01234567 name=Worker pids=11,12", output)
        self.assertEqual(sum(line.startswith("proc ") for line in output.splitlines()), 1)

    def test_client_rejects_remote_endpoint_by_default(self) -> None:
        with self.assertRaises(ClientError):
            VawsTopClient("http://198.51.100.8:8788")
        VawsTopClient("http://localhost:9999")

    def test_client_explicitly_bypasses_environment_proxies(self) -> None:
        with mock.patch("npu_top.client.build_opener") as build:
            VawsTopClient()
        proxy_handler = build.call_args.args[0]
        self.assertEqual(proxy_handler.proxies, {})

    def test_live_client_expands_http_timeout_to_collection_budget(self) -> None:
        client = VawsTopClient()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"server":{}}'
        with mock.patch.object(client.opener, "open", return_value=response) as opened:
            client.server("198.51.100.8", mode="live", timeout=30)
        self.assertEqual(opened.call_args.kwargs["timeout"], 32)

    def test_client_telemetry_window_preserves_fractional_seconds(self) -> None:
        client = VawsTopClient()
        with mock.patch.object(client, "_get", return_value={}) as get:
            client.telemetry_window(
                "198.51.100.8", [0], "1700000000.123456789", 1700000000.5,
            )
        query = get.call_args.args[1]
        self.assertEqual(query["start_ts"], "1700000000.123456789")
        self.assertEqual(query["end_ts"], "1700000000.5")

    def test_mcp_tool_contract_and_dispatch(self) -> None:
        client = FakeClient()
        listed = MCP.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, client,
        )
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        self.assertTrue(tools)
        for name, tool in tools.items():
            self.assertEqual(tool["inputSchema"]["type"], "object", name)
            self.assertTrue(tool["annotations"]["readOnlyHint"], name)
            self.assertIn("Observation only", tool["description"], name)
        called = MCP.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "npu_status", "arguments": {"host": "198.51.100.8"}},
        }, client)
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(called["result"]["structuredContent"]["source"], "cache")

        telemetry = MCP.handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "npu_telemetry_window",
                "arguments": {"host": "198.51.100.8", "devices": [0], "start_ts": 100, "end_ts": 200},
            },
        }, client)
        self.assertFalse(telemetry["result"]["isError"])
        self.assertEqual(client.telemetry_arguments, ("198.51.100.8", [0], 100, 200))
        payload = telemetry["result"]["structuredContent"]
        self.assertEqual(payload["window"], {"start_ts": 100, "end_ts": 200})
        self.assertIsNone(payload["throttling_detected"])
        self.assertIsNone(payload["resource_interference_detected"])
        self.assertIsNone(payload["freq_mhz_min"])
        self.assertEqual(payload["power_max_w"], 220.0)
        self.assertTrue(any("Resource interference" in item for item in payload["measurement_limitations"]))
        self.assertIn("limitation:", telemetry["result"]["content"][0]["text"])

    def test_mcp_capacity_dispatch_forwards_observed_constraints(self) -> None:
        client = FakeClient()
        response = MCP.handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "find_npu_capacity",
                "arguments": {
                    "min_idle_npus": 2, "max_age_seconds": 600, "tags": ["A3"],
                    "include_disabled": True, "min_weight_mount_avail_gb": 64.0,
                    "prefer_same_package": False, "exclude_throttled": False,
                },
            },
        }, client)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(client.capacity_arguments, (2, 600, ["A3"], True, 64.0, False, False))

    def test_serve_cli_overrides_primary_and_legacy_environment(self) -> None:
        environment = {
            "MINDIE_TOP_BIND": "127.0.0.2", "MINDIE_TOP_PORT": "9999",
            "NFM_BIND": "127.0.0.3", "NFM_PORT": "8888",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch("npu_top.static_files.require_static"), mock.patch("npu_top.serve.main") as serve:
                self.assertEqual(CLI.main(["serve", "--bind", "127.0.0.1", "--port", "9100"]), 0)
            serve.assert_called_once()
            settings = Settings.load()
            self.assertEqual(settings.bind, "127.0.0.1")
            self.assertEqual(settings.port, 9100)
            self.assertEqual(os.environ["MINDIE_TOP_BIND"], "127.0.0.1")
            self.assertEqual(os.environ["MINDIE_TOP_PORT"], "9100")

    def test_inventory_default_path_honors_primary_and_legacy_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "primary.json"
            legacy = Path(directory) / "legacy.json"
            with mock.patch.dict(os.environ, {
                "MINDIE_TOP_INVENTORY_FILES": str(primary),
                "NFM_INVENTORY_FILES": str(legacy),
            }, clear=True):
                self.assertEqual(CLI._machines_path(None), primary)
            with mock.patch.dict(os.environ, {"NFM_INVENTORY_FILES": str(legacy)}, clear=True):
                self.assertEqual(CLI._machines_path(None), legacy)

    def test_every_mcp_tool_declares_observation_only_contract(self) -> None:
        for tool in MCP.TOOLS:
            self.assertIn("Observation only", tool["description"], tool["name"])
            self.assertIn("not an allocation or reservation source", tool["description"], tool["name"])
            self.assertTrue(tool["annotations"]["readOnlyHint"], tool["name"])

    def test_cli_help_states_observation_only_contract(self) -> None:
        text = " ".join(CLI.parser().format_help().split())
        self.assertIn("Observation only", text)
        self.assertIn("not an allocation or reservation source", text)

    def test_modern_mcp_returns_result_type(self) -> None:
        request = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        }
        response = MCP.handle_request(request, FakeClient())
        self.assertEqual(response["result"]["resultType"], "complete")

    def test_mcp_stale_cache_is_actionable_without_refresh(self) -> None:
        request = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "npu_status", "arguments": {"host": "198.51.100.8", "max_age_seconds": 1}},
        }
        response = MCP.handle_request(request, FakeClient())
        self.assertTrue(response["result"]["isError"])
        self.assertIn("stale snapshot", response["result"]["content"][0]["text"])

    def test_mcp_server_info_uses_package_version(self) -> None:
        for method in ("initialize", "server/discover"):
            response = MCP.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
                FakeClient(),
            )
            self.assertEqual(response["result"]["serverInfo"]["name"], "npu-top")
            self.assertEqual(response["result"]["serverInfo"]["version"], npu_top.__version__)
            self.assertNotIn("experimental", response["result"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
