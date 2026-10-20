"""Environment-driven configuration for `npu-top`.

[Role] Parses and validates primary `MINDIE_TOP_*` environment variables (legacy `NFM_*` aliases remain accepted) into an immutable `Settings` dataclass.
[Invariants] `MINDIE_TOP_*` takes precedence over `NFM_*`; malformed or out-of-range explicit settings raise `ValueError` instead of silently falling back to defaults.
[Timeouts & Locks] Default `ssh_timeout=20s` exceeds inner `timeout 8` `npu-smi` commands; zero locks.
[Side-Effect Policy] `prepare()` creates `state_dir`, `keys`, and `ssh-control` directories (`0o700`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    return tuple(
        Path(item).expanduser()
        for item in raw.split(os.pathsep)
        if item.strip()
    )


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
    # Optional host sources and credential bootstrap. All are explicit paths or
    # commands supplied by the operator; nothing is discovered from the
    # checkout location. See .env.example for the exact contract.
    inventory_files: tuple[Path, ...] = ()
    host_pool_files: tuple[Path, ...] = ()
    bootstrap_command: str | None = None

    sqlite_max_mb: int = 1024

    @classmethod
    def load(cls) -> "Settings":
        raw_state_dir = _env("MINDIE_TOP_STATE_DIR", "NFM_STATE_DIR")
        state_dir = Path(raw_state_dir or Path.cwd() / "data").expanduser().resolve()
        bootstrap_command = (_env("MINDIE_TOP_BOOTSTRAP_COMMAND", "NFM_BOOTSTRAP_COMMAND") or "").strip() or None
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
            bootstrap_command=bootstrap_command,
        )

    def prepare(self) -> None:
        for directory in (
            self.state_dir,
            self.state_dir / "keys",
            self.state_dir / "ssh-control",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
