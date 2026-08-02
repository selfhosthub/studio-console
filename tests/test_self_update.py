# tests/test_self_update.py
"""In-container self-update: baked-version probe and the uv update path."""

import json
import shutil

from studio_console import commands


class TestBakedConsoleVersion:
    def test_reads_tag_from_release_json(self, tmp_path):
        p = tmp_path / "release.json"
        p.write_text(json.dumps({"tag_name": "v1.6.0"}))
        assert commands._baked_console_version(p) == "1.6.0"

    def test_missing_file_is_empty(self, tmp_path):
        assert commands._baked_console_version(tmp_path / "absent.json") == ""

    def test_malformed_json_is_empty(self, tmp_path):
        p = tmp_path / "release.json"
        p.write_text("not json")
        assert commands._baked_console_version(p) == ""


class TestContainerSelfUpdate:
    def test_updates_via_uv(self, monkeypatch):
        calls = []
        monkeypatch.setattr(commands, "run", lambda cmd, **kw: calls.append(cmd))
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/uv")
        commands.cmd_self_update("container")
        assert ["uv", "tool", "install", "--force", "studio-console"] in calls

    def test_runpod_takes_same_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(commands, "run", lambda cmd, **kw: calls.append(cmd))
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/uv")
        commands.cmd_self_update("runpod")
        assert ["uv", "tool", "install", "--force", "studio-console"] in calls

    def test_no_uv_means_no_update_attempt(self, monkeypatch):
        calls = []
        monkeypatch.setattr(commands, "run", lambda cmd, **kw: calls.append(cmd))
        monkeypatch.setattr(shutil, "which", lambda name: None)
        commands.cmd_self_update("container")
        assert calls == []
