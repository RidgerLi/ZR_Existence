import os
import tempfile

import pytest

from framework import telemetry


@pytest.fixture(scope="session", autouse=True)
def _telemetry_db():
    d = tempfile.mkdtemp(prefix="zrl_telemetry_")
    telemetry.init(os.path.join(d, "test.db"))
    yield
