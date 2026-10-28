"""Environment precedence for primary `MINDIE_TOP_*` settings and legacy `NFM_*` aliases."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from npu_top.settings import Settings


def load(environment: dict[str, str]) -> Settings:
    with mock.patch.dict(os.environ, environment, clear=True):
        return Settings.load()


class MindieTopSettingsTests(unittest.TestCase):
    def test_defaults_are_unchanged_with_no_environment(self) -> None:
        settings = load({})
        self.assertEqual(settings.bind, "127.0.0.1")
        self.assertEqual(settings.port, 8788)
        self.assertEqual(settings.idle_interval, 120)
        self.assertEqual(settings.history_interval, 30)
        self.assertEqual(settings.infrastructure_interval, 60)
        self.assertEqual(settings.retention_days, 90)
        self.assertEqual(settings.max_workers, 8)
        self.assertEqual(settings.ssh_timeout, 20)
        self.assertEqual(settings.sqlite_max_mb, 1024)
        self.assertEqual(settings.hbm_busy_threshold_mb, 8192)
        self.assertEqual(settings.inventory_files, ())
        self.assertEqual(settings.host_pool_files, ())

    def test_legacy_nfm_names_remain_accepted(self) -> None:
        settings = load({
            "NFM_BIND": "::1", "NFM_PORT": "9001", "NFM_HISTORY_INTERVAL_SECONDS": "15",
            "NFM_RETENTION_DAYS": "7", "NFM_SSH_TIMEOUT_SECONDS": "12",
        })
        self.assertEqual(settings.bind, "::1")
        self.assertEqual(settings.port, 9001)
        self.assertEqual(settings.history_interval, 15)
        self.assertEqual(settings.retention_days, 7)
        self.assertEqual(settings.ssh_timeout, 12)

    def test_mindie_top_names_take_precedence_over_legacy(self) -> None:
        settings = load({
            "MINDIE_TOP_BIND": "localhost", "NFM_BIND": "::1",
            "MINDIE_TOP_PORT": "9100", "NFM_PORT": "9001",
            "MINDIE_TOP_HISTORY_INTERVAL_SECONDS": "45", "NFM_HISTORY_INTERVAL_SECONDS": "15",
            "MINDIE_TOP_MAX_WORKERS": "3", "NFM_MAX_WORKERS": "12",
        })
        self.assertEqual(settings.bind, "localhost")
        self.assertEqual(settings.port, 9100)
        self.assertEqual(settings.history_interval, 45)
        self.assertEqual(settings.max_workers, 3)

    def test_primary_value_does_not_silently_fall_back_when_invalid(self) -> None:
        with self.assertRaisesRegex(ValueError, "MINDIE_TOP_PORT"):
            load({"MINDIE_TOP_PORT": "not-a-number", "NFM_PORT": "9001"})
        with self.assertRaisesRegex(ValueError, "MINDIE_TOP_HISTORY_INTERVAL_SECONDS"):
            load({"MINDIE_TOP_HISTORY_INTERVAL_SECONDS": "1", "NFM_HISTORY_INTERVAL_SECONDS": "30"})

    def test_path_lists_support_primary_and_legacy_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = str(Path(directory) / "machines.json")
            second = str(Path(directory) / "hosts.txt")
            primary = load({
                "MINDIE_TOP_INVENTORY_FILES": os.pathsep.join((first, second)),
                "NFM_HOST_POOL_FILES": os.pathsep.join((second, first)),
            })
            self.assertEqual(primary.inventory_files, (Path(first), Path(second)))
            self.assertEqual(primary.host_pool_files, (Path(second), Path(first)))
            legacy = load({"NFM_INVENTORY_FILES": first, "NFM_HOST_POOL_FILES": second})
            self.assertEqual(legacy.inventory_files, (Path(first),))
            self.assertEqual(legacy.host_pool_files, (Path(second),))

    def test_state_dir_uses_primary_and_resolves_only_the_configured_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load({
                "MINDIE_TOP_STATE_DIR": directory,
                "NFM_STATE_DIR": str(Path(directory) / "legacy"),
            })
        self.assertEqual(settings.state_dir, Path(directory).resolve())
        self.assertEqual(settings.project_root, settings.state_dir)

    def test_load_does_not_rewrite_user_environment(self) -> None:
        environment = {"MINDIE_TOP_PORT": "9200", "NFM_BIND": "::1"}
        with mock.patch.dict(os.environ, environment, clear=True):
            Settings.load()
            self.assertEqual(dict(os.environ), environment)


if __name__ == "__main__":
    unittest.main()
