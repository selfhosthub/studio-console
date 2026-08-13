import os
import sys
from unittest import mock

from studio_console.wizard import _run_gid, _run_uid


def test_darwin_uses_image_appuser_not_host_uid():
    with mock.patch.object(sys, "platform", "darwin"):
        assert _run_uid() == "1000"
        assert _run_gid() == "1000"


def test_linux_uses_host_ids():
    with mock.patch.object(sys, "platform", "linux"):
        assert _run_uid() == str(os.getuid())
        assert _run_gid() == str(os.getgid())
