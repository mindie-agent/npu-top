from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from npu_top.static_files import STATIC_MISSING, require_static


class StaticFilesTests(unittest.TestCase):
    def test_require_static_exits_with_explicit_message(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("npu_top.static_files.static_dir", return_value=Path(root)):
                with self.assertRaises(SystemExit) as caught:
                    require_static()
        self.assertEqual(str(caught.exception), STATIC_MISSING)


if __name__ == "__main__":
    unittest.main()
