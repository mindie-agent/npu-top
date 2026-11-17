"""Environment-driven configuration for `npu-top`.

[Role] Parses and validates primary `MINDIE_TOP_*` environment variables (legacy `NFM_*` aliases remain accepted) into an immutable `Settings` dataclass.
[Invariants] `MINDIE_TOP_*` takes precedence over `NFM_*`; malformed or out-of-range explicit settings raise `ValueError` instead of silently falling back to defaults. Unconfigured installs default to an owned MindIE/XDG state directory and adopt `./data` ONLY when it already holds a verified `npu-top` SQLite database (never polluting arbitrary working directories or moving unrelated `./data` folders).
[Timeouts & Locks] Default `ssh_timeout=20s` exceeds inner `timeout 8` `npu-smi` commands; read-only legacy DB inspection opens with `?mode=ro` (`timeout=1.0`) and acquires zero write locks.
[Side-Effect Policy] `load()` is strictly read-only (never mutating `os.environ` or creating WAL sidecars); `prepare()` creates the monitor state directory (`0o700` on POSIX).
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SQLITE_MAGIC = b"SQLite format 3\x00"
_RECOGNIZED_TABLES = ("servers", "host_rollups", "device_rollups", "device_samples", "telemetry_runs")
_LEGACY_DB_NAMES = ("monitor.sqlite3", "npu_top.sqlite3")


def _env(primary: str, legacy: str | None = None) -> str | None:
    """Return the primary name when it is set, otherwise the legacy alias."""
    if primary in os.environ:
        return os.environ[primary]
    if legacy is not None and legacy in os.environ:
        return os.environ[legacy]
    return None


def _number(primary: str, default: int, minimum: int, legacy: str | None = None) -> int:
    raw = _env(primary, legacy)
    if raw is None:
        return default
    name = primary if primary in os.environ else legacy or primary
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer >= {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _paths(primary: str, legacy: str | None = None) -> tuple[Path, ...]:
    """Parse an ``os.pathsep``-separated list of file paths from the environment."""
    raw = _env(primary, legacy) or ""
    return tuple(Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip())


def _is_recognized_npu_top_db(db_file: Path) -> bool:
    """Verify read-only that ``db_file`` is an existing `npu-top` SQLite DB without creating WAL sidecars."""
    try:
        if not db_file.is_file():
            return False
        with db_file.open("rb") as fh:
            if fh.read(16) != _SQLITE_MAGIC:
                return False
        query_flag = "mode=ro" if db_file.with_name(f"{db_file.name}-wal").exists() else "immutable=1"
        uri = f"{db_file.resolve().as_uri()}?{query_flag}"
        with contextlib.closing(sqlite3.connect(uri, timeout=1.0, uri=True)) as conn:
            placeholders = ",".join("?" for _ in _RECOGNIZED_TABLES)
            row = conn.execute(
                f"SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ({placeholders}) LIMIT 1",
                _RECOGNIZED_TABLES,
            ).fetchone()
            return row is not None
    except TimeoutError:
        raise
    except (sqlite3.Error, OSError, ValueError):
        return False


def default_state_dir(*, cwd: Path | None = None) -> Path:
    """Resolve owned state directory, adopting `./data` only if it already holds a genuine `npu-top` DB."""
    active_cwd = Path.cwd() if cwd is None else Path(cwd)
    legacy_dir = active_cwd / "data"
    if any(_is_recognized_npu_top_db(legacy_dir / name) for name in _LEGACY_DB_NAMES):
        return legacy_dir.resolve()
    mindie_data = (os.environ.get("MINDIE_DATA_HOME") or "").strip()
    if mindie_data:
        return (Path(mindie_data).expanduser() / "npu-top").resolve()
    xdg_data = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg_data:
        return (Path(xdg_data).expanduser() / "mindie-agent" / "npu-top").resolve()
    if os.name == "nt":
        local_app = (os.environ.get("LOCALAPPDATA") or "").strip()
        if local_app:
            return (Path(local_app).expanduser() / "mindie-agent" / "npu-top").resolve()
    try:
        base_home = Path.home()
    except RuntimeError:
        base_home = active_cwd
    return (base_home / ".local" / "share" / "mindie-agent" / "npu-top").resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    state_dir: Path
    bind: str
    port: int
    idle_interval: int
    history_interval: int
    infrastructure_interval: int
    retention_days: int
    max_workers: int
    ssh_timeout: int
    hbm_busy_threshold_mb: int
    # Inventory is explicitly configured, never inferred from a checkout.
    inventory_files: tuple[Path, ...] = ()
    host_pool_files: tuple[Path, ...] = ()
    sqlite_max_mb: int = 1024

    @classmethod
    def load(cls) -> "Settings":
        raw_state_dir = _env("MINDIE_TOP_STATE_DIR", "NFM_STATE_DIR")
        state_dir = (
            Path(raw_state_dir).expanduser().resolve()
            if raw_state_dir and raw_state_dir.strip()
            else default_state_dir()
        )
        return cls(
            project_root=state_dir,
            state_dir=state_dir,
            bind=_env("MINDIE_TOP_BIND", "NFM_BIND") or "127.0.0.1",
            port=_number("MINDIE_TOP_PORT", 8788, 1, "NFM_PORT"),
            idle_interval=_number("MINDIE_TOP_IDLE_INTERVAL_SECONDS", 120, 10, "NFM_IDLE_INTERVAL_SECONDS"),
            history_interval=_number("MINDIE_TOP_HISTORY_INTERVAL_SECONDS", 30, 5, "NFM_HISTORY_INTERVAL_SECONDS"),
            infrastructure_interval=_number("MINDIE_TOP_INFRA_INTERVAL_SECONDS", 60, 15, "NFM_INFRA_INTERVAL_SECONDS"),
            retention_days=_number("MINDIE_TOP_RETENTION_DAYS", 90, 1, "NFM_RETENTION_DAYS"),
            max_workers=_number("MINDIE_TOP_MAX_WORKERS", 8, 1, "NFM_MAX_WORKERS"),
            ssh_timeout=_number("MINDIE_TOP_SSH_TIMEOUT_SECONDS", 20, 2, "NFM_SSH_TIMEOUT_SECONDS"),
            sqlite_max_mb=_number("MINDIE_TOP_SQLITE_MAX_MB", 1024, 16, "NFM_SQLITE_MAX_MB"),
            hbm_busy_threshold_mb=_number(
                "MINDIE_TOP_HBM_BUSY_THRESHOLD_MB", 8192, 1, "NFM_HBM_BUSY_THRESHOLD_MB",
            ),
            inventory_files=_paths("MINDIE_TOP_INVENTORY_FILES", "NFM_INVENTORY_FILES"),
            host_pool_files=_paths("MINDIE_TOP_HOST_POOL_FILES", "NFM_HOST_POOL_FILES"),
        )

    def prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
