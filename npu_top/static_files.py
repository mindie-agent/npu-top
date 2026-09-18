from __future__ import annotations

from pathlib import Path

STATIC_MISSING = (
    "npu-top: 未找到打包的前端静态资源（npu_top/static/index.html）。"
    "请安装 GitHub Release 中的 wheel，或在源码树运行 scripts/build_wheel.sh 后再安装。"
)


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def require_static() -> Path:
    root = static_dir()
    if not (root / "index.html").is_file():
        raise SystemExit(STATIC_MISSING)
    return root
