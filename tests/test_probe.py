from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from npu_top.device_adapter import DeviceAdapter
from npu_top.probe import (
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

    def test_disk_parser_excludes_overlay_and_boot_partitions(self) -> None:
        rows = parse_disks("\n".join([
            "Filesystem 1-blocks Used Available Capacity Mounted on",
            "/dev/sda1 1000 400 600 40% /",
            "overlay 1000 990 10 99% /var/lib/docker/overlay2/abc/merged",
            "/dev/sda2 1000 950 50 95% /boot",
            "/dev/sda3 1000 920 80 92% /boot/efi",
            "/dev/sda4 1000 910 90 91% /efi",
            "/dev/sdb1 1000 500 500 50% /data/models",
            "/dev/sdc1 1000 700 300 70% /bootdata",
        ]))
        self.assertEqual([row["mount"] for row in rows], ["/", "/data/models", "/bootdata"])
        self.assertEqual(max(row["used_percent"] for row in rows), 70)

    def test_findmnt_parser_flattens_nested_mounts(self) -> None:
        mounts = parse_mounts(json.dumps({"filesystems": [{
            "target": "/", "source": "/dev/root", "fstype": "ext4", "options": "rw",
            "children": [{"target": "/data/models", "source": "nfs:/weights", "fstype": "nfs4", "options": "rw"}],
        }]}))
        self.assertEqual([mount["target"] for mount in mounts], ["/", "/data/models"])

    def test_mount_parser_excludes_overlay_and_boot_in_both_formats(self) -> None:
        filesystems = [{
            "target": "/", "source": "/dev/sda1", "fstype": "ext4",
            "children": [
                {"target": "/boot", "source": "/dev/sda2", "fstype": "ext4", "children": [
                    {"target": "/boot/efi", "source": "/dev/sda3", "fstype": "vfat"},
                ]},
                {"target": "/var/lib/docker/overlay2/abc/merged", "source": "overlay", "fstype": "overlay"},
                {"target": "/data/models", "source": "nfs:/weights", "fstype": "nfs4"},
            ],
        }]
        expected = ["/", "/data/models"]
        self.assertEqual(
            [mount["target"] for mount in parse_mounts(json.dumps({"filesystems": filesystems}))], expected,
        )
        proc_mounts = "\n".join([
            "/dev/sda1 / ext4 rw 0 0",
            "/dev/sda2 /boot ext4 rw 0 0",
            "overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0",
            "nfs:/weights /data/models nfs4 rw 0 0",
        ])
        self.assertEqual([mount["target"] for mount in parse_mounts(proc_mounts)], expected)

    def test_docker_stats_parser(self) -> None:
        docker = parse_docker(
            '{"ID":"abc","Names":"worker","Image":"vllm","Status":"Up","State":"running"}',
            '{"Name":"worker","CPUPerc":"12.4%","MemUsage":"2GiB / 8GiB","PIDs":"10"}',
            '{"ServerVersion":"28.0","Driver":"overlay2","DockerRootDir":"/var/lib/docker"}',
        )
        self.assertEqual(docker["containers"][0]["stats"]["cpu_percent"], "12.4%")

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
        self.assertFalse(process["disk_io_wait"])

        # 9-column format with D-state and lock_page_killable wchan
        d_details = parse_process_details("\t".join([
            "422", encoded("root"), encoded("/workspace"), encoded("vllm serve"),
            encoded("/usr/bin/python3"), encoded("python3"), encoded(f"0::/system.slice/docker-{container_id}.scope"),
            encoded("D"), encoded("lock_page_killable"),
        ]))
        d_devices = [{"processes": [{"pid": 422, "npu_process_name": "python3", "npu_memory_mb": 16384}]}]
        attach_process_details(d_devices, d_details, {"containers": []})
        d_proc = d_devices[0]["processes"][0]
        self.assertEqual(d_proc["proc_state"], "D")
        self.assertEqual(d_proc["wchan"], "lock_page_killable")
        self.assertTrue(d_proc["disk_io_wait"])

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



    def test_adapter_has_no_hosts_without_configured_sources(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            settings = Settings(
                project_root=PROJECT, state_dir=Path(state), bind="127.0.0.1", port=1, idle_interval=10,
                history_interval=5, infrastructure_interval=15, retention_days=1, max_workers=1,
                ssh_timeout=2, hbm_busy_threshold_mb=1,
            )
            adapter = DeviceAdapter.from_settings(settings)
            self.assertEqual(adapter.discover_servers(), [])





    def test_default_ssh_timeout_exceeds_inner_probe_budget(self) -> None:
        from npu_top.probe import HostProbe

        with mock.patch.dict("os.environ", {}, clear=True):
            settings = Settings.load()
        self.assertEqual(settings.ssh_timeout, 20)

        adapter = mock.MagicMock()
        adapter.preflight.return_value = {"ok": True}
        adapter.parse_npu.return_value = {"devices": [], "process_records": []}
        probe = HostProbe(adapter, timeout=settings.ssh_timeout)
        observed_timeouts: list[int] = []

        def fake_run_script(_server, _script, timeout):
            observed_timeouts.append(timeout)
            return subprocess.CompletedProcess(
                [], 0, "__NFM_SECTION__proc_stat\ncpu  100 0 50 850 0 0 0 0\n__NFM_SECTION__npu_info_rc\n0\n", ""
            )

        with mock.patch.object(probe, "_run_script", side_effect=fake_run_script):
            probe.collect({"id": "s1", "name": "s1", "host": "10.0.0.1"}, include_infrastructure=False)
            probe.collect({"id": "s1", "name": "s1", "host": "10.0.0.1"}, include_infrastructure=True)
        self.assertEqual(observed_timeouts, [20, 45])

    def test_probe_collect_preserves_fractional_timestamp(self) -> None:
        from npu_top.probe import HostProbe

        adapter = mock.MagicMock()
        adapter.preflight.return_value = {"ok": True}
        adapter.parse_npu.return_value = {"devices": [], "process_records": []}
        probe = HostProbe(adapter, timeout=20)
        result = subprocess.CompletedProcess(
            [], 0, "__NFM_SECTION__proc_stat\ncpu  100 0 50 850 0 0 0 0\n__NFM_SECTION__npu_info_rc\n0\n", "",
        )
        with mock.patch.object(probe, "_run_script", return_value=result), mock.patch(
            "npu_top.probe.time.time_ns", return_value=1_700_000_000_123_456_789,
        ):
            snapshot = probe.collect(
                {"id": "s1", "name": "s1", "host": "10.0.0.1"}, include_infrastructure=False,
            )
        self.assertEqual(snapshot["collected_at_ns"], 1_700_000_000_123_456_789)
        self.assertAlmostEqual(snapshot["collected_at"], 1_700_000_000.123456789, places=6)


if __name__ == "__main__":
    unittest.main()
