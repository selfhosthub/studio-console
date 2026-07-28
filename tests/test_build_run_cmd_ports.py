# tests/test_build_run_cmd_ports.py
"""_build_run_cmd port publishing across publish_internal and localhost_publish.

Pure-function tests (no Docker). Run with `python -m pytest tests/` or
standalone via `python tests/test_build_run_cmd_ports.py`.

Pins the contract: internal ports with localhost_publish are always published
on the bind address (never bare), internal ports without it follow
publish_internal, and SHS_PUBLISH_INTERNAL_BIND overrides the manifest bind.
"""

from __future__ import annotations

import os
from pathlib import Path

from studio_console.commands_launch import _build_run_cmd

MANIFEST = {
    "ports": [
        {"container": 80, "name": "nginx"},
        {"container": 8000, "name": "api", "internal": True, "localhost_publish": "127.0.0.1"},
        {"container": 3000, "name": "ui", "internal": True},
        {"container": 9001, "name": "supervisor", "internal": True, "localhost_publish": "127.0.0.1"},
    ],
    "volumes": [{"container_path": "/workspace"}],
}


def _port_flags(publish_internal: bool) -> list[str]:
    cmd = _build_run_cmd(
        MANIFEST, "img:tag", {}, Path("/data"), publish_internal=publish_internal
    )
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-p"]


def test_publish_internal_false_binds_localhost_publish_ports() -> None:
    assert _port_flags(publish_internal=False) == [
        "80:80",
        "127.0.0.1:8000:8000",
        "127.0.0.1:9001:9001",
    ]


def test_publish_internal_true_still_binds_localhost_publish_ports() -> None:
    assert _port_flags(publish_internal=True) == [
        "80:80",
        "127.0.0.1:8000:8000",
        "3000:3000",
        "127.0.0.1:9001:9001",
    ]


def test_env_override_replaces_manifest_bind() -> None:
    os.environ["SHS_PUBLISH_INTERNAL_BIND"] = "0.0.0.0"
    try:
        assert _port_flags(publish_internal=False) == [
            "80:80",
            "0.0.0.0:8000:8000",
            "0.0.0.0:9001:9001",
        ]
    finally:
        del os.environ["SHS_PUBLISH_INTERNAL_BIND"]


def test_env_override_leaves_plain_internal_ports_alone() -> None:
    os.environ["SHS_PUBLISH_INTERNAL_BIND"] = "192.168.1.10"
    try:
        assert _port_flags(publish_internal=True) == [
            "80:80",
            "192.168.1.10:8000:8000",
            "3000:3000",
            "192.168.1.10:9001:9001",
        ]
    finally:
        del os.environ["SHS_PUBLISH_INTERNAL_BIND"]


if __name__ == "__main__":
    import sys
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
