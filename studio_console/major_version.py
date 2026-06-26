# studio_console/major_version.py
"""Major-version boundary detection.

Studio's API refuses to start when the database was migrated by a prior major
version (see studio-app api/scripts/bootstrap.py:_check_major_version_compatibility).
This module lets the console detect that condition pre-start, so install,
upgrade, and restore can block with a clear explanation instead of letting
the operator hit a generic "API not responding" timeout.

The bundled `data/known_baselines.json` maps each major version to its alembic
baseline revision(s). A major may have more than one baseline because studio-app
periodically squashes-and-stamps its migration chain to a new baseline; each
historical baseline a restorable DB might sit on must be listed. It is re-synced
from studio-app at console release time. Staleness is accepted: between a Studio
major release (or squash) and the next console release, prior-major detection
falls back to log-scraping the API container after a failed start.
"""

from __future__ import annotations

import json
import re
import subprocess
from importlib import resources
from pathlib import Path

from .env import compose_cmd, read_env


# Possible outcomes of a major-boundary check.
#   ok                 — DB is on the target major, or we can't prove otherwise
#   prior_major        — DB is on an older major than the target — BLOCK
#   unknown_future     — DB rev is a known baseline for a newer major — BLOCK
#   fresh              — alembic_version absent / postgres down — proceed
#   indeterminate_tag  — target tag is non-semver (latest, main, sha) — proceed with notice
Result = str


_SEMVER_RE = re.compile(r"^(\d+)\.\d+\.\d+")
_FATAL_RE = re.compile(r"FATAL: Studio cannot start\.")


def load_baselines() -> dict[int, list[str]]:
    """Read the bundled {major: [baseline_revisions]} map. Empty dict on any error.

    Accepts both the historical scalar form ({"1": "abc"}) and the list form
    ({"1": ["abc", "def"]}); scalars are normalized to single-element lists so a
    major can carry multiple baselines after a squash-and-stamp.
    """
    try:
        text = (
            resources.files("studio_console.data")
            .joinpath("known_baselines.json")
            .read_text()
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[int, list[str]] = {}
    for k, v in raw.items():
        try:
            major = int(k)
        except (TypeError, ValueError):
            continue
        revs = v if isinstance(v, list) else [v]
        normalized = [str(r) for r in revs if r]
        if normalized:
            out[major] = normalized
    return out


def parse_target_major(version_tag: str) -> int | None:
    """Leading semver major from a tag, or None for non-semver (latest, main, sha)."""
    if not version_tag:
        return None
    m = _SEMVER_RE.match(version_tag.strip())
    return int(m.group(1)) if m else None


def read_db_revision(env_file: Path, context: str) -> str | None:
    """Query alembic_version.version_num. Context-aware.

    host       — exec into the postgres compose service.
    container  — psql directly (postgres on localhost in Full,
    /runpod      external via env in Core).

    Returns None for any of: postgres not running, DB absent, alembic_version
    table absent, query failed. Callers cannot distinguish — they all mean
    "no prior-major condition observable from here".
    """
    env_data = read_env(env_file)
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    sql = "SELECT version_num FROM alembic_version LIMIT 1;"
    if context == "host":
        cmd = compose_cmd(env_file) + [
            "exec", "-T", "postgres",
            "psql", "-U", pg_user, "-d", "selfhost_studio", "-tAc", sql,
        ]
    else:
        cmd = ["psql", "-U", pg_user, "-d", "selfhost_studio", "-tAc", sql]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    rev = result.stdout.strip()
    return rev or None


def classify_revision(
    rev: str | None,
    target_major: int | None,
    baselines: dict[int, list[str]] | None = None,
) -> tuple[Result, dict]:
    """Classify an alembic revision against the target major.

    Pure: takes a revision string and a target major, returns the outcome.
    Used both for the live-DB check (via classify_db) and the restore preflight
    (where the revision comes from the backup file).
    """
    if baselines is None:
        baselines = load_baselines()
    info: dict = {
        "baselines": baselines,
        "target_major": target_major,
        "db_revision": rev,
        "db_major": None,
    }

    if target_major is None:
        return "indeterminate_tag", info
    if rev is None:
        return "fresh", info

    db_major: int | None = None
    for major, baseline_revs in baselines.items():
        if rev in baseline_revs:
            db_major = major
            break
    info["db_major"] = db_major

    if db_major is None:
        # Non-baseline revision. Could be a normal mid-chain rev (fine) or an
        # unknown-future rev (not fine). Without the running API's full chain
        # we can't tell — defer to API's own guardrail. Don't block here.
        return "ok", info
    if db_major < target_major:
        return "prior_major", info
    if db_major > target_major:
        return "unknown_future", info
    return "ok", info


def classify_db(env_file: Path, target_major: int | None, context: str) -> tuple[Result, dict]:
    """Read the live DB revision and classify it. Thin wrapper over classify_revision."""
    return classify_revision(read_db_revision(env_file, context), target_major)


def render_block(result: Result, info: dict, action: str) -> None:
    """Print the shared major-boundary block. No-op for ok/fresh/indeterminate_tag."""
    from .tui import _bold, _yellow

    old = info.get("db_major")
    new = info.get("target_major")
    rev = info.get("db_revision") or "unknown"

    if result == "prior_major":
        print()
        print(_yellow(_bold("⚠  Studio major-version boundary")))
        print()
        print(f"   This database was migrated by Studio v{old} (revision {rev}).")
        print(f"   You are trying to {action} Studio v{new}.")
        print()
        print(f"   Studio v{new} cannot start against a v{old} database, and")
        print(f"   no automated migration tool exists yet.")
        print()
        print(f"   To proceed:")
        print(f"     • Stay on v{old}: set SHS_STUDIO_VERSION to a v{old} tag in")
        print(f"       ~/.studio/.env, then run 'studio-console start'.")
        print(f"     • Or restore a backup taken under v{new}.")
        print()
    elif result == "unknown_future":
        print()
        print(_yellow(_bold("⚠  Unknown database schema")))
        print()
        print(f"   The database is at revision {rev}, which this Studio version")
        print(f"   (v{new}) doesn't recognize.")
        print()
        print(f"   The database was likely migrated by a NEWER Studio version")
        print(f"   than the one you are about to run.")
        print()
        print(f"   To proceed: reinstall the newer Studio image, or restore a")
        print(f"   backup compatible with v{new}.")
        print()


def indeterminate_notice(tag: str) -> None:
    """Print the one-line notice when SHS_STUDIO_VERSION isn't semver."""
    from .tui import warn

    warn(
        f"Cannot determine target major from tag '{tag}' — "
        f"skipping major-version compatibility check."
    )


def scrape_guardrail_failure(env_file: Path, context: str) -> bool:
    """True if recent API logs show the bootstrap guardrail FATAL.

    host       — `docker compose logs api`.
    container  — `supervisorctl tail api stdout` (bootstrap.py prints to stdout).
    /runpod

    Backstop for when the bundled baselines map is stale (a Studio major
    shipped after this console release). Used by config_menu and cmd_health
    to distinguish "API down: prior-major" from "API down: unknown".
    """
    if context == "host":
        cmd = compose_cmd(env_file) + ["logs", "--tail=50", "--no-color", "api"]
    else:
        # supervisorctl tail returns the most recent bytes for the given channel.
        # Bootstrap's FATAL goes to stdout via print(); -10000 covers the typical
        # crash-loop window without unbounded reads.
        cmd = ["supervisorctl", "tail", "-10000", "api", "stdout"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return False
    haystack = (result.stdout or "") + (result.stderr or "")
    return bool(_FATAL_RE.search(haystack))


def check_and_block(
    env_file: Path,
    target_major: int | None,
    action: str,
    context: str,
    target_tag: str = "",
) -> bool:
    """Run the live-DB check and render output. Return True if the caller should block.

    Returns False for ok / fresh (proceed normally) and for indeterminate_tag
    (after printing the notice). Returns True for prior_major / unknown_future
    (after printing the shared block) — caller must not proceed.
    """
    result, info = classify_db(env_file, target_major, context)
    if result == "indeterminate_tag":
        indeterminate_notice(target_tag)
        return False
    if result in ("prior_major", "unknown_future"):
        render_block(result, info, action)
        return True
    return False
