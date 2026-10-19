from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from npu_top import cli as CLI, dcmi_telemetry, npu_smi
from npu_top.agent_view import AgentQueryError, capacity_candidates


def server(server_id: str, host: str, tags: list[str] | None = None) -> dict:
    return {
        "id": server_id, "name": server_id, "host": host, "port": 22,
        "enabled": True, "tags": tags or ["A3"], "last_error": None,
    }


def device(npu_id: int, *, phy_ids: tuple[int, ...] = (), busy: bool = False,
           health: str | None = None, temperature_c: float | None = None,
           process_chips: tuple[int, ...] = ()) -> dict:
    chips = [
        {
            "chip_id": index, "phy_id": phy_id, "aicore_percent": 0,
            "hbm": {"used_mb": 100, "total_mb": 65536},
        }
        for index, phy_id in enumerate(phy_ids)
    ]
    row = {
        "npu_id": npu_id, "name": "Ascend910", "busy": busy,
        "aicore_percent": 0, "hbm": {"used_mb": 7168, "total_mb": 131072},
        "processes": [{"pid": 100 + index, "chip_id": chip} for index, chip in enumerate(process_chips)],
        "temperature_c": temperature_c,
    }
    if chips:
        row["chips"] = chips
    if health is not None:
        row["health"] = health
    return row


def snapshot(devices: list[dict], *, mounts: list[dict] | None = None,
             disks: list[dict] | None = None, **extra) -> dict:
    return {
        "server_id": "server", "status": "online", "collected_at": 990,
        "summary": {
            "npu_count": len(devices), "busy_npu_count": sum(bool(row.get("busy")) for row in devices),
            "npu_util_percent": 0, "hbm_used_mb": 0, "hbm_total_mb": 0,
        },
        "devices": devices, "mounts": mounts or [], "disks": disks or [], **extra,
    }


def real_devices(name: str) -> list[dict]:
    raw = (Path(__file__).parent / "fixtures" / name).read_text()
    return npu_smi.parse_npu(raw)["devices"]


def actual_dcmi_cause() -> dict:
    path = Path(__file__).parent / "fixtures" / "dcmi-metrics-slice.jsonl"
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record.get("type") == "sample":
            return record["cards"]["0"]["getters"]["freq_reduc_cause"]["decoded"]
    raise AssertionError("DCMI slice contains no sample")


class CapacityConstraintTests(unittest.TestCase):
    def test_package_pairs_use_observed_physical_ids_not_rows_or_chip_ids(self) -> None:
        snapshot_data = snapshot([
            device(10, phy_ids=(20, 21)),
            device(11, phy_ids=(22, 23)),
            device(12),
        ])
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot_data}, now=1000,
        )
        candidate = result["candidates"][0]
        self.assertEqual(candidate["IdlePackagePairs"], [[20, 21], [22, 23]])
        self.assertNotIn(10, candidate["IdlePackagePairs"][0])
        self.assertEqual(candidate["ThermalHealth"], "unknown")
        self.assertIsNone(candidate["MaxWeightMountAvailGb"])
        self.assertIs(result["observation"]["allocation_authority"], False)

    def test_real_a3_capture_yields_physical_die_pairs(self) -> None:
        raw = (Path(__file__).parent / "fixtures" / "a3-192-168-13-165-idle.txt").read_text()
        devices = npu_smi.parse_npu(raw)["devices"]
        for row in devices:
            row["busy"] = False
        parsed_snapshot = snapshot(devices)
        candidate = capacity_candidates(
            [server("server", "192.168.13.165")], {"server": parsed_snapshot}, now=1000,
        )["candidates"][0]
        self.assertEqual(candidate["IdlePackagePairs"], [
            [2 * index, 2 * index + 1] for index in range(8)
        ])

    def test_unmapped_phy_id_never_invents_a_pair(self) -> None:
        row = device(0, phy_ids=(0, 1))
        row["chips"][1].pop("phy_id")
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot([row])}, now=1000,
        )
        self.assertEqual(result["candidates"][0]["IdlePackagePairs"], [])

    def test_only_fully_idle_dies_form_a_same_package_pair(self) -> None:
        snapshot_data = snapshot([
            device(0, phy_ids=(0, 1), busy=True, process_chips=(0,)),
            device(1, phy_ids=(2, 3)),
        ])
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot_data}, now=1000,
        )
        self.assertEqual(result["candidates"][0]["IdlePackagePairs"], [[2, 3]])

    def test_weight_mount_filter_uses_largest_candidate_mount(self) -> None:
        snapshot_data = snapshot(
            [device(0, phy_ids=(0, 1))],
            mounts=[
                {"target": "/data/models", "fstype": "nfs4"},
                {"target": "/scratch", "fstype": "ext4"},
            ],
            disks=[
                {"mount": "/data/models", "available_bytes": 40 * 1024 ** 3},
                {"mount": "/scratch", "available_bytes": 900 * 1024 ** 3},
            ],
        )
        filtered = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot_data},
            min_weight_mount_avail_gb=64, now=1000,
        )
        self.assertEqual(filtered["candidates"], [])
        kept = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot_data},
            min_weight_mount_avail_gb=32, now=1000,
        )
        self.assertEqual(kept["candidates"][0]["MaxWeightMountAvailGb"], 40.0)

    def test_temperature_alone_is_unknown_but_explicit_throttle_is_excluded(self) -> None:
        hot = snapshot([device(0, phy_ids=(0, 1), temperature_c=96)])
        hot_result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": hot}, now=1000,
        )
        self.assertEqual(hot_result["candidates"][0]["ThermalHealth"], "unknown")

        throttled = snapshot([device(0, phy_ids=(0, 1), health="Throttled")])
        excluded = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": throttled}, now=1000,
        )
        self.assertEqual(excluded["candidates"], [])
        included = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": throttled},
            exclude_throttled=False, now=1000,
        )
        self.assertEqual(included["candidates"][0]["ThermalHealth"], "throttled")
        node_throttled = snapshot([device(0, phy_ids=(0, 1), temperature_c=96)], throttled=True)
        self.assertEqual(capacity_candidates(
            [server("server", "198.51.100.10")], {"server": node_throttled}, now=1000,
        )["candidates"], [])

    def test_health_ok_alone_is_not_healthy_without_throttle_evidence(self) -> None:
        health_only = snapshot([device(0, phy_ids=(0, 1), health="OK")])
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": health_only}, now=1000,
        )
        self.assertEqual(result["candidates"][0]["ThermalHealth"], "unknown")
        observed_flag = snapshot([device(0, phy_ids=(0, 1))], throttling_detected=False)
        flagged = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": observed_flag}, now=1000,
        )
        self.assertEqual(flagged["candidates"][0]["ThermalHealth"], "healthy")
        unknown_flag = snapshot([device(0, phy_ids=(0, 1))], thermal_health="unknown")
        unknown = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": unknown_flag}, now=1000,
        )
        self.assertEqual(unknown["candidates"][0]["ThermalHealth"], "unknown")

    def test_real_a2_and_a3_health_ok_without_throttle_evidence_is_unknown(self) -> None:
        for name in ("a2-173-125-1-2.txt", "a3-192-168-13-165-idle.txt"):
            with self.subTest(capture=name):
                result = capacity_candidates(
                    [server("server", "198.51.100.10")],
                    {"server": snapshot(real_devices(name))}, now=1000,
                )
                self.assertEqual(result["candidates"][0]["ThermalHealth"], "unknown")

    def test_actual_dcmi_idle_cause_is_explicit_no_throttle_evidence(self) -> None:
        devices = [real_devices("a2-173-125-1-2.txt")[0]]
        devices[0]["dcmi_freq_reduc"] = actual_dcmi_cause()
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot(devices)}, now=1000,
        )
        self.assertEqual(result["candidates"][0]["ThermalHealth"], "healthy")

    def test_hypothetical_dcmi_thermal_bit_overrides_health_ok(self) -> None:
        # Derived decoder case, not a hardware capture.
        devices = [real_devices("a2-173-125-1-2.txt")[0]]
        devices[0]["dcmi_freq_reduc"] = dcmi_telemetry.decode_cause_mask(1 << 2)
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot(devices)},
            exclude_throttled=False, now=1000,
        )
        self.assertEqual(result["candidates"][0]["ThermalHealth"], "throttled")

    def test_dcmi_unknown_bits_or_read_failure_stays_unknown(self) -> None:
        for record in (
            dcmi_telemetry.decode_cause_mask(1 << 7),
            dcmi_telemetry.decode_cause_mask(0, ret=-1),
        ):
            with self.subTest(state=record.get("state")):
                devices = real_devices("a2-173-125-1-2.txt")
                devices[0]["dcmi_freq_reduc"] = record
                result = capacity_candidates(
                    [server("server", "198.51.100.10")],
                    {"server": snapshot(devices)}, now=1000,
                )
                self.assertEqual(result["candidates"][0]["ThermalHealth"], "unknown")

    def test_reported_throttle_overrides_frequency_read_failure(self) -> None:
        devices = [real_devices("a2-173-125-1-2.txt")[0]]
        devices[0].update({"health": "Throttled", "frequency_exit_code": 1})
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot(devices)},
            exclude_throttled=False, now=1000,
        )
        self.assertEqual(result["candidates"][0]["ThermalHealth"], "throttled")

    def test_real_measured_frequency_below_rated_without_threshold_is_unknown(self) -> None:
        devices = real_devices("a2-173-125-1-2.txt")
        common = npu_smi.parse_npu_common(
            (Path(__file__).parent / "fixtures" / "a2-common-card0.txt").read_text()
        )[0]
        devices[0].update({"freq_mhz": common["freq_mhz"], "freq_max_mhz": common["freq_max_mhz"]})
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot(devices)}, now=1000,
        )
        self.assertEqual(result["candidates"][0]["ThermalHealth"], "unknown")

    def test_hypothetical_measured_equal_to_rated_is_healthy_evidence(self) -> None:
        devices = [real_devices("a2-173-125-1-2.txt")[0]]
        devices[0].update({"freq_mhz": 1650, "freq_max_mhz": 1650})
        result = capacity_candidates(
            [server("server", "198.51.100.10")], {"server": snapshot(devices)}, now=1000,
        )
        self.assertEqual(result["candidates"][0]["ThermalHealth"], "healthy")

    def test_prefer_same_package_ranks_pairs_before_higher_idle_count(self) -> None:
        paired = snapshot([device(0, phy_ids=(0, 1))])
        unpaired = snapshot([device(0, phy_ids=(2, 3), busy=True)])
        unpaired["summary"]["npu_count"] = 4
        unpaired["summary"]["busy_npu_count"] = 1
        servers = {
            "paired": server("paired", "198.51.100.10"),
            "unpaired": server("unpaired", "198.51.100.11"),
        }
        snapshots = {"paired": paired, "unpaired": unpaired}
        preferred = capacity_candidates(list(servers.values()), snapshots, now=1000)
        self.assertEqual([row["id"] for row in preferred["candidates"]], ["paired", "unpaired"])
        legacy = capacity_candidates(
            list(servers.values()), snapshots, prefer_same_package=False, now=1000,
        )
        self.assertEqual([row["id"] for row in legacy["candidates"]], ["unpaired", "paired"])

    def test_negative_mount_requirement_is_rejected(self) -> None:
        with self.assertRaises(AgentQueryError):
            capacity_candidates([], {}, min_weight_mount_avail_gb=-1)

    def test_cli_forwards_new_keyword_only_capacity_arguments(self) -> None:
        calls = []

        class FakeClient:
            def __init__(self, url=None):
                pass

            def capacity(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {"candidates": []}

        output = io.StringIO()
        with mock.patch.object(CLI, "VawsTopClient", FakeClient), redirect_stdout(output):
            code = CLI.main([
                "--json", "capacity", "--min-weight-mount-avail-gb", "64",
                "--no-prefer-same-package", "--include-throttled",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"candidates": []})
        self.assertEqual(calls[0][0], (1, 300, [], False))
        self.assertEqual(calls[0][1], {
            "min_weight_mount_avail_gb": 64.0,
            "prefer_same_package": False,
            "exclude_throttled": False,
        })


if __name__ == "__main__":
    unittest.main()
