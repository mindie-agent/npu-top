"""Never send intentional test failures to the user's diagnostic worker."""
import os

import pytest


_ISOLATED_ENV = (
    "MINDIE_DIAGNOSTICS_ROOT",
    "MINDIE_DIAGNOSTICS_CONFIG",
    "MINDIE_TOP_STATE_DIR",
)


@pytest.fixture(scope="session", autouse=True)
def isolated_diagnostics(tmp_path_factory):
    from mindie_diagnostics import configure

    root = tmp_path_factory.mktemp("top-diagnostics")
    state = root / "state"
    state.mkdir()
    previous = {name: os.environ.get(name) for name in _ISOLATED_ENV}
    os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(root)
    os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(root / "diagnostics.json")
    os.environ["MINDIE_TOP_STATE_DIR"] = str(state)
    recorder = configure("npu-top", root=root)
    yield root
    recorder.close()
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

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
