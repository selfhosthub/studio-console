# tests/test_resolve.py
"""Resolution engine invariants (ruling 2026-07-30).

Operational vars: explicit > profile > persisted .env > default. Identity
vars invert profile and persisted so a wizard-set value survives relaunch.
Ambient exports that disagree with the resolved config are named for refusal.
Pure functions, no Docker. Run with `make test`.
"""

from __future__ import annotations

from studio_console.env import read_env
from studio_console.resolve import (
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_PERSISTED,
    SOURCE_PROFILE,
    ambient_conflicts,
    effective,
    format_provenance,
    load_profile,
    resolve_boot,
    resolve_env,
)

OPERATIONAL = "SHS_API_HOSTNAME"
IDENTITY = "SHS_PUBLIC_BASE_URL"


def _one(name, **tiers):
    return resolve_env(
        explicit=tiers.get("explicit", {}),
        profile=tiers.get("profile", {}),
        persisted=tiers.get("persisted", {}),
        defaults=tiers.get("defaults", {}),
    )[name]


def test_operational_profile_beats_persisted():
    r = _one(OPERATIONAL, profile={OPERATIONAL: "api.example.com"}, persisted={OPERATIONAL: "stale.example.com"})
    assert r.value == "api.example.com"
    assert r.source == SOURCE_PROFILE
    assert r.losers == [(SOURCE_PERSISTED, "stale.example.com")]


def test_identity_persisted_beats_profile():
    r = _one(IDENTITY, profile={IDENTITY: "http://profile"}, persisted={IDENTITY: "http://wizard-set"})
    assert r.value == "http://wizard-set"
    assert r.source == SOURCE_PERSISTED


def test_explicit_beats_everything():
    r = _one(IDENTITY, explicit={IDENTITY: "http://flag"}, profile={IDENTITY: "http://profile"}, persisted={IDENTITY: "http://wizard-set"})
    assert r.value == "http://flag"
    assert r.source == SOURCE_EXPLICIT


def test_default_is_last_for_both_classes():
    assert _one(OPERATIONAL, defaults={OPERATIONAL: "d"}).source == SOURCE_DEFAULT
    assert _one(IDENTITY, defaults={IDENTITY: "d"}).source == SOURCE_DEFAULT


def test_unclassified_var_is_operational():
    r = _one("SHS_DB_POOL_SIZE", profile={"SHS_DB_POOL_SIZE": "20"}, persisted={"SHS_DB_POOL_SIZE": "10"})
    assert r.value == "20"


def test_empty_values_are_unset():
    r = _one(OPERATIONAL, profile={OPERATIONAL: ""}, persisted={OPERATIONAL: "kept"})
    assert r.value == "kept"
    assert r.source == SOURCE_PERSISTED


def test_agreeing_tiers_are_not_losers():
    r = _one(OPERATIONAL, profile={OPERATIONAL: "same"}, persisted={OPERATIONAL: "same"})
    assert r.losers == []


def test_load_profile(tmp_path):
    p = tmp_path / "profile"
    p.write_text("# comment\nSHS_API_HOSTNAME=api.example.com\nEMPTY=\nbroken line\n")
    assert load_profile(p) == {"SHS_API_HOSTNAME": "api.example.com"}
    assert load_profile(tmp_path / "missing") == {}


def test_ambient_conflicts_named():
    env = {"SHS_STUDIO_VERSION": "9.9.9", "POSTGRES_PASSWORD": "hunter2", "PATH": "/usr/bin"}
    eff = {"SHS_STUDIO_VERSION": "1.2.10"}
    assert ambient_conflicts(env, eff) == ["POSTGRES_PASSWORD", "SHS_STUDIO_VERSION"]


def test_ambient_agreement_and_allowlist_pass():
    env = {"SHS_STUDIO_VERSION": "1.2.10", "SHS_WORKSPACE_DIR": "/tmp/ws"}
    eff = {"SHS_STUDIO_VERSION": "1.2.10"}
    assert ambient_conflicts(env, eff, allow=frozenset({"SHS_WORKSPACE_DIR"})) == []


def test_resolve_boot_merges_profile_into_existing_env(tmp_path):
    ef = tmp_path / ".env"
    ef.write_text("SHS_API_HOSTNAME=stale.example.com\nSHS_PUBLIC_BASE_URL=http://wizard-set\n")
    profile = tmp_path / "profile"
    profile.write_text("SHS_API_HOSTNAME=api.example.com\nSHS_PUBLIC_BASE_URL=http://profile\n")
    eff = resolve_boot(ef, profile, {}, {})
    persisted = read_env(ef)
    assert persisted["SHS_API_HOSTNAME"] == "api.example.com"
    assert persisted["SHS_PUBLIC_BASE_URL"] == "http://wizard-set"
    assert eff["SHS_API_HOSTNAME"] == "api.example.com"


def test_resolve_boot_never_creates_the_env(tmp_path):
    ef = tmp_path / ".env"
    profile = tmp_path / "profile"
    profile.write_text("SHS_API_HOSTNAME=api.example.com\n")
    eff = resolve_boot(ef, profile, {}, {})
    assert not ef.exists()
    assert eff["SHS_API_HOSTNAME"] == "api.example.com"


def test_resolve_boot_never_persists_transient_creds(tmp_path):
    ef = tmp_path / ".env"
    ef.write_text("SHS_ENV=production\n")
    profile = tmp_path / "profile"
    profile.write_text("SHS_ADMIN_PASSWORD=hunter2\nSHS_ENTITLEMENT_TOKEN=tok\n")
    eff = resolve_boot(ef, profile, {}, {})
    persisted = read_env(ef)
    assert "SHS_ADMIN_PASSWORD" not in persisted
    assert "SHS_ENTITLEMENT_TOKEN" not in persisted
    assert eff["SHS_ADMIN_PASSWORD"] == "hunter2"


def test_resolve_boot_refuses_conflicting_export(tmp_path):
    ef = tmp_path / ".env"
    ef.write_text("SHS_STUDIO_VERSION=1.2.10\n")
    environ = {"SHS_STUDIO_VERSION": "9.9.9"}
    assert resolve_boot(ef, None, {}, environ) is None
    assert read_env(ef)["SHS_STUDIO_VERSION"] == "1.2.10"


def test_resolve_boot_allows_agreeing_export(tmp_path):
    ef = tmp_path / ".env"
    ef.write_text("SHS_STUDIO_VERSION=1.2.10\n")
    environ = {"SHS_STUDIO_VERSION": "1.2.10"}
    assert resolve_boot(ef, None, {}, environ) is not None


def test_provenance_marks_disagreement_and_masks():
    resolved = resolve_env(
        explicit={},
        profile={"SHS_ADMIN_PASSWORD": "supersecret", OPERATIONAL: "api.example.com"},
        persisted={OPERATIONAL: "stale.example.com"},
        defaults={},
    )
    lines = format_provenance(resolved)
    text = "\n".join(lines)
    assert "supersecret" not in text
    assert "supe***" in text
    assert f"{OPERATIONAL} = api.example.com (profile)  WINNER" in lines
    assert f"  .env = stale.example.com  OVERRIDDEN" in lines
