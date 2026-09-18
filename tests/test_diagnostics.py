"""Real local collector threads and HTTP; no SSH/NPU or monitor services."""
import http.client
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from mindie_diagnostics import configure
from npu_top.scheduler import AdaptiveScheduler


def records(root):
    return [json.loads(line) for path in root.glob("events/*/*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def diagnostic_root(tmp_path, monkeypatch):
    root = tmp_path / "diagnostics"
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(root))
    rec = configure("npu-top", root=root, level="DEBUG")
    yield root
    rec.close()


def settings():
    return SimpleNamespace(idle_interval=120, history_interval=30, infrastructure_interval=60,
                           retention_days=90, max_workers=2)


class DB:
    def close(self):
        pass
    def latest_persisted(self):
        return {}
    def list_servers(self):
        return []
    def prune(self, days):
        raise OSError("simulated database failure")


def test_collector_death_notifies_waiters_and_health_is_degraded(diagnostic_root):
    from npu_top.api import App, AppServer
    scheduler = AdaptiveScheduler(settings(), DB(), object())
    scheduler.start()
    scheduler._thread.join(3)
    assert not scheduler._thread.is_alive()
    state = scheduler.runtime_state()
    assert state["collector_status"] == "failed" and not state["collector_alive"]
    assert state["collector_error_type"] == "OSError"
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="collector failed"):
        scheduler.collect_and_wait("missing", timeout=30)
    assert time.monotonic() - started < 1
    server = AppServer(("127.0.0.1", 0), App(settings(), DB(), object(), scheduler))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        client.request("GET", "/api/health")
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503 and payload["status"] == "degraded"
        client.close()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
        scheduler.stop()
    end = [row for row in records(diagnostic_root) if row.get("operation") == "top.collector"
           and row["event"] == "operation.end"][-1]
    assert end["status"] == "error" and end["attributes"]["stack_frames"]
    http_end = [row for row in records(diagnostic_root) if row.get("operation") == "top.http.get"
                and row["event"] == "operation.end"][-1]
    assert http_end["status"] == "error"
    assert http_end["attributes"]["error_code"] == 503


def test_failed_probe_records_actual_worker_duration(diagnostic_root):
    class ProbeDB(DB):
        def list_servers(self):
            return [{"id": "sample", "enabled": True}]
        def record_failure(self, ident, error, duration, persisted):
            self.duration = duration
    class Probe:
        def collect(self, server, include):
            time.sleep(0.025)
            raise TimeoutError("probe timed out")
    db = ProbeDB()
    scheduler = AdaptiveScheduler(settings(), db, Probe())
    scheduler._collect_cycle(set())
    snapshot = scheduler.snapshots()["sample"]
    assert db.duration >= 20
    assert snapshot["probe_duration_ms"] == db.duration
    assert snapshot["status"] == "offline"
    event = [row for row in records(diagnostic_root) if row["event"] == "operation.end"][-1]
    assert event["status"] == "error" and event["attributes"]["error_type"] == "TimeoutError"


def test_mcp_caught_fault_keeps_protocol_and_diagnostic(diagnostic_root):
    from npu_top.mcp import handle_request
    class Client:
        def servers(self):
            raise OSError("API down")
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "list_npu_servers"}}, Client())
    assert response["error"]["code"] == -32603
    event = records(diagnostic_root)[-1]
    assert event["status"] == "error" and event["attributes"]["error_type"] == "OSError"


def test_public_cli_local_help_and_diagnostics(diagnostic_root, capsys):
    from npu_top.cli import main
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    with pytest.raises(SystemExit) as result:
        main(["diagnostics", "--help"])
    assert result.value.code == 0
    assert "diagnostic" in capsys.readouterr().out.lower()
    assert all(row.get("status") != "error" for row in records(diagnostic_root))


def test_diagnostics_with_global_flags_does_not_contact_monitor(diagnostic_root, monkeypatch, capsys):
    from npu_top import cli
    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic export must not initialize a monitor client")
    monkeypatch.setattr(cli, "VawsTopClient", forbidden)
    assert cli.main(["--json", "diagnostics", "bundle", "--root", str(diagnostic_root / "empty")]) == 0
    assert json.loads(capsys.readouterr().out)["events"] == []


def test_only_actual_argparse_errors_are_classified_caller(diagnostic_root):
    from npu_top.cli import main
    from npu_top.observability import observed
    with pytest.raises(SystemExit) as caught:
        main(["unknown-command"])
    assert caught.value.code == 2
    assert records(diagnostic_root)[-1]["attributes"]["classification"] == "caller"

    @observed("top.fixture.business")
    def business():
        raise SystemExit(2)
    with pytest.raises(SystemExit):
        business()
    assert records(diagnostic_root)[-1]["attributes"].get("classification") != "caller"


@pytest.mark.parametrize("classification, error_code", [("caller", -32602), ("unknown", 503)])
def test_public_bundle_retains_failure_classification_and_numeric_code(
    diagnostic_root, tmp_path, classification, error_code
):
    from mindie_diagnostics import get_recorder

    with get_recorder("npu-top").operation("projection.fixture") as operation:
        operation.fail("argument_validation", classification=classification, error_code=error_code)
    output = tmp_path / "public-bundle.json"
    proc = subprocess.run(
        [sys.executable, "-m", "npu_top", "diagnostics", "bundle",
         "--root", str(diagnostic_root), "--operation-id", operation.summary()["operation_id"],
         "--output", str(output)],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    bundle = json.loads(proc.stdout)
    assert json.loads(output.read_text(encoding="utf-8")) == bundle
    ended = [event for event in bundle["events"] if event["event"] == "operation.end"]
    assert len(ended) == 1
    assert ended[0]["severity"] == "ERROR"
    assert ended[0]["status"] == "error"
    assert ended[0]["attributes"]["category"] == "argument_validation"
    assert ended[0]["attributes"]["classification"] == classification
    assert ended[0]["attributes"]["error_code"] == error_code
