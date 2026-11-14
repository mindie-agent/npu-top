"""Hatch wheel build hook that packages prebuilt frontend assets.

[Role] Builds `npu_top/static` with native `npm` only when a wheel target lacks `index.html`, then registers the asset tree as an artifact.
[Invariants] Source and sdist targets never run npm; wheel builds prefer `npm.cmd` with `CREATE_NO_WINDOW` on Windows, reject WSL2 DrvFS / Windows PE `npm` shadows on POSIX, and propagate `TimeoutError` across every `OSError` boundary.
[Timeouts & Locks] `npm ci` is bounded at `300s` and `npm run build` at `120s` with `stdin=DEVNULL`; zero locks.
[Side-Effect Policy] Installs Node dependencies and writes generated frontend assets under `npu_top/static` when assets are absent.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # pragma: no cover - runtime test env without hatchling
    BuildHookInterface = object  # type: ignore[misc,assignment]

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_IS_WINDOWS = os.name == "nt"
_MOUNTINFO_PATH = "/proc/self/mountinfo"
_WIN_SCRIPT_EXTS = (".exe", ".cmd", ".bat", ".com", ".ps1")


def _no_window_kwargs() -> dict[str, int]:
    return {"creationflags": CREATE_NO_WINDOW} if _IS_WINDOWS else {}


def _decode_mountinfo_field(field: str) -> str:
    for oct_seq, ch in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(oct_seq, ch)
    return field


def _strict_realpath(raw_text: str) -> str:
    probe = raw_text if os.path.isabs(raw_text) else os.path.join(os.getcwd(), raw_text)
    suffix: list[str] = []
    while True:
        try:
            resolved = os.path.realpath(probe, strict=True)
            return os.path.normpath(os.path.join(resolved, *reversed(suffix))) if suffix else resolved
        except TimeoutError:
            raise
        except (FileNotFoundError, NotADirectoryError):
            parent, name = os.path.split(probe.rstrip("/"))
            if not name or parent == probe:
                return os.path.abspath(raw_text)
            suffix.append(name)
            probe = parent
        except OSError:
            return os.path.abspath(raw_text)


def is_wsl_drvfs(value: str | os.PathLike[str]) -> bool:
    if _IS_WINDOWS:
        return False
    try:
        resolved = _strict_realpath(os.fspath(value))
        lines = Path(_MOUNTINFO_PATH).read_text(encoding="utf-8", errors="replace").splitlines()
    except TimeoutError:
        raise
    except OSError:
        return False
    best_mp = ""
    best_fstype = ""
    best_opts = ""
    for line in lines:
        parts = line.split(" - ", 1)
        if len(parts) != 2:
            continue
        lhs = parts[0].split()
        rhs = parts[1].split()
        if len(lhs) < 5 or len(rhs) < 3:
            continue
        mp = _decode_mountinfo_field(lhs[4])
        if resolved == mp or resolved.startswith(mp.rstrip("/") + "/") or mp == "/":
            if len(mp) >= len(best_mp):
                best_mp = mp
                best_fstype = rhs[0]
                best_opts = rhs[2]
    if not best_mp:
        return False
    return best_fstype in {"drvfs", "9p"} and ("aname=drvfs" in best_opts or best_fstype == "drvfs")


def is_pe_executable(value: str | os.PathLike[str]) -> bool:
    try:
        with Path(value).open("rb") as handle:
            dos = handle.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(dos[0x3C:0x40], "little", signed=False)
            handle.seek(pe_offset)
            return handle.read(4) == b"PE\x00\x00"
    except TimeoutError:
        raise
    except OSError:
        return False


def _is_exec_regular_file(cand: Path) -> bool:
    try:
        st = cand.stat()
        return stat.S_ISREG(st.st_mode) and os.access(cand, os.X_OK)
    except TimeoutError:
        raise
    except OSError:
        return False


def resolve_npm(*, path: str | None = None) -> str | None:
    if _IS_WINDOWS:
        return shutil.which("npm.cmd", path=path) or shutil.which("npm", path=path)
    search_path = os.environ.get("PATH", None) if path is None else path
    if search_path is None:
        search_path = os.defpath
    if not search_path:
        return None
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        cand = Path(entry) / "npm"
        if cand.suffix.lower() in _WIN_SCRIPT_EXTS:
            continue
        if not _is_exec_regular_file(cand):
            continue
        if is_wsl_drvfs(cand) or is_pe_executable(cand):
            continue
        return str(cand)
    return None


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        static = root / "npu_top" / "static"
        if not (static / "index.html").is_file():
            npm = resolve_npm()
            if not npm:
                raise RuntimeError("npu-top wheel requires frontend assets; install npm or build static assets first")
            extra = _no_window_kwargs()
            subprocess.check_call(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=root, stdin=subprocess.DEVNULL, timeout=300, **extra,
            )
            subprocess.check_call(
                [npm, "run", "build"],
                cwd=root, stdin=subprocess.DEVNULL, timeout=120, **extra,
            )
        # Assets may already be present in an sdist; include each path once.
        build_data["artifacts"].append("npu_top/static/**")
