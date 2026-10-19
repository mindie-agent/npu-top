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
            subprocess.check_call([npm, "ci", "--no-audit", "--no-fund"], cwd=root)
            subprocess.check_call([npm, "run", "build"], cwd=root)
        # Assets may already be present in an sdist; include each path once.
        build_data["artifacts"].append("npu_top/static/**")
