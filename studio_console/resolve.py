# studio_console/resolve.py
"""Configuration resolution: explicit > profile > persisted .env > default.

Identity vars invert the last two so a wizard-set value is never clobbered.
The resolved dict is written to the workspace .env, the only surface anything
reads at runtime. Ambient process env is never an input; conflicting exports
refuse boot by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .constants import ENV_CLASS_IDENTITY, ENV_CLASSES, TRANSIENT_VARS
from .env import mask_value, read_env, write_env
from .tui import error, info

SOURCE_EXPLICIT = "explicit"
SOURCE_PROFILE = "profile"
SOURCE_PERSISTED = ".env"
SOURCE_DEFAULT = "default"

AMBIENT_PREFIXES = ("SHS_", "POSTGRES_")


@dataclass
class Resolved:
    name: str
    value: str
    source: str
    losers: list[tuple[str, str]] = field(default_factory=list)


def load_profile(path: Path) -> dict[str, str]:
    """Parse a secrets profile: KEY=VALUE lines, comments and empty values skipped."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and value.strip():
            data[key.strip()] = value.strip()
    return data


def _tiers(name: str) -> list[str]:
    if ENV_CLASSES.get(name, "operational") == ENV_CLASS_IDENTITY:
        return [SOURCE_EXPLICIT, SOURCE_PERSISTED, SOURCE_PROFILE, SOURCE_DEFAULT]
    return [SOURCE_EXPLICIT, SOURCE_PROFILE, SOURCE_PERSISTED, SOURCE_DEFAULT]


def resolve_env(
    explicit: Mapping[str, str],
    profile: Mapping[str, str],
    persisted: Mapping[str, str],
    defaults: Mapping[str, str],
) -> dict[str, Resolved]:
    inputs = {
        SOURCE_EXPLICIT: explicit,
        SOURCE_PROFILE: profile,
        SOURCE_PERSISTED: persisted,
        SOURCE_DEFAULT: defaults,
    }
    resolved: dict[str, Resolved] = {}
    for name in {k for tier in inputs.values() for k, v in tier.items() if v}:
        candidates = [
            (source, inputs[source][name])
            for source in _tiers(name)
            if inputs[source].get(name)
        ]
        winner_source, winner_value = candidates[0]
        losers = [(s, v) for s, v in candidates[1:] if v != winner_value]
        resolved[name] = Resolved(name, winner_value, winner_source, losers)
    return resolved


def effective(resolved: Mapping[str, Resolved]) -> dict[str, str]:
    return {name: r.value for name, r in resolved.items()}


def ambient_conflicts(
    environ: Mapping[str, str],
    effective_env: Mapping[str, str],
    allow: frozenset[str] = frozenset(),
) -> list[str]:
    """Names of SHS_*/POSTGRES_* exports that disagree with the resolved config.

    Compose gives the shell precedence over --env-file, so a live export wins
    interpolation even though the console never consumes it; any disagreement
    must refuse boot.
    """
    return sorted(
        name
        for name, value in environ.items()
        if name.startswith(AMBIENT_PREFIXES)
        and name not in allow
        and value != effective_env.get(name)
    )


def resolve_boot(
    env_file: Path,
    profile_path: Path | None,
    explicit: Mapping[str, str],
    environ: Mapping[str, str],
    persist: bool = True,
) -> dict[str, str] | None:
    """Resolve boot config, refuse on ambient conflicts, persist and print.

    Returns the effective env, or None when a conflicting export refuses boot.
    *persist* False (full/core launches) resolves without touching the
    workspace .env: the entrypoint owns that file (root 0600) and resolved
    values travel as container env instead.
    """
    try:
        persisted = read_env(env_file) if env_file.exists() else {}
    except PermissionError:
        persisted = {}
        persist = False
    profile = load_profile(profile_path) if profile_path else {}
    resolved = resolve_env(explicit, profile, persisted, {})
    eff = effective(resolved)
    conflicts = ambient_conflicts(environ, eff)
    if conflicts:
        error("Ambient environment is not a configuration input. Refusing to boot.")
        for name in conflicts:
            error(f"  conflicting export: {name} (unset it, or put the value in a profile or flag)")
        return None
    # First boot has no .env yet; the launcher/entrypoint owns creating it, so
    # resolved values travel as process/env-file inputs instead of a pre-write
    # that would suppress the entrypoint's generate-once seeding. Transient
    # bootstrap creds are consumed in-process and never persisted.
    changed = {
        k: v
        for k, v in eff.items()
        if persisted.get(k) != v and k not in TRANSIENT_VARS
    }
    if changed and persist and env_file.exists():
        write_env(env_file, changed)
    info(f"Configuration ({env_file}):")
    for line in format_provenance(resolved):
        info(f"  {line}")
    return eff


def format_provenance(
    resolved: Mapping[str, Resolved],
    mask: Callable[[str, str], str] = mask_value,
) -> list[str]:
    lines: list[str] = []
    for name in sorted(resolved):
        r = resolved[name]
        flag = "  WINNER" if r.losers else ""
        lines.append(f"{name} = {mask(name, r.value)} ({r.source}){flag}")
        for source, value in r.losers:
            lines.append(f"  {source} = {mask(name, value)}  OVERRIDDEN")
    return lines
