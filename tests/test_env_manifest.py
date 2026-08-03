# tests/test_env_manifest.py
"""Every env var the console touches is classified in ENV_CLASSES.

Guards the configuration-resolution ruling: each variable is identity
(first-boot persisted, never clobbered) or operational (profile re-resolves
every boot). An unclassified var is a build error; this suite is the build
error. Pure file/regex checks, no Docker. Run with `make test`.
"""

from __future__ import annotations

import re
from pathlib import Path

from studio_console.constants import (
    ENV_CLASS_IDENTITY,
    ENV_CLASS_OPERATIONAL,
    ENV_CLASSES,
    ENV_SECTIONS,
    SCALE_PROFILES,
    SECRET_KEYS,
)

REPO = Path(__file__).parent.parent

ENV_EXAMPLE_RE = re.compile(r"^#? ?([A-Z][A-Z0-9_]*)=")
COMPOSE_VAR_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
CODE_LITERAL_RE = re.compile(r"[\"']((?:SHS|POSTGRES|CONSOLE|CLOUDFLARE|COMPOSE)_[A-Z0-9_]+)[\"']")


def _env_example_vars() -> set[str]:
    text = (REPO / "templates" / ".env.example").read_text()
    return {m.group(1) for line in text.splitlines() if (m := ENV_EXAMPLE_RE.match(line))}


def _compose_vars() -> set[str]:
    return set(COMPOSE_VAR_RE.findall((REPO / "docker-compose.yml").read_text()))


def _code_literal_vars() -> set[str]:
    found: set[str] = set()
    for path in (REPO / "studio_console").rglob("*.py"):
        if path.name == "constants.py":
            continue
        found.update(CODE_LITERAL_RE.findall(path.read_text()))
    return found


def _universe() -> set[str]:
    universe = _env_example_vars() | _compose_vars() | _code_literal_vars()
    for keys in ENV_SECTIONS.values():
        universe.update(keys)
    universe.update(SECRET_KEYS)
    universe.update(SCALE_PROFILES)
    return universe


def test_every_var_is_classified():
    missing = sorted(_universe() - set(ENV_CLASSES))
    assert not missing, f"unclassified env vars, add to ENV_CLASSES: {missing}"


def test_no_dead_manifest_entries():
    dead = sorted(set(ENV_CLASSES) - _universe())
    assert not dead, f"ENV_CLASSES entries no repo surface references: {dead}"


def test_classes_are_valid():
    bad = {k: v for k, v in ENV_CLASSES.items() if v not in (ENV_CLASS_IDENTITY, ENV_CLASS_OPERATIONAL)}
    assert not bad, f"invalid lifecycle class: {bad}"


def test_ruling_pins():
    identity = {"SHS_PUBLIC_API_URL", "SHS_WS_URL", "SHS_FRONTEND_URL", "SHS_PUBLIC_BASE_URL"}
    operational = {
        "SHS_API_HOSTNAME",
        "SHS_ADMIN_PASSWORD",
        "SHS_WORKER_SHARED_SECRET",
        "SHS_COMMUNITY_SOURCE",
        "SHS_PLUS_SOURCE",
    }
    for var in identity:
        assert ENV_CLASSES[var] == ENV_CLASS_IDENTITY, var
    for var in operational:
        assert ENV_CLASSES[var] == ENV_CLASS_OPERATIONAL, var
