"""Local-only Ascend NPU fleet monitor."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("npu-top")
except PackageNotFoundError:
    __version__ = "0.1.6"
