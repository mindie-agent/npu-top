"""A distributable wheel contains complete assets for these exact inputs."""
from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def build(tmp_path):
    pytest.importorskip("hatchling")
    source = Path(__file__).resolve().parents[1] / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("monitor_build_hook", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json"):
        (tmp_path / name).write_text("synthetic source")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "dashboard.tsx").write_text("current source")
    static = tmp_path / "npu_top" / "static"
    static.mkdir(parents=True)
    return module, tmp_path, static


def test_missing_npm_cannot_produce_incomplete_wheel(build, monkeypatch):
    module, root, _ = build
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    data = {"force_include": {}}
    with pytest.raises(RuntimeError, match="requires frontend assets"):
        module.CustomBuildHook.initialize(SimpleNamespace(target_name="wheel", root=root), "1", data)
    assert not data["force_include"]


@pytest.mark.parametrize("damage", ["unchanged", "changed-source", "missing-asset", "changed-asset"])
def test_wheel_reuses_only_matching_complete_assets(build, monkeypatch, damage):
    module, root, static = build
    (static / "index.html").write_text('<script type="module" src="/entry.js"></script>')
    (static / "entry.js").write_text("current bundle")
    monkeypatch.setattr(module.shutil, "which", lambda _: "npm")
    monkeypatch.setattr(module.subprocess, "check_call", lambda *_a, **_k: None)
    hook = SimpleNamespace(target_name="wheel", root=root)
    module.CustomBuildHook.initialize(hook, "1", {"force_include": {}})
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    if damage == "changed-source":
        (root / "app" / "dashboard.tsx").write_text("new source")
    elif damage == "missing-asset":
        (static / "entry.js").unlink()
    elif damage == "changed-asset":
        (static / "entry.js").write_text("corrupt bundle")
    data = {"force_include": {}}
    if damage == "unchanged":
        module.CustomBuildHook.initialize(hook, "1", data)
        assert str(static) in data["force_include"]
    else:
        with pytest.raises(RuntimeError, match="requires frontend assets"):
            module.CustomBuildHook.initialize(hook, "1", data)
        assert not data["force_include"]
