"""Tier 1 tests for `npu-top` Diagnostics 2.0 unhandled internal exception reporting."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindie_diagnostics import configure
from mindie_diagnostics.ingestion import ingest
from mindie_diagnostics.integration import configure_reporting
from mindie_diagnostics.outbox import Outbox
from npu_top.client import ClientError
from npu_top.observability import observed


def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    base = tmp_path.resolve()
    diag_root = base / "diag"
    cfg_file = base / "reporting.json"
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(diag_root))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_file))
    (base / "home").mkdir(parents=True, exist_ok=True)
    diag_root.mkdir(parents=True, exist_ok=True)
    configure("npu-top", root=diag_root)
    configure_reporting(True, roots=[str(diag_root)], config=cfg_file)
    return diag_root, cfg_file


def test_observed_reports_internal_exception_and_ignores_client_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diag_root, cfg_file = _isolate_env(tmp_path, monkeypatch)
    queue = Outbox(tmp_path.resolve() / "queue.db")

    @observed("top.cli")
    def expected_unavailable():
        raise ClientError("loopback down")

    with pytest.raises(ClientError):
        expected_unavailable()
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 0

    @observed("top.wrapped_perm")
    def wrapped_permission():
        try:
            raise PermissionError("denied")
        except PermissionError as err:
            raise RuntimeError("wrapper") from err

    with pytest.raises(RuntimeError):
        wrapped_permission()
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 0

    @observed("top.probe")
    def internal_bug():
        raise KeyError("private_host_metric")

    with pytest.raises(KeyError):
        internal_bug()
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] >= 1
    item = queue.claim()
    ev = item["payload"]["events"][-1]
    assert ev["component"] == "npu-top"
    assert ev["operation"] == "top.probe"
    assert "private_host_metric" not in json.dumps(item["payload"])
    assert all(not f["module"].startswith("npu_top.npu_top.") for f in ev["attributes"].get("stack_frames", []))

    from npu_top.observability import _is_internal_top_exception
    cur: BaseException = PermissionError("root")
    for idx in range(40):
        nxt = RuntimeError(f"w{idx}")
        nxt.__cause__ = cur
        cur = nxt
    assert _is_internal_top_exception(cur) is False


def test_nested_observed_causal_wrapper_emits_single_incident_and_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    diag_root, cfg_file = _isolate_env(tmp_path, monkeypatch)
    queue = Outbox(tmp_path.resolve() / "nested.db")

    @observed("top.inner")
    def inner_step():
        raise KeyError("secret_inner")

    @observed("top.outer")
    def outer_step():
        try:
            return inner_step()
        except KeyError as exc:
            raise RuntimeError("secret_outer") from exc

    with pytest.raises(RuntimeError):
        outer_step()
    stderr = capsys.readouterr().err
    assert stderr.count("mindie-diagnostic-incident:") == 1
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 1
    assert queue.claim() is not None and queue.claim() is None
