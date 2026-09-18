from __future__ import annotations

import unittest
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from npu_top.probe import FAST_SCRIPT, INFRA_SCRIPT, PROCESS_DETAIL_SCRIPT, HostProbe


class RemoteScriptTests(unittest.TestCase):
    def test_ssh_stdin_preserves_lf_and_utf8_on_every_platform(self) -> None:
        receiver = (
            "import json,sys; data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(json.dumps(list(data)).encode()); "
            "sys.stderr.buffer.write(bytes([255]))"
        )
        adapter = SimpleNamespace(
            ssh_base=lambda server: [sys.executable, "-c", receiver],
            project_root=Path.cwd(),
        )
        result = HostProbe(adapter, 10)._run_script({}, "set +e\r\n# 探测\n", 10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(bytes(json.loads(result.stdout)), "set +e\n# 探测\n".encode())
        self.assertEqual(result.stderr, "\ufffd")

    def test_core_probe_captures_its_own_status(self) -> None:
        self.assertIn("npu_info_rc", FAST_SCRIPT)
        self.assertNotIn("exit 0", FAST_SCRIPT)

    def test_optional_infrastructure_has_section_boundaries(self) -> None:
        for section in ("disk", "mounts", "docker", "docker_stats", "docker_info"):
            self.assertIn(f"__NFM_SECTION__{section}", INFRA_SCRIPT)

    def test_process_detail_probe_reads_proc_without_docker_polling(self) -> None:
        for path in ("cwd", "cmdline", "exe", "comm", "cgroup"):
            self.assertIn(f'/proc/$nfm_pid/{path}', PROCESS_DETAIL_SCRIPT)
        self.assertNotIn("docker inspect", PROCESS_DETAIL_SCRIPT)


if __name__ == "__main__":
    unittest.main()
