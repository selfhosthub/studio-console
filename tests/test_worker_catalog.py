# tests/test_worker_catalog.py
"""Worker catalog consistency + audio GPU override generation."""

from pathlib import Path

from studio_console.constants import (
    ALL_COMPONENTS,
    COMPONENT_TO_IMAGE,
    COMPONENT_TO_PROFILE,
    IMAGE_BUILD_CONFIG,
    SCALE_PROFILES,
    SCALE_VARS,
    WORKER_CATALOG,
)


def test_every_catalog_worker_is_a_component():
    for w in WORKER_CATALOG:
        assert w["component"] in ALL_COMPONENTS


def test_catalog_derives_all_mappings():
    for w in WORKER_CATALOG:
        assert COMPONENT_TO_PROFILE[w["component"]] == w["profile"]
        assert COMPONENT_TO_IMAGE[w["component"]] == w["image"]
        assert SCALE_VARS[w["component"]] == w["scale_var"]
        assert SCALE_PROFILES[w["scale_var"]] == w["profile"]


def test_catalog_images_are_buildable():
    for w in WORKER_CATALOG:
        assert w["image"] in IMAGE_BUILD_CONFIG


def test_catalog_profiles_exist_in_compose():
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    for w in WORKER_CATALOG:
        assert f"profiles: [{w['profile']}]" in compose


def test_no_comfyui_video_vestiges():
    assert "ComfyUI video worker" not in ALL_COMPONENTS
    assert "SHS_COMFYUI_VIDEO_WORKERS" not in SCALE_PROFILES


def test_gpu_override_block_all_and_device():
    from studio_console.wizard import _gpu_override_block

    block_all = _gpu_override_block("all")
    assert "worker-audio:" in block_all
    assert "count: all" in block_all
    assert "driver: nvidia" in block_all

    block_dev = _gpu_override_block("1")
    assert 'device_ids: ["1"]' in block_dev
    assert "capabilities: [gpu]" in block_dev
