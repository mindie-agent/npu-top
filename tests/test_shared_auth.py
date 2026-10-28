"""Monitor access reuses remote-dev profiles without a second credential store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from remote_dev.auth import CredentialStore, ProfileStore
from remote_dev.auth import askpass
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.ssh_transport import RemoteBytesCompleted, SshLaunch
from npu_top.db import Database
from npu_top.device_adapter import DeviceAdapter
from npu_top.probe import HostProbe
from npu_top.serve import import_inventory
from npu_top.ssh_access import SshAccess
from test_console_routes import StubAdapter, _json, console


SERVER = {"id": "shared", "name": "shared", "host": "192.0.2.19", "port": 2222, "username": "ops"}


@pytest.fixture
def shared_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("REMOTE_DEV_CREDENTIAL_FILE", str(tmp_path / "credentials.json"))
    endpoint = Endpoint(host=SERVER["host"], port=SERVER["port"], user=SERVER["username"])
    profiles = ProfileStore()
    credential_ref = CredentialStore().put("isolated-test-password")
    profile = {
        "schema_version": 1, "auth_ref": profiles.reference(endpoint), "target": profiles.target(endpoint),
        "auth_method": "password", "identity_file": None, "credential_ref": credential_ref,
        "revision": "test-profile", "bootstrap": {"status": "not_requested", "key_verified": False},
    }
    profiles.save(profile)
    return {**SERVER, "auth_ref": profile["auth_ref"]}, profile


def test_new_monitor_process_uses_shared_password_reference(tmp_path, shared_profile):
    server, profile = shared_profile
    plan = SshAccess(tmp_path, tmp_path).launch(server, ["true"], timeout_ms=1000)
    assert plan.env["REMOTE_DEV_ASKPASS_REF"] == profile["credential_ref"]
    assert "isolated-test-password" not in json.dumps(plan.argv)
    assert "isolated-test-password" not in json.dumps(plan.env)
    result = subprocess.run(
        [sys.executable, "-I", str(Path(askpass.__file__)), "Password:"],
        env=plan.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=True,
    )
    assert result.stdout == b"isolated-test-password\n"


def test_monitor_reuses_publickey_connections_but_password_stays_independent(tmp_path, shared_profile):
    server, profile = shared_profile
    access = SshAccess(tmp_path, tmp_path)
    password_plan = access.launch(server, ["true"], timeout_ms=1000)
    assert "ControlMaster=no" in password_plan.argv
    assert "ControlPath=none" in password_plan.argv
    profile.update(auth_method="publickey", credential_ref=None)
    ProfileStore().save(profile)
    first = access.launch(server, ["true"], timeout_ms=1000)
    first_path = next(value for value in first.argv if value.startswith("ControlPath="))
    profile["revision"] = "rotated-profile"
    ProfileStore().save(profile)
    rotated = access.launch(server, ["true"], timeout_ms=1000)
    rotated_path = next(value for value in rotated.argv if value.startswith("ControlPath="))
    if os.name == "nt":
        assert first_path == rotated_path == "ControlPath=none"
    else:
        assert "ControlMaster=auto" in first.argv
        assert first_path != rotated_path


def test_monitor_can_use_saved_password_without_repairing_a_publickey_profile(tmp_path, shared_profile, monkeypatch):
    server, profile = shared_profile
    profile["auth_method"] = "publickey"
    ProfileStore().save(profile)
    path = ProfileStore.path(profile["auth_ref"])
    before = path.read_bytes()
    monkeypatch.setattr(
        "remote_dev.auth.access._probe",
        lambda *_args, **_kwargs: pytest.fail("monitor plans must not probe or repair authentication"),
    )
    plan = SshAccess(tmp_path, tmp_path).launch(server, ["true"], timeout_ms=1000)
    assert "PreferredAuthentications=publickey,password,keyboard-interactive" in plan.argv
    assert "ControlPath=none" in plan.argv
    assert plan.env["REMOTE_DEV_ASKPASS_REF"] == profile["credential_ref"]
    assert path.read_bytes() == before


def test_missing_shared_reference_is_not_recreated_or_bootstrapped(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    server = {**SERVER, "auth_ref": "ssh-" + "0" * 24}
    result = SshAccess(tmp_path, tmp_path).connect(server)
    assert not result["ok"] and result["category"] == "credential_unavailable"
    assert result["state"] == "not_ready"
    assert not (tmp_path / "auth").exists()
    assert not (tmp_path / "keys").exists()


def test_existing_monitor_host_trust_stays_on_the_shared_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    trust = tmp_path / "known_hosts"
    trust.write_text("# existing monitor host trust\n", encoding="utf-8")
    plan = SshAccess(tmp_path, tmp_path).launch(SERVER, ["true"], timeout_ms=1000)
    assert f"UserKnownHostsFile={trust}" in plan.argv
    assert trust.read_text(encoding="utf-8") == "# existing monitor host trust\n"
    assert not (tmp_path / "keys").exists()


@pytest.mark.parametrize("explicit_ref", [True, False])
def test_shared_profile_supersedes_legacy_monitor_identity_and_trust(tmp_path, shared_profile, explicit_ref):
    server, profile = shared_profile
    if not explicit_ref:
        server = {key: value for key, value in server.items() if key != "auth_ref"}
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("unused legacy monitor identity\n", encoding="utf-8")
    trust = tmp_path / "known_hosts"
    trust.write_text("# existing legacy monitor trust\n", encoding="utf-8")
    access = SshAccess(tmp_path, tmp_path)
    endpoint = access.endpoint(server)
    assert endpoint.identity_file is None and endpoint.known_hosts_file is None
    plan = access.launch(server, ["true"], timeout_ms=1000)
    assert plan.env["REMOTE_DEV_ASKPASS_REF"] == profile["credential_ref"]
    assert str(key) not in plan.argv
    assert f"UserKnownHostsFile={trust}" not in plan.argv
    assert key.read_text() == "unused legacy monitor identity\n"
    assert trust.read_text() == "# existing legacy monitor trust\n"


def test_probe_forwards_shared_launch_environment_without_auth_repair(tmp_path, shared_profile, monkeypatch):
    server, profile = shared_profile
    adapter = DeviceAdapter(SshAccess(tmp_path, tmp_path))
    captured = []

    def run(plan, **kwargs):
        captured.append((plan, kwargs))
        return RemoteBytesCompleted(plan.argv, 0, b"probe\n", b"")

    monkeypatch.setattr("npu_top.ssh_access.capture_launch", run)
    result = HostProbe(adapter, timeout=20)._run_script(server, "printf probe\n", 20)
    plan, options = captured[0]
    assert plan.env["REMOTE_DEV_ASKPASS_REF"] == profile["credential_ref"]
    assert options["input"] == b"printf probe\n"
    assert "BatchMode=no" in plan.argv
    assert result.stdout == "probe\n"


def test_probe_returns_completed_output_with_owned_cleanup_failure(tmp_path, monkeypatch):
    from remote_dev.core.local_process import OwnedProcess

    original_stop = OwnedProcess.stop

    def fail_cleanup(self, **kwargs):
        original_stop(self, **kwargs)
        raise OSError("isolated cleanup failure")

    monkeypatch.setattr(OwnedProcess, "stop", fail_cleanup)
    adapter = SimpleNamespace(launch=lambda *_args, **_kwargs: SshLaunch(
        [sys.executable, "-c", "print('completed probe')"], None, ("isolated-test",),
    ))
    result = HostProbe(adapter, timeout=1)._run_script(SERVER, "unused", 1)
    assert result.returncode == 0 and result.stdout == "completed probe\n"
    assert "isolated cleanup failure" in result.cleanup_error


def test_probe_capture_uses_only_the_budget_remaining_after_preparation(monkeypatch):
    from npu_top.ssh_access import capture_ssh

    moments = iter((10.0, 10.75))
    monkeypatch.setattr("npu_top.ssh_access.time", SimpleNamespace(monotonic=lambda: next(moments)))
    seen = []

    def capture(plan, **kwargs):
        seen.append(kwargs["timeout_ms"])
        return RemoteBytesCompleted(plan.argv, 0, b"done", b"")

    monkeypatch.setattr("npu_top.ssh_access.capture_launch", capture)
    adapter = SimpleNamespace(launch=lambda *_args, **_kwargs: SshLaunch(["ssh"], None, ("isolated-test",)))
    assert capture_ssh(adapter, SERVER, ["true"], timeout_ms=1000).stdout == "done"
    assert seen == [250]


def test_registered_reference_survives_database_reopen_and_metadata_update(tmp_path, shared_profile):
    server, _ = shared_profile
    db = Database(tmp_path / "monitor.sqlite3")
    db.initialize()
    db.upsert_server(server)
    db.close()
    db = Database(tmp_path / "monitor.sqlite3")
    db.initialize()
    updated = db.upsert_server({**SERVER, "name": "renamed"})
    assert updated["auth_ref"] == server["auth_ref"]
    assert "password" not in json.dumps(db.list_servers())
    db.close()


def test_older_server_schema_migrates_without_losing_host(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE servers (id TEXT PRIMARY KEY, name TEXT, host TEXT, port INTEGER, username TEXT, "
            "tags_json TEXT, enabled INTEGER, created_at INTEGER, updated_at INTEGER, last_seen_at INTEGER, "
            "last_error TEXT, bootstrap_receipt_json TEXT, UNIQUE(host,port,username))"
        )
        connection.execute(
            "INSERT INTO servers VALUES ('old','old','192.0.2.18',22,'ops','[]',1,1,1,NULL,NULL,NULL)"
        )
    db = Database(path)
    db.initialize()
    assert db.get_server("old")["host"] == "192.0.2.18"
    db.update_server("old", auth_ref="ssh-" + "1" * 24)
    assert db.get_server("old")["auth_ref"] == "ssh-" + "1" * 24
    db.close()


def test_batch_keeps_shared_reference_and_imports_password_once(monkeypatch):
    calls = []
    reference = "ssh-" + "2" * 24

    def connect(self, server, *, password=None):
        calls.append((dict(server), password))
        return {"ok": True, "auth_ref": reference, "auth_method": "password"}

    monkeypatch.setattr(StubAdapter, "connect", connect)
    with console(seed_server=False) as (db, host, port):
        status, _, payload = _json(host, port, "POST", "/api/servers/batch", {
            "servers": [SERVER], "passwords": ["import-once"],
        })
        assert status == 207 and payload["results"][0]["auth"]["ok"]
        assert db.list_servers()[0]["auth_ref"] == reference
        assert "import-once" not in json.dumps(payload)
    assert len(calls) == 1 and calls[0][1] == "import-once"


def test_batch_unknown_reference_registers_host_but_reports_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    access = SshAccess(tmp_path, tmp_path)
    monkeypatch.setattr(StubAdapter, "connect", lambda self, server, **kw: access.connect(server, **kw))
    server = {**SERVER, "auth_ref": "ssh-" + "4" * 24}
    with console(seed_server=False) as (db, host, port):
        _, _, payload = _json(host, port, "POST", "/api/servers/batch", {"servers": [server]})
        receipt = payload["results"][0]
        assert receipt["registration"] == "completed"
        assert receipt["auth"]["state"] == "not_ready"
        assert receipt["auth"]["category"] == "credential_unavailable"
        assert db.list_servers()[0]["auth_ref"] == server["auth_ref"]
    assert not (tmp_path / "auth").exists()


def test_shared_bootstrap_uncertainty_is_preserved(tmp_path, monkeypatch):
    error = RemoteExecutionError("connection lost", submission_state="uncertain")
    error.bootstrap = {"status": "unknown", "installation": "unknown", "key_verified": False}

    def setup(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("npu_top.ssh_access.auth.setup_access", setup)
    result = SshAccess(tmp_path, tmp_path).connect(SERVER, password="one-time")
    assert result["state"] == "unknown" and result["submission_state"] == "uncertain"
    assert result["bootstrap"] == error.bootstrap


def test_reference_record_failure_preserves_successful_shared_setup(monkeypatch):
    reference = "ssh-" + "5" * 24
    auth = {"ok": True, "auth_ref": reference, "auth_method": "password"}
    monkeypatch.setattr(StubAdapter, "connect", lambda *_a, **_kw: auth)
    with console(seed_server=False) as (db, host, port):
        def fail(*_args, **_kwargs):
            raise OSError("local storage failed")

        monkeypatch.setattr(db, "update_server", fail)
        _, _, payload = _json(host, port, "POST", "/api/servers/batch", {"servers": [SERVER]})
        receipt = payload["results"][0]
        assert receipt["auth"] == auth and receipt["registration"] == "completed"
        assert receipt["failure"]["stage"] == "record_auth_ref"


def test_inventory_updates_reference_before_access_check(tmp_path):
    db = Database(tmp_path / "monitor.sqlite3")
    db.initialize()
    db.upsert_server(SERVER)
    seen = []
    reference = "ssh-" + "3" * 24
    adapter = SimpleNamespace(connect=lambda server: seen.append(server["auth_ref"]) or {"ok": True})
    app = SimpleNamespace(db=db, adapter=adapter, scheduler=SimpleNamespace(collect_now=lambda _: None))
    result = import_inventory(app, [{**SERVER, "auth_ref": reference}])
    assert result["state"] == "completed" and seen == [reference]
    assert db.get_server(SERVER["id"])["auth_ref"] == reference
    db.close()
