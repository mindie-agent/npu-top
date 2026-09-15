from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _number(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _paths(name: str) -> tuple[Path, ...]:
    """Parse an ``os.pathsep``-separated list of file paths from the environment."""
    raw = os.environ.get(name, "")
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
        state_dir = Path(os.environ.get("NFM_STATE_DIR", Path.cwd() / "data")).expanduser().resolve()
        bootstrap_command = os.environ.get("NFM_BOOTSTRAP_COMMAND", "").strip() or None
        return cls(
            project_root=state_dir,
            state_dir=state_dir,
            bind=os.environ.get("NFM_BIND", "127.0.0.1"),
            port=_number("NFM_PORT", 8788, 1),
            idle_interval=_number("NFM_IDLE_INTERVAL_SECONDS", 120, 10),
            history_interval=_number("NFM_HISTORY_INTERVAL_SECONDS", 30, 5),
            infrastructure_interval=_number("NFM_INFRA_INTERVAL_SECONDS", 60, 15),
            retention_days=_number("NFM_RETENTION_DAYS", 90, 1),
            max_workers=_number("NFM_MAX_WORKERS", 8, 1),
            ssh_timeout=_number("NFM_SSH_TIMEOUT_SECONDS", 12, 2),
            sqlite_max_mb=_number("NFM_SQLITE_MAX_MB", 1024, 16),
            hbm_busy_threshold_mb=_number("NFM_HBM_BUSY_THRESHOLD_MB", 8192, 1),
            inventory_files=_paths("NFM_INVENTORY_FILES"),
            host_pool_files=_paths("NFM_HOST_POOL_FILES"),
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
