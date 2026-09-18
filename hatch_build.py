from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Include Vite static assets in the wheel; run npm only when they are absent."""

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        static = root / "npu_top" / "static"
        if not (static / "index.html").is_file():
            npm = shutil.which("npm")
            if npm:
                subprocess.check_call([npm, "ci", "--no-audit", "--no-fund"], cwd=root)
                subprocess.check_call([npm, "run", "build"], cwd=root)
        if (static / "index.html").is_file():
            build_data["force_include"][str(static)] = "npu_top/static"
