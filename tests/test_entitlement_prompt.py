"""First boot must not re-ask for the entitlement token the wizard already asked."""
import pytest

from studio_console import commands


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SHS_ENTITLEMENT_TOKEN", raising=False)


def _fail_prompt(*a, **k):
    raise AssertionError("prompted for a token the wizard already answered")


class TestResolveEntitlementToken:
    def test_key_present_empty_means_answered_no_prompt(self, monkeypatch):
        monkeypatch.setattr(commands, "_prompt", _fail_prompt)
        commands._resolve_entitlement_token({"SHS_ENTITLEMENT_TOKEN": ""})

    def test_key_present_value_bridges_to_env(self, monkeypatch):
        monkeypatch.setattr(commands, "_prompt", _fail_prompt)
        commands._resolve_entitlement_token({"SHS_ENTITLEMENT_TOKEN": "tok123"})
        import os
        assert os.environ["SHS_ENTITLEMENT_TOKEN"] == "tok123"

    def test_key_absent_prompts_once(self, monkeypatch):
        asked = []
        monkeypatch.setattr(
            commands, "_prompt", lambda *a, **k: asked.append(1) or "tok9"
        )
        commands._resolve_entitlement_token({})
        import os
        assert asked == [1]
        assert os.environ["SHS_ENTITLEMENT_TOKEN"] == "tok9"

    def test_process_env_wins_over_absent_key(self, monkeypatch):
        monkeypatch.setenv("SHS_ENTITLEMENT_TOKEN", "envtok")
        monkeypatch.setattr(commands, "_prompt", _fail_prompt)
        commands._resolve_entitlement_token({})
