"""Never send intentional test failures to the user's diagnostic worker."""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_diagnostics(tmp_path_factory):
    from mindie_diagnostics import configure

    root = tmp_path_factory.mktemp("top-diagnostics")
    previous = os.environ.get("MINDIE_DIAGNOSTICS_ROOT")
    os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(root)
    recorder = configure("npu-top", root=root)
    yield root
    recorder.close()
    if previous is None:
        os.environ.pop("MINDIE_DIAGNOSTICS_ROOT", None)
    else:
        os.environ["MINDIE_DIAGNOSTICS_ROOT"] = previous

@pytest.fixture(scope="session")
def standing_npu_snapshots(tmp_path_factory):
    """Probe each standing host once; independent read-only hosts run in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    from npu_top import npu_smi
    from npu_top.probe import HostProbe

    class ReadOnlySshAdapter:
        project_root = tmp_path_factory.mktemp("live-probe")

        def preflight(self, server):
            return {"ok": True, "category": "ssh_config_valid", "error": None}

        def ssh_base(self, server, *, batch_mode=True):
            return [
                "ssh", "-T", "-o", "ControlMaster=no",
                "-o", f"BatchMode={'yes' if batch_mode else 'no'}",
                "-o", "ConnectTimeout=5", "-p", str(server["port"]),
                f"{server['username']}@{server['host']}",
            ]

        def parse_npu(self, info, usages):
            return npu_smi.parse_npu(info, usages)

    def collect(host):
        server = {
            "id": host, "name": host, "host": host, "port": 22,
            "username": "root", "enabled": True,
            "tags": ["a2" if host == "173.125.1.2" else "a3"],
        }
        snapshot = HostProbe(ReadOnlySshAdapter(), timeout=20).collect(
            server, include_infrastructure=True
        )
        return host, (server, snapshot)

    hosts = ("173.125.1.2", "192.168.13.165", "192.168.13.154")
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        return dict(executor.map(collect, hosts))
