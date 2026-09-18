from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from npu_top.device_adapter import DeviceAdapter
from npu_top.inventory import ExternalKeyBootstrap
from npu_top.probe import (
    attach_npu_telemetry,
    attach_process_details,
    build_process_detail_script,
    cpu_percent,
    extract_ownership_labels,
    is_device_busy,
    parse_disks,
    parse_docker,
    parse_meminfo,
    parse_mounts,
    parse_process_details,
    split_sections,
)
from npu_top.settings import Settings
from npu_top.ssh_access import SshAccess


PROJECT = Path(__file__).resolve().parents[1]


class ProbeTests(unittest.TestCase):
    def test_split_and_host_parsers(self) -> None:
        sections = split_sections("noise\n__NFM_SECTION__meminfo\nMemTotal: 1000 kB\nMemAvailable: 250 kB")
        memory = parse_meminfo(sections["meminfo"])
        self.assertEqual(memory["memory_total_bytes"], 1024000)
        self.assertEqual(memory["memory_used_bytes"], 768000)
        self.assertEqual(cpu_percent((100, 40), (200, 60)), 80.0)

    def test_disk_parser(self) -> None:
        rows = parse_disks("Filesystem 1-blocks Used Available Capacity Mounted on\n/dev/sda 1000 800 200 80% /")
        self.assertEqual(rows[0]["used_percent"], 80)
        self.assertEqual(rows[0]["mount"], "/")

    def test_findmnt_parser_flattens_nested_mounts(self) -> None:
        mounts = parse_mounts(json.dumps({"filesystems": [{
            "target": "/", "source": "/dev/root", "fstype": "ext4", "options": "rw",
            "children": [{"target": "/data/models", "source": "nfs:/weights", "fstype": "nfs4", "options": "rw"}],
        }]}))
        self.assertEqual([mount["target"] for mount in mounts], ["/", "/data/models"])

    def test_docker_stats_and_npu_telemetry(self) -> None:
        docker = parse_docker(
            '{"ID":"abc","Names":"worker","Image":"vllm","Status":"Up","State":"running"}',
            '{"Name":"worker","CPUPerc":"12.4%","MemUsage":"2GiB / 8GiB","PIDs":"10"}',
            '{"ServerVersion":"28.0","Driver":"overlay2","DockerRootDir":"/var/lib/docker"}',
        )
        self.assertEqual(docker["containers"][0]["stats"]["cpu_percent"], "12.4%")
        devices = [{"npu_id": 0}]
        attach_npu_telemetry(devices, "| 0 910B4 | OK 91.8 41 0 / 0 |")
        self.assertEqual(devices[0]["temperature_c"], 41)
        self.assertEqual(devices[0]["power_w"], 91.8)

    def test_busy_threshold_ignores_a3_driver_baseline(self) -> None:
        idle = {"processes": [], "aicore_percent": 0, "hbm": {"used_mb": 5989}}
        self.assertFalse(is_device_busy(idle, 8192))
        self.assertTrue(is_device_busy({**idle, "aicore_percent": 2}, 8192))
        self.assertTrue(is_device_busy({**idle, "hbm": {"used_mb": 8192}}, 8192))
        self.assertTrue(is_device_busy({**idle, "processes": [{"pid": 1}]}, 8192))

    def test_process_details_are_decoded_and_attached_to_container(self) -> None:
        encoded = lambda value: base64.b64encode(value.encode()).decode()
        container_id = "a" * 64
        details = parse_process_details("\t".join([
            "421", encoded("root"), encoded("/workspace"), encoded("python -m vllm.entrypoints.openai.api_server"),
            encoded("/usr/bin/python3"), encoded("python3"), encoded(f"0::/system.slice/docker-{container_id}.scope"),
        ]))
        devices = [{"processes": [{"pid": 421, "npu_process_name": "python3", "npu_memory_mb": 2048}]}]
        attach_process_details(devices, details, {"containers": [{"id": container_id, "name": "worker-01", "image": "vllm:latest", "status": "Up"}]})
        process = devices[0]["processes"][0]
        self.assertEqual(process["cwd"], "/workspace")
        self.assertEqual(process["command"], "python -m vllm.entrypoints.openai.api_server")
        self.assertEqual(process["container"]["name"], "worker-01")
        self.assertEqual(process["npu_memory_mb"], 2048)

    def test_process_detail_script_only_contains_validated_pids(self) -> None:
        script = build_process_detail_script([23, 7, 23, -1])
        self.assertIn("for nfm_pid in 7 23; do", script)
        self.assertIn('/proc/$nfm_pid/cmdline', script)
        self.assertIn('/proc/$nfm_pid/cwd', script)

    def test_ownership_labels_extract_employee_ids_and_initials(self) -> None:
        labels = extract_ownership_labels(
            "/home/x01234567/workspace/xyz/project/abc1234567",
            "xyz_pqr_uv_x01234567",
        )
        by_kind = {
            kind: [label["value"] for label in labels if label["kind"] == kind]
            for kind in ("employee_id", "initials")
        }
        self.assertEqual(by_kind["employee_id"], ["x01234567", "abc1234567"])
        self.assertEqual(by_kind["initials"], ["xyz", "pqr", "uv"])
        x_label = next(label for label in labels if label["value"] == "x01234567")
        self.assertEqual(x_label["sources"], ["pwd", "container"])

    def test_ownership_label_boundaries_reject_overlong_candidates(self) -> None:
        labels = extract_ownership_labels(
            "/home/abcd1234567/a1234567890/abcde/team",
            None,
        )
        self.assertEqual(labels, [{"value": "team", "kind": "initials", "sources": ["pwd"]}])

    def test_adapter_uses_bundled_npu_parser_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            adapter = DeviceAdapter(SshAccess(Path(state), PROJECT))
            parsed = adapter.parse_npu(
                """
| NPU Name | Health Power(W) Temp(C) Hugepages-Usage |
| 0 910B4 | OK 91.8 41 0 / 0 |
| NPU Chip | Bus-Id AICore(%) Memory-Usage(MB) HBM-Usage(MB) |
| 0 0 | 0000:C1:00.0 87 1024 / 2048 32768 / 65536 |
""",
                "NPU ID : 0\nAicore Usage Rate(%) : 55\nHBM Usage Rate(%) : 50",
            )
            self.assertEqual(parsed["devices"][0]["aicore_percent"], 55)
            self.assertEqual(parsed["devices"][0]["hbm"]["used_mb"], 32768)

    def test_adapter_accepts_injected_npu_parser(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            parser = mock.Mock(return_value={"devices": [{"npu_id": 7}], "process_records": []})
            adapter = DeviceAdapter(SshAccess(Path(state), PROJECT), npu_parser=parser)
            self.assertEqual(adapter.parse_npu("info", "usages")["devices"][0]["npu_id"], 7)
            parser.assert_called_once_with("info", "usages")

    def test_control_path_stays_below_unix_socket_limit(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            ssh = SshAccess(Path(state), PROJECT, is_windows=False)
            command = ssh.ssh_base({"host": "198.51.100.1", "port": 22, "username": "root"})
            option = next(command[index + 1] for index, value in enumerate(command) if value == "-o" and command[index + 1].startswith("ControlPath="))
            expanded = option.split("=", 1)[1].replace("%C", "x" * 40)
            self.assertLess(len(expanded), 100)

    def test_windows_ssh_omits_unix_control_socket_options(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            ssh = SshAccess(Path(state), PROJECT, is_windows=True)
            with mock.patch.object(ssh, "ensure_key", return_value=ssh.private_key):
                command = ssh.ssh_base({"host": "198.51.100.1", "port": 22, "username": "root"})
            rendered = " ".join(command)
            self.assertNotIn("ControlMaster", rendered)
            self.assertNotIn("ControlPersist", rendered)
            self.assertNotIn("ControlPath", rendered)
            self.assertIn("IdentitiesOnly=yes", rendered)

    def test_windows_private_key_acl_is_scoped_to_current_user_once(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            ssh = SshAccess(Path(state), PROJECT, is_windows=True)
            ssh.private_key.parent.mkdir(parents=True)
            ssh.private_key.write_text("private", encoding="utf-8")
            ssh.public_key.write_text("public", encoding="utf-8")
            responses = [
                subprocess.CompletedProcess(["whoami"], 0, "DOMAIN\\monitor\n", ""),
                subprocess.CompletedProcess(["icacls"], 0, "processed", ""),
            ]
            with mock.patch("npu_top.ssh_access.subprocess.run", side_effect=responses) as run:
                ssh._secure_key_permissions()
                ssh._secure_key_permissions()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["icacls", str(ssh.private_key), "/inheritance:r", "/grant:r", "DOMAIN\\monitor:(R,W)"],
            )

    def test_adapter_has_no_hosts_without_configured_sources(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            settings = Settings(
                project_root=PROJECT, state_dir=Path(state), bind="127.0.0.1", port=1, idle_interval=10,
                history_interval=5, infrastructure_interval=15, retention_days=1, max_workers=1,
                ssh_timeout=2, hbm_busy_threshold_mb=1,
            )
            adapter = DeviceAdapter.from_settings(settings)
            self.assertEqual(adapter.discover_servers(), [])
            self.assertIsNone(adapter.key_bootstrap)

    def test_password_bootstrap_requires_configured_command(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            ssh = SshAccess(Path(state), PROJECT)
            adapter = DeviceAdapter(ssh)
            server = {"host": "198.51.100.1", "port": 22, "username": "root"}
            with (
                mock.patch.object(ssh, "preflight", return_value={"ok": True}),
                mock.patch.object(ssh, "key_auth_works", return_value=False),
                mock.patch.object(ssh, "install_key_with_default_identity", return_value=False),
            ):
                result = adapter.bootstrap_with_passwords(server, ["secret"])
        self.assertFalse(result["ok"])
        self.assertIn("NFM_BOOTSTRAP_COMMAND", result["error"])

    def test_password_bootstrap_delegates_to_external_command_via_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            ssh = SshAccess(Path(state), PROJECT)
            bootstrap = ExternalKeyBootstrap(
                "{python} tool.py install --host {host} --host-port {port} --user {user} --public-key-file {public_key_file}",
            )
            adapter = DeviceAdapter(ssh, key_bootstrap=bootstrap)
            server = {"host": "198.51.100.1", "port": 2222, "username": "ops"}
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                mock.patch.object(ssh, "preflight", return_value={"ok": True}),
                mock.patch.object(ssh, "key_auth_works", side_effect=[False, True]),
                mock.patch.object(ssh, "install_key_with_default_identity", return_value=False),
                mock.patch("npu_top.inventory.subprocess.run", return_value=completed) as run,
            ):
                result = adapter.bootstrap_with_passwords(server, ["one-time"])
            self.assertEqual(result, {"ok": True, "method": "external-bootstrap", "attempts": 1})
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(argv[1:], [
                "tool.py", "install", "--host", "198.51.100.1", "--host-port", "2222", "--user", "ops",
                "--public-key-file", str(ssh.public_key),
            ])
            self.assertEqual(run.call_args.kwargs["input"], "one-time\n")
            self.assertNotIn("one-time", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
