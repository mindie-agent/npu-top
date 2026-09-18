"""Never send intentional test failures to the user's diagnostic worker."""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_diagnostics(tmp_path_factory):
    from mindie_diagnostics import configure

    root = tmp_path_factory.mktemp("top-diagnostics")
    previous = os.environ.get("MINDIE_DIAGNOSTICS_ROOT")
    os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(root)
    recorder = configure("npu-top", root=root)
    yield root
    recorder.close()
    if previous is None:
        os.environ.pop("MINDIE_DIAGNOSTICS_ROOT", None)
    else:
        os.environ["MINDIE_DIAGNOSTICS_ROOT"] = previous
