from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


MANIFEST = ".npu-top-assets.json"


def source_fingerprint(root: Path) -> str:
    paths = sorted([path for path in (root / "app").rglob("*") if path.is_file()] +
                   [root / name for name in ("index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")])
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def asset_hashes(static: Path) -> dict[str, str]:
    return {path.relative_to(static).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(static.rglob("*")) if path.is_file() and path.name != MANIFEST}


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        source = attributes.get("src") if tag == "script" else attributes.get("href") if tag == "link" else None
        if source:
            self.paths.append(source)


def require_assets(static: Path) -> None:
    index = static / "index.html"
    if not index.is_file():
        raise RuntimeError("frontend build did not produce npu_top/static/index.html")
    links = AssetLinks()
    links.feed(index.read_text(encoding="utf-8"))
    if not any(path.endswith(".js") for path in links.paths):
        raise RuntimeError("frontend index has no JavaScript entrypoint")
    for source in links.paths:
        url = urlparse(source)
        asset = (static / url.path.lstrip("/")).resolve()
        if url.scheme or url.netloc or static.resolve() not in asset.parents or not asset.is_file():
            raise RuntimeError(f"frontend index references a missing or nonlocal asset: {source}")


class CustomBuildHook(BuildHookInterface):
    """Reuse only complete Vite output matching the current frontend inputs."""

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        static = root / "npu_top" / "static"
        fingerprint = source_fingerprint(root)
        try:
            manifest = json.loads((static / MANIFEST).read_text(encoding="utf-8"))
            require_assets(static)
            valid = manifest == {"source": fingerprint, "files": asset_hashes(static)}
        except (OSError, ValueError, RuntimeError):
            valid = False
        if not valid:
            npm = shutil.which("npm")
            if not npm:
                raise RuntimeError("npu-top wheel requires frontend assets matching current sources; install npm or build the wheel with npm available")
            subprocess.check_call([npm, "ci", "--no-audit", "--no-fund"], cwd=root)
            subprocess.check_call([npm, "run", "build"], cwd=root)
            require_assets(static)
            if source_fingerprint(root) != fingerprint:
                raise RuntimeError("frontend sources changed during build; wheel was not produced")
            (static / MANIFEST).write_text(json.dumps({"source": fingerprint, "files": asset_hashes(static)}, sort_keys=True), encoding="utf-8")
        build_data["force_include"][str(static)] = "npu_top/static"
