"""Regression tests for Windows CP936/long-path SQLite, wheel hook npm lookup, and WSL2 shadow exclusion."""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from npu_top import cli, db

_HATCH_SPEC = importlib.util.spec_from_file_location(
    "hatch_build", Path(__file__).resolve().parents[1] / "hatch_build.py"
)
assert _HATCH_SPEC is not None and _HATCH_SPEC.loader is not None
hatch_build = importlib.util.module_from_spec(_HATCH_SPEC)
_HATCH_SPEC.loader.exec_module(hatch_build)


@pytest.mark.unit
def test_hatch_build_windows_prefers_npm_cmd_with_create_no_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hatch_build, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        hatch_build.shutil,
        "which",
        lambda cmd, path=None: r"C:\Program Files\nodejs\npm.cmd" if cmd == "npm.cmd" else None,
    )
    assert hatch_build.resolve_npm(path=r"C:\Program Files\nodejs") == r"C:\Program Files\nodejs\npm.cmd"

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_check_call(cmd: list[str], **kwargs: object) -> int:
        calls.append((cmd, kwargs))
        return 0

    monkeypatch.setattr(hatch_build.subprocess, "check_call", fake_check_call)
    hook = object.__new__(hatch_build.CustomBuildHook)
    monkeypatch.setattr(hatch_build.CustomBuildHook, "target_name", property(lambda self: "wheel"), raising=False)
    monkeypatch.setattr(hatch_build.CustomBuildHook, "root", property(lambda self: str(tmp_path)), raising=False)
    editable_data: dict[str, list[str]] = {"artifacts": []}
    hook.initialize("editable", editable_data)
    assert calls == []

    build_data: dict[str, list[str]] = {"artifacts": []}
    hook.initialize("standard", build_data)

    assert len(calls) == 2
    assert calls[0][0][0] == r"C:\Program Files\nodejs\npm.cmd"
    assert calls[0][1]["creationflags"] == hatch_build.CREATE_NO_WINDOW
    assert calls[0][1]["stdin"] == subprocess.DEVNULL


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="POSIX/WSL2 DrvFS, PE, and PATH edge-case lookup")
def test_hatch_build_posix_skips_drvfs_pe_and_handles_path_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drv_dir = tmp_path / "mnt_c_node"
    pe_dir = tmp_path / "pe_node"
    lin_dir = tmp_path / "usr_bin"
    safe_dir = tmp_path / "ext4_dir"
    for d in (drv_dir / "sub", pe_dir, lin_dir, safe_dir):
        d.mkdir(parents=True, exist_ok=True)

    drv_npm = drv_dir / "npm"
    drv_npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    drv_npm.chmod(0o755)
    (safe_dir / "link").symlink_to(drv_dir / "sub")

    large_pe = pe_dir / "npm"
    buf = bytearray(0x2200)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = (0x2000).to_bytes(4, "little")
    buf[0x2000:0x2004] = b"PE\x00\x00"
    large_pe.write_bytes(bytes(buf))
    large_pe.chmod(0o755)

    lin_npm = lin_dir / "npm"
    lin_npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    lin_npm.chmod(0o755)

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"20 1 8:1 / / rw,relatime - ext4 /dev/sdb rw\n"
        f"45 20 0:40 / {drv_dir.resolve()} rw,noatime - 9p drvfs rw,aname=drvfs\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hatch_build, "_MOUNTINFO_PATH", str(mountinfo))

    assert hatch_build.is_wsl_drvfs(f"{safe_dir}/link/../npm") is True
    assert hatch_build.is_pe_executable(large_pe) is True
    assert hatch_build.resolve_npm(path=f"{safe_dir}/link/..:{pe_dir}:{lin_dir}") == str(lin_npm)
    assert hatch_build.resolve_npm(path=f"{drv_dir}:{pe_dir}") is None

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    cwd_npm = cwd_dir / "npm"
    cwd_npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cwd_npm.chmod(0o755)
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(hatch_build.os, "defpath", str(lin_dir))
    monkeypatch.setenv("PATH", "")
    assert hatch_build.resolve_npm() is None
    monkeypatch.setenv("PATH", f"{drv_dir}:.:{lin_dir}")
    dot_resolved = hatch_build.resolve_npm()
    assert dot_resolved == str(cwd_npm.resolve()) and os.path.isabs(dot_resolved)
    monkeypatch.delenv("PATH", raising=False)
    assert hatch_build.resolve_npm() == str(lin_npm)

    with mock.patch.object(Path, "stat", side_effect=TimeoutError("npm stat deadline")):
        with pytest.raises(TimeoutError, match="npm stat deadline"):
            hatch_build.resolve_npm(path=str(lin_dir))


@pytest.mark.unit
def test_utf8_stdio_reconfigures_non_utf8_text_wrapper_and_tolerates_none() -> None:
    raw_out = io.BytesIO()
    wrapper = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
    cli._ensure_utf8_stdio(None, io.StringIO(), wrapper)
    assert wrapper.encoding.lower().replace("_", "-") == "utf-8"
    wrapper.write("主机地址只允许域名、IPv4 或 IPv6 字符 — SSH 连接超时")
    wrapper.flush()
    assert "SSH 连接超时" in raw_out.getvalue().decode("utf-8")


@pytest.mark.integration
def test_database_long_path_and_win_extended_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deep_dir = tmp_path
    for idx in range(6):
        deep_dir = deep_dir / f"seg_{idx}_{'x' * 36}"
    db_path = deep_dir / "monitor.sqlite3"
    database = db.Database(db_path, max_bytes=32 * 1024 * 1024)
    database.initialize()
    try:
        server = database.upsert_server(
            {"id": "s1", "name": "测试节点", "host": "127.0.0.1", "port": 22, "username": "root", "tags": ["A3"]}
        )
        assert server["name"] == "测试节点"
    finally:
        database.close()

    monkeypatch.setattr(db, "_IS_WINDOWS", True)
    monkeypatch.setattr(db.os.path, "abspath", lambda p: str(p))
    long_nt = "C:\\" + "\\".join(["a" * 50] * 6) + "\\monitor.sqlite3"
    assert str(db._win_extended_fs_path(long_nt)).startswith("\\\\?\\C:\\")
    long_unc = "\\\\server\\share\\" + "\\".join(["b" * 50] * 6) + "\\monitor.sqlite3"
    assert str(db._win_extended_fs_path(long_unc)).startswith("\\\\?\\UNC\\server\\share\\")

    near_1032 = "C:\\" + ("d" * 1014) + "\\monitor.sqlite3"
    assert len(near_1032.encode("utf-8")) == 1033
    uri_target, use_uri = db._sqlite_connect_target(near_1032)
    assert use_uri is True
    assert str(uri_target).startswith("file:////%3F/C:/") and str(uri_target).endswith("?vfs=win32-longpath")
    unc_uri, unc_flag = db._sqlite_connect_target(long_unc)
    assert unc_flag is True and str(unc_uri).startswith("file:////%3F/UNC/server/share/") and "?vfs=win32-longpath" in str(unc_uri)

    monkeypatch.setattr(db, "_IS_WINDOWS", os.name == "nt")
    existing_dir = tmp_path / "stat_timeout_dir"
    existing_dir.mkdir()
    real_stat = Path.stat

    def timeout_on_existing_dir(self: Path, *args: object, **kwargs: object) -> object:
        if self == existing_dir:
            raise TimeoutError("directory stat deadline")
        return real_stat(self, *args, **kwargs)

    with (
        mock.patch("os.mkdir", side_effect=FileExistsError(17, "exists")),
        mock.patch.object(Path, "stat", timeout_on_existing_dir),
    ):
        with pytest.raises(TimeoutError, match="directory stat deadline"):
            db.Database(existing_dir / "monitor.sqlite3").connection()
