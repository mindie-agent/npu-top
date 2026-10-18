"""Verify failed background work visibility and non-replay of uncertain bootstrap writes."""
import contextlib
import http.client
import json
import sqlite3
import subprocess
import threading
from types import SimpleNamespace

import pytest

from npu_top.db import Database
from npu_top.device_adapter import DeviceAdapter
from npu_top.inventory import ExternalKeyBootstrap
from npu_top.probe import HostProbe, attach_process_details
from npu_top.serve import import_inventory, run_inventory_import
from npu_top.ssh_access import KeyInstallResult, SshAccess
from test_console_routes import _json, console

SERVER = {"id": "one", "name": "one", "host": "192.0.2.1", "port": 22, "username": "root"}


def initialized(path):
    db = Database(path)
    db.initialize()
    server = db.upsert_server(SERVER)
    return db, server


def adapter_for(db, tmp_path, monkeypatch, *, install, auth=False):
    ssh = SshAccess(tmp_path, tmp_path)
    monkeypatch.setattr(ssh, "preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(ssh, "check_key_auth", lambda *_: (auth, None))
    monkeypatch.setattr(ssh, "install_key_with_default_identity", install)
    return DeviceAdapter(ssh, bootstrap_state=db)


def test_background_inventory_failure_reaches_http_health_and_normal_read():
    from npu_top.api import App, AppServer

    app = App(
        SimpleNamespace(),
        SimpleNamespace(list_servers=lambda: [], close=lambda: None),
        object(),
        SimpleNamespace(runtime_state=lambda: {"collector_status": "running"}, snapshots=lambda: {}),
    )

    def fail():
        raise OSError("synthetic import failure")

    run_inventory_import(app, fail)
    assert app.agent_servers()["runtime"]["inventory"]["state"] == "failed"
    server = AppServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        client.request("GET", "/api/health")
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503
        assert payload["runtime"]["inventory"]["error_type"] == "OSError"
        client.close()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_failed_process_detail_probe_has_distinct_evidence(monkeypatch):
    probe = HostProbe(object(), 12)
    monkeypatch.setattr(probe, "_run_script", lambda *_: subprocess.CompletedProcess([], 255, "", "channel lost"))
    details, status = probe._collect_process_details({"id": "one"}, [4])
    assert details == {} and status["state"] == "unavailable" and status["exit_code"] == 255
    devices = [{"processes": [{"pid": 4, "npu_memory_mb": 42}]}]
    attach_process_details(devices, details, {})
    assert devices[0]["processes"][0]["npu_memory_mb"] == 42
    assert devices[0]["processes"][0]["command"] is None


def test_completed_probe_survives_record_failure_without_advancing_history():
    from npu_top.scheduler import AdaptiveScheduler

    settings = SimpleNamespace(
        idle_interval=120, history_interval=30, infrastructure_interval=60, retention_days=90, max_workers=1
    )

    def fail(*args):
        raise OSError("synthetic disk error")

    db = SimpleNamespace(
        latest_persisted=lambda: {},
        close=lambda: None,
        list_servers=lambda: [{"id": "one", "enabled": True}],
        record_success=fail,
    )
    probe = SimpleNamespace(collect=lambda *_: {"server_id": "one", "collected_at": 100, "summary": {"npu_count": 8}})
    scheduler = AdaptiveScheduler(settings, db, probe)
    scheduler._collect_cycle(set())
    snapshot = scheduler.snapshots()["one"]
    assert snapshot["status"] == "online" and snapshot["summary"]["npu_count"] == 8
    assert snapshot["recording"]["state"] == "failed" and snapshot["recording"]["diagnostic_ref"]
    assert "one" not in scheduler._latest_persisted
    assert scheduler.runtime_state()["storage_failures"] == 1


@pytest.mark.parametrize("outcome", ["unknown", "completed"])
def test_bootstrap_never_replays_unknown_or_completed_write(tmp_path, monkeypatch, outcome):
    ssh = SshAccess(tmp_path, tmp_path)
    monkeypatch.setattr(ssh, "preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(ssh, "check_key_auth", lambda *_: (False, None))
    monkeypatch.setattr(
        ssh, "install_key_with_default_identity", lambda *_: KeyInstallResult("not_started", authentication_rejected=True)
    )
    calls = []

    def install(*args):
        calls.append(args)
        return KeyInstallResult(outcome, "synthetic outcome")

    db, server = initialized(tmp_path / "monitor.sqlite3")
    adapter = DeviceAdapter(ssh, key_bootstrap=SimpleNamespace(run=install), bootstrap_state=db)
    result = adapter.bootstrap_with_passwords(server, ["first", "second"])
    db.close()
    assert len(calls) == 1
    assert result["installation"] == outcome
    assert result["state"] == ("unknown" if outcome == "unknown" else "verification_failed")
    assert not result["ok"]


def test_external_bootstrap_nonzero_is_uncertain_except_authentication_rejection(tmp_path, monkeypatch):
    bootstrap = ExternalKeyBootstrap("synthetic-command")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "failed"))
    assert bootstrap.run({"host": "x", "port": 22, "username": "u"}, tmp_path / "public", "one-time").state == "unknown"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 77, "", "denied"))
    result = bootstrap.run({"host": "x", "port": 22, "username": "u"}, tmp_path / "public", "one-time")
    assert result.state == "not_started" and result.authentication_rejected


def test_password_echo_is_not_saved_in_error_receipt(tmp_path, monkeypatch):
    password = "unique-one-time-password"
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "failed: " + password)
    )
    result = ExternalKeyBootstrap("synthetic").run(SERVER, tmp_path / "public", password)
    assert password not in result.error and "[REDACTED]" in result.error


def test_later_batch_record_failure_does_not_replace_completed_key_receipt(monkeypatch):
    from test_console_routes import StubAdapter

    auth = {"ok": False, "installation": "completed", "state": "verification_failed", "error": "login not verified"}
    monkeypatch.setattr(StubAdapter, "bootstrap_with_passwords", lambda *_: auth)
    with console(seed_server=False) as (db, host, port):
        monkeypatch.setattr(db, "record_failure", lambda *_: (_ for _ in ()).throw(OSError("record failure")))
        status, _, result = _json(host, port, "POST", "/api/servers/batch", {"servers": [SERVER], "passwords": []})
        assert status == 207
        receipt = result["results"][0]
        assert receipt["registration"] == "completed" and receipt["auth"] == auth
        assert receipt["failure"]["stage"] == "record_auth_failure"
