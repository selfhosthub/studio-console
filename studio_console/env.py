# studio_console/env.py
"""Environment / .env I/O, context detection, and shell helpers.

Zero TUI dependencies - this module never imports menu widgets.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, NoReturn

from .constants import (
    APP_DB_ROLE,
    COMPONENT_TO_IMAGE,
    ENV_SECTIONS,  # noqa: F401 - re-exported for callers
    IMAGE_BUILD_CONFIG,
    SECRET_KEYS,
    SECRET_PATTERNS,
)

# ---------------------------------------------------------------------------
# Color output (minimal, no TUI)
# ---------------------------------------------------------------------------

_IS_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _IS_TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


def _red(t: str) -> str:
    return _c("0;31", t)


def _green(t: str) -> str:
    return _c("0;32", t)


# ---------------------------------------------------------------------------
# Print helpers (not TUI widgets - plain stderr/stdout)
# ---------------------------------------------------------------------------


def ok(msg: str) -> None:
    print(f"{_green('✓')} {msg}")


def error(msg: str) -> None:
    print(f"{_red('✗')} {msg}", file=sys.stderr)


def fatal(msg: str) -> NoReturn:
    error(msg)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Context detection
# ---------------------------------------------------------------------------


def detect_context() -> str:
    """Detect deployment context: runpod, container, or host."""
    if os.getenv("RUNPOD_POD_ID"):
        return "runpod"
    if os.path.exists("/.dockerenv"):
        return "container"
    return "host"


def _workspace_dir(context: str) -> Path:
    """Return the workspace root.

    Honors SHS_WORKSPACE_DIR (preferred) then SHS_WORKSPACE_HOST (legacy)
    from the shell environment. Defaults: ~/.studio (host) or /workspace
    (container/runpod). .env sits at the workspace root; data subdirs
    (db/, storage/, models/, backups/) sit alongside it.
    """
    if context == "host":
        raw = os.environ.get("SHS_WORKSPACE_DIR") or os.environ.get(
            "SHS_WORKSPACE_HOST"
        )
        if raw:
            return Path(os.path.expanduser(raw))
        return Path.home() / ".studio"
    return Path("/workspace")


def env_path(context: str) -> Path:
    """Return the .env file path — always at the workspace root."""
    return _workspace_dir(context) / ".env"


def detect_legacy_layout(workspace: Path) -> str:
    """Detect pre-current layouts that need migration.

    Returns:
      "split":   workspace/private/.env exists (the failed split-layout attempt)
      "flat":    workspace/postgres/ or workspace/orgs/ exists at root (pre-split)
      "none":    current layout (or empty)
    """
    if (workspace / "private" / ".env").exists():
        return "split"
    if (workspace / "postgres").exists() or (workspace / "orgs").exists():
        return "flat"
    return "none"


def _root_from_env(env_file: Path | None, key: str) -> Path | None:
    if env_file is not None and env_file.exists():
        val = read_env(env_file).get(key, "").strip()
        if val:
            return Path(os.path.expanduser(val))
    return None


# Data roots — each defaults to a subdir of the workspace; each is
# independently repointable for cloud (CloudSQL, GCS, RunPod network volumes).
def db_data(context: str, env_file: Path | None = None) -> Path:
    return _root_from_env(env_file, "SHS_DB_DATA") or (_workspace_dir(context) / "db")


def storage_root(context: str, env_file: Path | None = None) -> Path:
    return _root_from_env(env_file, "SHS_STORAGE_ROOT") or (
        _workspace_dir(context) / "storage"
    )


def models_root(context: str, env_file: Path | None = None) -> Path:
    return _root_from_env(env_file, "SHS_MODELS_ROOT") or (
        _workspace_dir(context) / "models"
    )


def backup_root(context: str, env_file: Path | None = None) -> Path:
    return _root_from_env(env_file, "CONSOLE_BACKUP_ROOT") or (
        _workspace_dir(context) / "backups"
    )


_SHAPE_FILE = Path("/etc/studio-shape")
_VALID_SHAPES = ("split", "core", "full")


def detect_shape(env_file: Path | None = None) -> str | None:
    """Return the deployment shape from .env or /etc/studio-shape, else None."""
    if env_file is not None and env_file.exists():
        shape = read_env(env_file).get("SHS_DEPLOYMENT_SHAPE", "").strip()
        if shape in _VALID_SHAPES:
            return shape
    if _SHAPE_FILE.exists():
        try:
            shape = _SHAPE_FILE.read_text().strip()
        except OSError:
            return None
        if shape in _VALID_SHAPES:
            return shape
    return None


# ---------------------------------------------------------------------------
# .env I/O
# ---------------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def derive_url_vars(
    public_url: str, api_public_url: str = "", nginx_port: str = "80"
) -> dict[str, str]:
    """Derive the five browser/SSR/CORS URL vars from a public domain."""
    public_url = public_url.strip()
    api_public_url = api_public_url.strip()
    localhost_origins = "http://localhost" if nginx_port == "80" else f"http://localhost:{nginx_port}"

    if public_url.startswith("https://"):
        api_host = api_public_url if api_public_url.startswith("https://") else public_url
        api_url = api_host
        ws_url = api_host.replace("https://", "wss://")
        frontend_url = public_url
        origins = [localhost_origins, public_url]
        if api_public_url and api_public_url != public_url:
            origins.append(api_public_url)
        cors_origins = ",".join(origins)
    else:
        nginx_base = f"http://localhost:{nginx_port}"
        api_url = nginx_base
        ws_url = f"ws://localhost:{nginx_port}"
        frontend_url = nginx_base
        cors_origins = localhost_origins

    return {
        "SHS_API_BASE_URL": api_url,
        "SHS_PUBLIC_API_URL": api_url,
        "SHS_WS_URL": ws_url,
        "SHS_FRONTEND_URL": frontend_url,
        "SHS_CORS_ORIGINS": cors_origins,
    }


def derive_app_db_url(privileged_url: str, password: str | None = None) -> str:
    """Return the restricted-role URL: same DSN as *privileged_url*, shs_app creds.

    The API's bootstrap creates/refreshes the role from these credentials on
    every boot, so a fresh password here is safe — it never needs out-of-band
    registration.
    """
    import secrets
    from urllib.parse import urlsplit

    parts = urlsplit(privileged_url)
    host = parts.hostname or "postgres"
    port = f":{parts.port}" if parts.port else ""
    query = f"?{parts.query}" if parts.query else ""
    password = password or secrets.token_hex(24)
    return f"{parts.scheme}://{APP_DB_ROLE}:{password}@{host}{port}{parts.path}{query}"


def write_env(path: Path, data: dict[str, str]) -> None:
    """Atomic write of a .env file, preserving comments from existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # If the file exists, update values in place preserving structure
    if path.exists():
        lines = path.read_text().splitlines()
        written_keys: set[str] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in data:
                    new_lines.append(f"{key}={data[key]}")
                    written_keys.add(key)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        # Append any new keys not in the original file
        for key, value in data.items():
            if key not in written_keys:
                new_lines.append(f"{key}={value}")
        content = "\n".join(new_lines) + "\n"
    else:
        content = "\n".join(f"{k}={v}" for k, v in data.items()) + "\n"

    # Atomic write: write to temp with 600 perms, then rename
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".env.tmp")
    closed = False
    try:
        os.chmod(fd, 0o600)
        os.write(fd, content.encode())
        os.close(fd)
        closed = True
        os.replace(tmp, str(path))
        # Verify the secret file really landed at 0o600 — an overriding umask,
        # prior file, or odd filesystem could leave it world-readable.
        mode = os.stat(path).st_mode & 0o777
        if mode != 0o600:
            os.chmod(path, 0o600)
    except BaseException:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def promote_runpod_secrets(env_file: Path) -> list[str]:
    """Copy RUNPOD_SHS_* env vars into .env without overwriting existing keys."""
    prefix = "RUNPOD_SHS_"
    existing = read_env(env_file)
    new_keys: dict[str, str] = {}
    promoted: list[str] = []
    for env_key, value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        target_key = "SHS_" + env_key[len(prefix):]
        if target_key in existing:
            continue
        new_keys[target_key] = value
        promoted.append(target_key)
    if new_keys:
        merged = {**existing, **new_keys}
        write_env(env_file, merged)
    return promoted


def set_env_value(path: Path, key: str, value: str) -> None:
    """Set a single key in the .env file."""
    data = read_env(path)
    data[key] = value
    write_env(path, data)


def unset_env_values(path: Path, keys: list[str]) -> None:
    """Remove keys from the .env file entirely (used to scrub transient
    bootstrap credentials once they live in the DB)."""
    data = read_env(path)
    changed = False
    for k in keys:
        if k in data:
            del data[k]
            changed = True
    if changed:
        write_env(path, data)


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    """Check if a key should be treated as a secret."""
    if key in SECRET_KEYS:
        return True
    upper = key.upper()
    return any(p in upper for p in SECRET_PATTERNS)


# user:password@ inside any URL-shaped value (e.g. SHS_DATABASE_URL)
_URL_CRED_RE = re.compile(r"(://[^:/@]+:)[^@]+(@)")


def mask_value(key: str, value: str) -> str:
    """Mask secret values for display."""
    if _is_secret_key(key) and value:
        if len(value) <= 4:
            return "***"
        return value[:4] + "***"
    if "://" in value and "@" in value:
        return _URL_CRED_RE.sub(r"\1***\2", value)
    return value


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str], check: bool = True, capture: bool = False, timeout: int | None = 30
) -> subprocess.CompletedProcess[str]:
    """Run a shell command."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def run_quiet(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command, return (returncode, stdout+stderr). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = r.stdout.strip()
        if r.returncode != 0 and r.stderr.strip():
            output = output + "\n" + r.stderr.strip() if output else r.stderr.strip()
        return r.returncode, output
    except Exception:
        return 1, ""


def _package_root() -> Path:
    """Return the studio-console package root (where docker-compose.yml lives).

    Works for all three install methods:
      - pip install   → site-packages/studio_console/../  (one level up from package)
      - curl install  → ~/.studio-console/studio_console/../
      - dev/monorepo  → deploy/studio_console/../
    """
    return Path(__file__).resolve().parent.parent


def _studio_dir(env_file: Path) -> Path:
    """Return the operator's Studio working directory (~/.studio by default)."""
    return env_file.parent


def compose_cmd(env_file: Path) -> list[str]:
    """Return the base docker compose command for production.

    Resolves docker-compose.yml from the operator's studio dir first
    (copied there on first run), then falls back to the package root.
    Worker profiles are activated via COMPOSE_PROFILES in the .env file.
    Includes the generated override file when it exists (nginx load balancing).
    """
    studio_dir = _studio_dir(env_file)
    compose_file = studio_dir / "docker-compose.yml"
    if not compose_file.exists():
        # Fall back to bundled copy (pre-first-run or pip install edge case)
        compose_file = _package_root() / "docker-compose.yml"
    cmd = ["docker", "compose", "-f", str(compose_file), "--env-file", str(env_file)]
    override = studio_dir / "docker-compose.override.yml"
    if override.exists():
        cmd += ["-f", str(override)]
    return cmd


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------


def validate_password(password: str) -> tuple[bool, str]:
    """Validate password strength."""
    if len(password) < 8:
        return False, "Must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return False, "Must contain an uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Must contain a lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Must contain a digit"
    if all(c.isalnum() for c in password):
        return False, "Must contain a special character (!@#$%^&*...)"
    return True, ""


# ---------------------------------------------------------------------------
# Repo / image helpers
# ---------------------------------------------------------------------------


def _find_repo_root(env_file: Path | None = None) -> Path | None:
    """Return the monorepo root (studio-app checkout).

    Resolution order:
      1. CONSOLE_REPO_ROOT shell environment variable
      2. CONSOLE_REPO_ROOT key in the .env file
      3. None — callers must handle this gracefully

    In standalone installs (pip/curl/brew) without CONSOLE_REPO_ROOT set,
    this returns None and build-from-source is unavailable.
    """
    # 1. Shell environment
    shell_val = os.environ.get("CONSOLE_REPO_ROOT", "").strip()
    if shell_val:
        p = Path(shell_val).expanduser().resolve()
        if p.is_dir():
            return p

    # 2. .env file
    if env_file and env_file.exists():
        env_val = read_env(env_file).get("CONSOLE_REPO_ROOT", "").strip()
        if env_val:
            p = Path(env_val).expanduser().resolve()
            if p.is_dir():
                return p

    return None


def _images_from_env(env_file: Path) -> list[str]:
    """Determine which images to build based on CONSOLE_COMPONENTS in .env."""
    env_data = read_env(env_file) if env_file.exists() else {}
    components_str = env_data.get("CONSOLE_COMPONENTS", "")
    if not components_str:
        return list(IMAGE_BUILD_CONFIG)  # No config - build all

    components = [c.strip() for c in components_str.split(",")]
    images: list[str] = []
    seen: set[str] = set()
    for comp in components:
        img = COMPONENT_TO_IMAGE.get(comp)
        if img and img not in seen:
            images.append(img)
            seen.add(img)
    return images if images else list(IMAGE_BUILD_CONFIG)


# ---------------------------------------------------------------------------
# Env validation
# ---------------------------------------------------------------------------


def _env_example_path() -> Path | None:
    """Find .env.example — bundled in the package, or in the monorepo deploy/ dir."""
    # Bundled copy (pip/curl/brew installs and standalone repo)
    bundled = _package_root() / "templates" / ".env.example"
    if bundled.exists():
        return bundled
    # Monorepo checkout fallback
    repo = _find_repo_root()
    if repo:
        example = repo / "deploy" / ".env.example"
        if example.exists():
            return example
    return None


def _validate_env(env_file: Path) -> None:
    """Check .env against .env.example. Fatal if required keys are missing."""
    if not env_file.exists():
        fatal(f"No .env found at {env_file}. Run studio-console first.")

    example_file = _env_example_path()
    if not example_file:
        return  # Can't validate without the example file

    required = read_env(example_file)
    actual = read_env(env_file)

    # Keys that are in the example with a value are required
    missing: list[str] = []
    for key, val in required.items():
        if val and key not in actual:
            missing.append(key)

    if missing:
        error(f"Missing required env vars ({len(missing)}):")
        for key in missing:
            default = required[key]
            print(f"  {key}={default}")
        print()
        answer = input("Add missing vars with default values? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            for key in missing:
                set_env_value(env_file, key, required[key])
            ok(f"Added {len(missing)} missing var(s) to {env_file}")
        else:
            fatal(
                "Cannot start with missing env vars. Add them manually or re-run the wizard."
            )


# ---------------------------------------------------------------------------
# Service introspection
# ---------------------------------------------------------------------------


def _get_running_services(
    context: str, env_file: Path, use_container_names: bool = False
) -> list[str]:
    """Get list of all compose/supervisor services (any state).

    When *use_container_names* is True, returns container names so scaled
    instances show individually (e.g. studio-worker-general-1, -2, -3).
    Otherwise returns compose service names (e.g. worker-general).
    """
    if context == "host":
        rc, out = run_quiet(
            compose_cmd(env_file) + ["ps", "-a", "--format", "json"],
            timeout=15,
        )
        if rc != 0 or not out:
            return []
        key = "Name" if use_container_names else "Service"
        services: list[str] = []
        for line in out.strip().splitlines():
            try:
                svc = json.loads(line)
                name = svc.get(key, svc.get("Service", ""))
                if name and name not in services:
                    services.append(name)
            except json.JSONDecodeError:
                pass
        return services
    else:
        rc, out = run_quiet(["supervisorctl", "status"], timeout=10)
        if not out:
            return []
        # All services regardless of state (matches host 'ps -a' and the
        # docstring) — otherwise 'Start one' can't list a stopped service.
        # Keep only real status lines (state token in column 2) so a supervisord
        # error banner / traceback isn't mistaken for a service name.
        states = {
            "RUNNING", "STOPPED", "STARTING", "STOPPING",
            "BACKOFF", "EXITED", "FATAL", "UNKNOWN",
        }
        services: list[str] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in states:
                services.append(parts[0])
        return services
