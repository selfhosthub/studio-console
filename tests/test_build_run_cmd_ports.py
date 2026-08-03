# tests/test_build_run_cmd_ports.py
"""_build_run_cmd port publishing across publish_internal and localhost_publish.

Pure-function tests (no Docker). Run with `make test`.

Pins the contract: internal ports with localhost_publish are always published
on the bind address (never bare), internal ports without it follow
publish_internal, and SHS_PUBLISH_INTERNAL_BIND from the resolved boot config
overrides the manifest bind (ambient env is never an input).
"""

from __future__ import annotations

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


def _port_flags(publish_internal: bool, eff: dict | None = None) -> list[str]:
    cmd = _build_run_cmd(
        MANIFEST, "img:tag", {}, Path("/data"), publish_internal=publish_internal, eff=eff
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


def test_resolved_override_replaces_manifest_bind() -> None:
    eff = {"SHS_PUBLISH_INTERNAL_BIND": "0.0.0.0"}
    assert _port_flags(publish_internal=False, eff=eff) == [
        "80:80",
        "0.0.0.0:8000:8000",
        "0.0.0.0:9001:9001",
    ]


def test_resolved_override_leaves_plain_internal_ports_alone() -> None:
    eff = {"SHS_PUBLISH_INTERNAL_BIND": "192.168.1.10"}
    assert _port_flags(publish_internal=True, eff=eff) == [
        "80:80",
        "192.168.1.10:8000:8000",
        "3000:3000",
        "192.168.1.10:9001:9001",
    ]
