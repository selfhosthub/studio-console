# studio_console/commands_launch.py
"""Host-side launcher for the single-image `full` shape."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .env import run, run_quiet
from .tui import (
    _bold,
    _cyan,
    _dim,
    _prompt,
    _prompt_password,
    error,
    heading,
    info,
    ok,
    warn,
)

MANIFEST_PATH_IN_IMAGE = "/app/contracts/launch-manifest.json"
DEFAULT_IMAGE = "ghcr.io/selfhosthub/studio-full"
DEFAULT_TAG = "latest"
CONTAINER_NAME = "studio-full"
STATE_DIR = Path.home() / ".studio-full"
STATE_FILE = STATE_DIR / ".console-state"


def _read_manifest(image_ref: str) -> dict | None:
    """Read the launch manifest from inside the image."""
    rc, out = run_quiet(
        ["docker", "run", "--rm", "--entrypoint", "cat", image_ref, MANIFEST_PATH_IN_IMAGE],
        timeout=60,
    )
    if rc != 0:
        error(f"Could not read launch manifest from {image_ref}")
        if out:
            print(f"  {_dim(out)}")
        return None
    try:
        return json.loads(out)["shapes"]["full"]
    except (json.JSONDecodeError, KeyError) as e:
        error(f"Launch manifest malformed: {e}")
        return None


def _load_state() -> dict:
    """Read persisted supervisor creds (KEY=VALUE lines). Empty if absent."""
    if not STATE_FILE.exists():
        return {}
    state: dict[str, str] = {}
    for line in STATE_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            state[k.strip()] = v.strip()
    return state


def _save_state(state: dict) -> None:
    """Persist creds atomically at 0600."""
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    content = "\n".join(f"{k}={v}" for k, v in state.items()) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        os.chmod(fd, 0o600)
        os.write(fd, content.encode())
        os.close(fd)
        os.replace(tmp, str(STATE_FILE))
        if os.stat(STATE_FILE).st_mode & 0o777 != 0o600:
            os.chmod(STATE_FILE, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _ensure_creds(manifest: dict) -> dict:
    """Return required_env creds, prompting once and persisting if not yet stored."""
    required = [e["name"] for e in manifest.get("required_env", [])]
    state = _load_state()
    if all(k in state and state[k] for k in required):
        return {k: state[k] for k in required}

    info("Supervisor credentials are required on every boot and are not stored in the")
    info("container — console keeps them so you don't re-enter them each launch.")
    print()
    for entry in manifest.get("required_env", []):
        name = entry["name"]
        if state.get(name):
            continue
        if entry.get("secret"):
            state[name] = _prompt_password(entry.get("description", name).split(".")[0])
        else:
            state[name] = _prompt(f"{name}")
    _save_state(state)
    return {k: state[k] for k in required}


def _build_run_cmd(manifest: dict, image_ref: str, creds: dict, workspace: Path) -> list[str]:
    """Assemble `docker run` from the manifest."""
    cmd = ["docker", "run", "-d", "--name", CONTAINER_NAME]
    for port in manifest.get("ports", []):
        c = port["container"]
        cmd += ["-p", f"{c}:{c}"]
    vol = manifest["volumes"][0]["container_path"]
    cmd += ["-v", f"{workspace}:{vol}"]
    for name, value in creds.items():
        cmd += ["-e", f"{name}={value}"]
    cmd.append(image_ref)
    return cmd


def cmd_launch_full(context: str, tag: str | None = None, workspace: Path | None = None) -> bool:
    """Launch the full single-image deployment and exec into its console."""
    if context != "host":
        error("launch-full runs on the host — you appear to be inside a container.")
        return False
    if run_quiet(["docker", "version"])[0] != 0:
        error("Docker is not available. Install Docker and retry.")
        return False

    heading("Launch Studio (full)")
    tag = tag or DEFAULT_TAG
    image_ref = f"{DEFAULT_IMAGE}:{tag}"

    existing = run_quiet(["docker", "ps", "-aq", "--filter", f"name=^{CONTAINER_NAME}$"])[1]
    if existing:
        warn(f"Container '{CONTAINER_NAME}' already exists.")
        print(f"  {_dim('Connect:')} {_bold(f'docker exec -it {CONTAINER_NAME} studio-console')}")
        return False

    manifest = _read_manifest(image_ref)
    if manifest is None:
        return False

    workspace = workspace or (Path.home() / ".studio")

    creds = _ensure_creds(manifest)
    cmd = _build_run_cmd(manifest, image_ref, creds, workspace)

    info(f"Starting {image_ref} (data: {workspace}) ...")
    if run(cmd, check=False).returncode != 0:
        error("docker run failed.")
        return False

    ok(f"{CONTAINER_NAME} started.")
    ui_port = next((p["container"] for p in manifest["ports"] if p.get("name") == "ui"), 3000)
    print(f"  {_dim('UI:')} {_cyan(f'http://localhost:{ui_port}')}")
    print(f"  {_dim('Console:')} {_bold(f'docker exec -it {CONTAINER_NAME} studio-console')}")
    print()

    api_healthy = _wait_healthy(manifest)

    if api_healthy:
        _bootstrap_admin(workspace)

    info("Opening in-container console ...")
    run(["docker", "exec", "-it", CONTAINER_NAME, "studio-console"], check=False, timeout=None)
    return True


def _bootstrap_admin(workspace: Path) -> None:
    """First-boot super admin creation via full's exec plumbing."""
    env_file = workspace / ".env"
    if not env_file.exists():
        warn("Workspace .env not found — skipping admin bootstrap.")
        return

    from .commands import _bootstrap_first_admin, _full_plan

    _bootstrap_first_admin(env_file, _full_plan(CONTAINER_NAME))


def _curl_status(url: str) -> int:
    """Return HTTP status code for url, or 0 if unreachable."""
    rc, out = run_quiet(["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=5)
    try:
        return int(out.strip())
    except ValueError:
        return 0


def _wait_healthy(manifest: dict, attempts: int = 30, interval: int = 3) -> bool:
    """Poll API + UI ports on first boot; flag a non-200 UI. Returns API health."""
    import time

    api_port = next((p["container"] for p in manifest["ports"] if p.get("name") == "api"), 8000)
    ui_port = next((p["container"] for p in manifest["ports"] if p.get("name") == "ui"), 3000)
    api_url = f"http://localhost:{api_port}/health"
    ui_url = f"http://localhost:{ui_port}"

    info("Waiting for the stack to come up (first boot initializes Postgres) ...")
    api_ok = ui_status = 0
    for _ in range(attempts):
        if api_ok != 200:
            api_ok = _curl_status(api_url)
        ui_status = _curl_status(ui_url)
        if api_ok == 200 and ui_status == 200:
            ok("API and UI healthy.")
            return True
        time.sleep(interval)

    if api_ok == 200:
        ok(f"API healthy (:{api_port}).")
    else:
        warn(f"API not healthy (:{api_port}) — check: docker exec {CONTAINER_NAME} supervisorctl status")
    if ui_status == 200:
        ok(f"UI healthy (:{ui_port}).")
    else:
        warn(f"UI returned {ui_status or 'no response'} (:{ui_port}) — SSR may be failing.")
        print(f"  {_dim('Logs:')} {_bold(f'docker exec {CONTAINER_NAME} supervisorctl tail ui stderr')}")
    return api_ok == 200
