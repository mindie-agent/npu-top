"""Hatch wheel build hook that packages prebuilt frontend assets.

[Role] Builds `npu_top/static` with npm only when a wheel target lacks `index.html`, then registers the asset tree as an artifact.
[Invariants] Source and sdist targets never run npm; wheel builds either reuse shipped assets or fail closed when npm is unavailable.
[Timeouts & Locks] `npm ci` is bounded at `300s` and `npm run build` at `120s`; zero locks.
[Side-Effect Policy] Installs Node dependencies and writes generated frontend assets under `npu_top/static` when assets are absent.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        static = root / "npu_top" / "static"
        if not (static / "index.html").is_file():
            npm = shutil.which("npm")
            if not npm:
                raise RuntimeError("npu-top wheel requires frontend assets; install npm or build static assets first")
            subprocess.check_call([npm, "ci", "--no-audit", "--no-fund"], cwd=root, timeout=300)
            subprocess.check_call([npm, "run", "build"], cwd=root, timeout=120)
        # Assets may already be present in an sdist; include each path once.
        build_data["artifacts"].append("npu_top/static/**")
