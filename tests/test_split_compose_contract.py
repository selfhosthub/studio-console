"""The split boot refuses images that don't declare the compose worker contract."""

import json

from studio_console import commands


GOOD_MANIFEST = json.dumps(
    {"shapes": {"split": {"compose_capabilities": {"runtime_user": "..."}}}}
)
OLD_MANIFEST = json.dumps({"shapes": {"full": {}, "core": {}}})


def _env(profiles, version="1.4.0"):
    return {"COMPOSE_PROFILES": profiles, "SHS_STUDIO_VERSION": version}


def _patch_manifest_read(monkeypatch, rc, out):
    from studio_console import env as env_mod

    calls = []

    def fake_run_quiet(cmd, timeout=10):
        calls.append(cmd)
        return rc, out

    monkeypatch.setattr(env_mod, "run_quiet", fake_run_quiet)
    return calls


class TestSplitComposeContract:
    def test_no_worker_profiles_skips_check(self, monkeypatch):
        calls = _patch_manifest_read(monkeypatch, 1, "")
        assert commands._check_split_compose_contract(_env("cloudflared")) is True
        assert calls == []

    def test_declared_capability_passes(self, monkeypatch):
        _patch_manifest_read(monkeypatch, 0, GOOD_MANIFEST)
        assert commands._check_split_compose_contract(_env("worker-audio")) is True

    def test_old_image_without_manifest_blocks(self, monkeypatch, capsys):
        _patch_manifest_read(monkeypatch, 1, "cat: /app/contracts/launch-manifest.json: No such file")
        assert commands._check_split_compose_contract(_env("worker-video,cloudflared")) is False
        out = capsys.readouterr().out
        assert "SHS_STUDIO_VERSION" in out and "1.8.x" in out

    def test_manifest_without_split_section_blocks(self, monkeypatch):
        _patch_manifest_read(monkeypatch, 0, OLD_MANIFEST)
        assert commands._check_split_compose_contract(_env("worker-general")) is False

    def test_malformed_manifest_blocks(self, monkeypatch):
        _patch_manifest_read(monkeypatch, 0, "not json {")
        assert commands._check_split_compose_contract(_env("worker-comfyui-image")) is False

    def test_checks_the_pinned_api_image(self, monkeypatch):
        calls = _patch_manifest_read(monkeypatch, 0, GOOD_MANIFEST)
        commands._check_split_compose_contract(_env("worker-audio", version="1.4.2"))
        assert any("ghcr.io/selfhosthub/studio-api:1.4.2" in c for c in calls[0])
