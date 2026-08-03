# tests/test_shape_lifecycle.py
"""Host-side lifecycle for full/core targets the docker-run container.

full/core containers carry no compose labels, so start/stop/restart must be
docker verbs on the named container, never the split compose stack. Pure
function tests (no Docker). Run with `make test`.
"""

from __future__ import annotations

from studio_console.commands_launch import SHAPE_CONTAINERS, _lifecycle_cmd


def test_shape_containers_cover_launcher_shapes():
    assert SHAPE_CONTAINERS == {"full": "studio-full", "core": "studio-core"}


def test_lifecycle_targets_named_container():
    assert _lifecycle_cmd("full", "stop") == ["docker", "stop", "studio-full"]
    assert _lifecycle_cmd("full", "restart") == ["docker", "restart", "studio-full"]
    assert _lifecycle_cmd("core", "start") == ["docker", "start", "studio-core"]
