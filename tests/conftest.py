"""Session-wide host sandbox isolation and standing Ascend NPU fixtures for `npu-top` tests."""
from __future__ import annotations

import os

import pytest

_ISOLATED_ENV = (
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "MINDIE_CONFIG_HOME",
    "MINDIE_DATA_HOME",
    "MINDIE_DIAGNOSTICS_ROOT",
    "MINDIE_DIAGNOSTICS_CONFIG",
    "MINDIE_TOP_STATE_DIR",
    "NFM_STATE_DIR",
    "REMOTE_DEV_AUTH_DIR",
    "REMOTE_DEV_CREDENTIAL_FILE",
    "REMOTE_DEV_SSH_MUX",
    "UV_CACHE_DIR",
    "TMPDIR",
    "TEMP",
    "TMP",
)


@pytest.fixture(scope="session", autouse=True)
def isolated_diagnostics(tmp_path_factory):
    from mindie_diagnostics import configure
    from remote_dev.core import ssh_transport

    root = tmp_path_factory.mktemp("top-isolated")
    home = root / "home"
    state = root / "state"
    tmp_dir = root / "tmp"
    for d in (home, state, tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    previous = {name: os.environ.get(name) for name in _ISOLATED_ENV}
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ["APPDATA"] = str(home / "AppData" / "Roaming")
    os.environ["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
    os.environ["XDG_DATA_HOME"] = str(home / ".local" / "share")
    os.environ["XDG_STATE_HOME"] = str(home / ".local" / "state")
    os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
    os.environ["MINDIE_CONFIG_HOME"] = str(root / "mindie-config")
    os.environ["MINDIE_DATA_HOME"] = str(root / "mindie-data")
    os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(root / "diag")
    os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(root / "diag" / "diagnostics.json")
    os.environ["MINDIE_TOP_STATE_DIR"] = str(state)
    os.environ.pop("NFM_STATE_DIR", None)
    os.environ["REMOTE_DEV_AUTH_DIR"] = str(root / "ssh-auth")
    os.environ["REMOTE_DEV_CREDENTIAL_FILE"] = str(root / "ssh-credentials.json")
    os.environ.pop("REMOTE_DEV_SSH_MUX", None)
    os.environ["UV_CACHE_DIR"] = str(root / "uv-cache")
    os.environ["TMPDIR"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ["TMP"] = str(tmp_dir)
    previous_mux = ssh_transport._MUX_DIR, ssh_transport._MUX_READY
    ssh_transport._MUX_DIR, ssh_transport._MUX_READY = root / "ssh-mux", None
    recorder = configure("npu-top", root=root / "diag")
    yield root
    recorder.close()
    ssh_transport._MUX_DIR, ssh_transport._MUX_READY = previous_mux
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="session")
def standing_npu_snapshots(tmp_path_factory):
    """Probe each standing host once; independent read-only hosts run in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from npu_top import npu_smi
    from npu_top.probe import HostProbe

    class ReadOnlySshAdapter:
        project_root = tmp_path_factory.mktemp("live-probe")

        def preflight(self, server):
            return {"ok": True, "category": "ssh_config_valid", "error": None}

        def launch(self, server, command, *, timeout_ms):
            argv = [
                "ssh", "-T", "-o", "ControlMaster=no", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5", "-p", str(server["port"]),
                f"{server['username']}@{server['host']}",
            ]
            return SimpleNamespace(argv=[*argv, *command], env=None)

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
