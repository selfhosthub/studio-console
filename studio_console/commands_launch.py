# studio_console/commands_launch.py
"""Host-side launcher for the single-image `full` and `core` shapes."""
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
    _interactive_single,
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

# Core (external-DB) launcher constants. Core needs a Postgres it does not
# provide; the launcher owns the optional sidecar and its lifecycle.
CORE_IMAGE = "ghcr.io/selfhosthub/studio-core"
CORE_CONTAINER_NAME = "studio-core"
CORE_STATE_DIR = Path.home() / ".studio-core"
CORE_STATE_FILE = CORE_STATE_DIR / ".console-state"
CORE_NETWORK = "studio-core-net"
CORE_PG_CONTAINER = "studio-core-pg"
CORE_PG_DB = "selfhost_studio"
CORE_PG_USER = "postgres"


def _read_manifest(image_ref: str, shape: str = "full") -> dict | None:
    """Read the launch manifest for *shape* from inside the image."""
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
        parsed = json.loads(out)
    except json.JSONDecodeError as e:
        error(f"Launch manifest malformed: {e}")
        return None
    manifest = parsed.get("shapes", {}).get(shape)
    if manifest is None:
        error(f"Launch manifest has no '{shape}' shape — image predates {shape} support.")
        return None
    # engine.image (pgvector pin) lives at top level; carry it for the sidecar.
    manifest["_engine"] = parsed.get("engine", {})
    return manifest


def _load_state(state_file: Path = STATE_FILE) -> dict:
    """Read persisted creds (KEY=VALUE lines). Empty if absent."""
    if not state_file.exists():
        return {}
    state: dict[str, str] = {}
    for line in state_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            state[k.strip()] = v.strip()
    return state


def _save_state(state: dict, state_dir: Path = STATE_DIR, state_file: Path = STATE_FILE) -> None:
    """Persist creds atomically at 0600."""
    state_dir.mkdir(mode=0o700, exist_ok=True)
    content = "\n".join(f"{k}={v}" for k, v in state.items()) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), suffix=".tmp")
    try:
        os.chmod(fd, 0o600)
        os.write(fd, content.encode())
        os.close(fd)
        os.replace(tmp, str(state_file))
        if os.stat(state_file).st_mode & 0o777 != 0o600:
            os.chmod(state_file, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _ensure_creds(
    manifest: dict,
    state_dir: Path = STATE_DIR,
    state_file: Path = STATE_FILE,
    only: list[str] | None = None,
) -> dict:
    """Return required_env creds, prompting once and persisting if not yet stored.

    *only* restricts which required_env names are handled here (the rest are
    owned by a caller-specific flow, e.g. core's DB provisioning). Returns just
    the handled keys.
    """
    entries = [e for e in manifest.get("required_env", []) if only is None or e["name"] in only]
    required = [e["name"] for e in entries]
    state = _load_state(state_file)
    if all(k in state and state[k] for k in required):
        return {k: state[k] for k in required}

    info("Supervisor credentials are required on every boot and are not stored in the")
    info("container — console keeps them so you don't re-enter them each launch.")
    print()
    for entry in entries:
        name = entry["name"]
        if state.get(name):
            continue
        env_val = os.environ.get(name, "").strip()
        if env_val:
            state[name] = env_val
        elif entry.get("secret"):
            state[name] = _prompt_password(entry.get("description", name).split(".")[0])
        else:
            state[name] = _prompt(f"{name}")
    _save_state(state, state_dir, state_file)
    return {k: state[k] for k in required}


def _ensure_workspace(workspace: Path) -> None:
    """Create the bind-mount root 0711 regardless of umask.

    Docker auto-creates a missing bind-mount root root-owned with the daemon
    umask (0700 under a 077 umask), which the container's non-root initdb can't
    traverse. mkdir's mode is umask-masked, so chmod explicitly.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace, 0o711)


# Non-secret config injected from host env via -e on first boot only; not
# re-injected on relaunch, so operator-tuned values are not clobbered.
_FIRST_BOOT_SEED_VARS = [
    "SHS_GENERAL_WORKERS",
    "SHS_TRANSFER_WORKERS",
]

# Container path the entrypoint reads the tunnel token from (consumed_secret_files
# in the launch manifest). Must match studio-app's entrypoint exactly.
CF_TOKEN_MOUNT = "/run/secrets/cf-token"


def _seed_first_boot_vars(workspace: Path, creds: dict) -> None:
    """Inject worker counts and derived public-URL vars from host env, first boot only."""
    if (workspace / ".env").exists():
        for name in _FIRST_BOOT_SEED_VARS:
            if os.environ.get(name, "").strip():
                info(f"{name}: kept existing.")
        if os.environ.get("SHS_PUBLIC_BASE_URL", "").strip():
            info("public URLs: kept existing.")
        return
    for name in _FIRST_BOOT_SEED_VARS:
        val = os.environ.get(name, "").strip()
        if val:
            creds[name] = val
            info(f"seeded {name} (first boot).")
    public_url = os.environ.get("SHS_PUBLIC_BASE_URL", "").strip()
    if public_url:
        from .env import derive_url_vars

        # Only the browser bundle vars; SHS_API_BASE_URL/CORS are excluded so SSR
        # stays on the in-container API and nginx single-origin CORS is untouched.
        urls = derive_url_vars(
            public_url,
            os.environ.get("CONSOLE_PUBLIC_API_BASE_URL", ""),
            os.environ.get("SHS_NGINX_PORT", "80"),
        )
        creds["SHS_PUBLIC_BASE_URL"] = public_url
        for key in ("SHS_PUBLIC_API_URL", "SHS_WS_URL", "SHS_FRONTEND_URL"):
            creds[key] = urls[key]
        info("seeded public URLs (first boot).")


def _cf_token_mount(workspace: Path, state_dir: Path) -> Path | None:
    """Write the token to a 0600 host file, return its path; None if no token or on relaunch."""
    token_file = state_dir / ".cf-token"
    token_file.unlink(missing_ok=True)
    token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    if not token:
        return None
    if (workspace / ".env").exists():
        info("CLOUDFLARE_TUNNEL_TOKEN: kept existing.")
        return None
    state_dir.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, token.encode())
    os.close(fd)
    info("cf-token: dropped (reason=first-boot).")
    return token_file


def _build_run_cmd(
    manifest: dict,
    image_ref: str,
    creds: dict,
    workspace: Path,
    container_name: str = CONTAINER_NAME,
    network: str | None = None,
    publish_internal: bool = True,
    mounts: list[str] | None = None,
) -> list[str]:
    """Assemble `docker run` from the manifest.

    *publish_internal* False publishes only ports without ``internal: true``
    (core: expose nginx :80, keep api/ui/supervisor off the host).
    *mounts* are extra ``-v`` specs (e.g. the consumed cf-token secret file).
    """
    cmd = ["docker", "run", "-d", "--name", container_name]
    if network:
        cmd += ["--network", network]
    for port in manifest.get("ports", []):
        if not publish_internal and port.get("internal"):
            continue
        c = port["container"]
        cmd += ["-p", f"{c}:{c}"]
    vol = manifest["volumes"][0]["container_path"]
    cmd += ["-v", f"{workspace}:{vol}"]
    for spec in mounts or []:
        cmd += ["-v", spec]
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
    _ensure_workspace(workspace)

    creds = _ensure_creds(manifest)
    _seed_first_boot_vars(workspace, creds)
    cf_token = _cf_token_mount(workspace, STATE_DIR)
    mounts = [f"{cf_token}:{CF_TOKEN_MOUNT}:ro"] if cf_token else None
    cmd = _build_run_cmd(manifest, image_ref, creds, workspace, mounts=mounts)

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
        from .commands import _full_plan

        _bootstrap_admin(workspace, _full_plan(CONTAINER_NAME))

    info("Opening in-container console ...")
    run(["docker", "exec", "-it", CONTAINER_NAME, "studio-console"], check=False, timeout=None)
    return True


# --- core (external-DB) launcher -----------------------------------------


def _ensure_core_network() -> bool:
    """Create the core docker network if absent. Named to avoid split collision."""
    exists = run_quiet(
        ["docker", "network", "ls", "-q", "--filter", f"name=^{CORE_NETWORK}$"]
    )[1]
    if exists:
        return True
    return run_quiet(["docker", "network", "create", CORE_NETWORK])[0] == 0


def _sidecar_running() -> bool:
    return bool(run_quiet(["docker", "ps", "-aq", "--filter", f"name=^{CORE_PG_CONTAINER}$"])[1])


def _start_sidecar(engine_image: str, workspace: Path) -> str | None:
    """Start (or reuse) the pinned pgvector sidecar; return its SHS_DATABASE_URL.

    The sidecar owns its data volume and outlives core — never torn down on
    core removal. Password is generated once and persisted in console state so
    the derived URL is stable across launches.
    """
    if not engine_image:
        error("Manifest has no engine.image pin — cannot start a trusted Postgres sidecar.")
        return None
    if not _ensure_core_network():
        error(f"Could not create docker network '{CORE_NETWORK}'.")
        return None

    state = _load_state(CORE_STATE_FILE)
    password = state.get("_SIDECAR_PG_PASSWORD")
    if not password:
        password = os.urandom(24).hex()
        state["_SIDECAR_PG_PASSWORD"] = password
        _save_state(state, CORE_STATE_DIR, CORE_STATE_FILE)

    url = (
        f"postgresql+asyncpg://{CORE_PG_USER}:{password}"
        f"@{CORE_PG_CONTAINER}:5432/{CORE_PG_DB}"
    )

    if _sidecar_running():
        info(f"Reusing existing Postgres sidecar '{CORE_PG_CONTAINER}'.")
        run_quiet(["docker", "start", CORE_PG_CONTAINER])
        return url

    db_dir = workspace / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    info(f"Starting Postgres sidecar '{CORE_PG_CONTAINER}' ({engine_image}) ...")
    # PG 18+ images store data in a major-version subdir and require the mount
    # at /var/lib/postgresql (not .../data) — mounting .../data exits non-zero.
    cmd = [
        "docker", "run", "-d", "--name", CORE_PG_CONTAINER,
        "--network", CORE_NETWORK,
        "-e", f"POSTGRES_USER={CORE_PG_USER}",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-e", f"POSTGRES_DB={CORE_PG_DB}",
        "-v", f"{db_dir}:/var/lib/postgresql",
        engine_image,
    ]
    if run(cmd, check=False).returncode != 0:
        error("Failed to start Postgres sidecar.")
        return None
    if not _wait_pg_ready():
        warn("Postgres sidecar did not report ready — core bootstrap may fail on first boot.")
    return url


def _wait_pg_ready(attempts: int = 30, interval: int = 2) -> bool:
    """Poll pg_isready inside the sidecar until it accepts connections."""
    import time

    for _ in range(attempts):
        rc, _ = run_quiet(
            ["docker", "exec", CORE_PG_CONTAINER, "pg_isready", "-U", CORE_PG_USER],
        )
        if rc == 0:
            ok("Postgres sidecar ready.")
            return True
        time.sleep(interval)
    return False


def _resolve_core_db_url(engine_image: str, workspace: Path) -> str | None:
    """Return SHS_DATABASE_URL for core, provisioning as chosen.

    Precedence: an already-persisted URL is reused silently. Otherwise a
    three-way prompt: enter a URL, spin up the pinned sidecar, or defer.
    Returns None to mean "defer — do not launch core yet".
    """
    state = _load_state(CORE_STATE_FILE)
    if state.get("SHS_DATABASE_URL"):
        return state["SHS_DATABASE_URL"]

    info("Core uses an external PostgreSQL. Choose how to supply it:")
    print()
    choices = [
        "Enter a connection URL   (local · docker · cloud Postgres)",
        "Spin up a Postgres sidecar   (pinned pgvector, local, opt-in)",
        "Configure later   (don't launch yet; add the URL from the console)",
    ]
    idx = _interactive_single("Database", choices, default=0, nav=False)

    if idx == 0:
        info(_dim(
            "The URL's role must have CREATEROLE — the API provisions the "
            "restricted shs_app runtime role from it on boot."
        ))
        url = _prompt_password("SHS_DATABASE_URL (postgresql+asyncpg://user:pass@host:5432/db)")
        if not url.strip():
            error("No URL entered — aborting.")
            return None
        url = url.strip()
    elif idx == 1:
        url = _start_sidecar(engine_image, workspace)
        if url is None:
            return None
    else:
        _defer_core_db()
        return None

    state["SHS_DATABASE_URL"] = url
    _save_state(state, CORE_STATE_DIR, CORE_STATE_FILE)
    return url


def _ensure_core_app_db_url(db_url: str) -> str:
    """Derive/persist core's restricted-role URL matching *db_url*'s DSN.

    Password is generated once and persisted; if the operator repoints the
    privileged URL at a different host/port/db, the DSN is re-derived keeping
    the same password (the API's bootstrap refreshes the role from it on boot).
    """
    from urllib.parse import urlsplit

    from .env import derive_app_db_url

    state = _load_state(CORE_STATE_FILE)
    existing = state.get("SHS_DATABASE_APP_URL", "")
    new_parts = urlsplit(db_url)
    if existing:
        old = urlsplit(existing)
        if (old.hostname, old.port, old.path) == (
            new_parts.hostname,
            new_parts.port,
            new_parts.path,
        ):
            return existing
    password = urlsplit(existing).password if existing else None
    app_url = derive_app_db_url(db_url, password=password)
    state["SHS_DATABASE_APP_URL"] = app_url
    _save_state(state, CORE_STATE_DIR, CORE_STATE_FILE)
    return app_url


def _print_byo_provision_help(db_url: str, app_url: str) -> None:
    """Fail-closed diagnosis for BYO Postgres: shs_app provisioning failed.

    Two supported fixes (7cc4942ce): grant CREATEROLE for automatic mode, or
    pre-create the role as a DB admin (manual mode — a correctly-attributed
    role provisions fully every boot; the operator owns its password).
    """
    from urllib.parse import unquote, urlsplit

    priv_role = urlsplit(db_url).username or "<your role>"
    app = urlsplit(app_url)
    app_role = app.username or "shs_app"
    app_password = unquote(app.password or "")
    error("The API did not become healthy. On boot it provisions the restricted")
    error(f"{app_role} role via SHS_DATABASE_URL and fails closed if it can't.")
    create_sql = (
        f"CREATE ROLE {app_role} LOGIN PASSWORD '{app_password}' "
        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;"
    )
    print(f"  {_dim('Fix on your Postgres (either one), then restart the container:')}")
    print(f"    {_bold(f'ALTER ROLE {priv_role} CREATEROLE;')}   {_dim('(automatic mode)')}")
    print(f"    {_bold(create_sql)}   {_dim('(manual mode)')}")
    print(f"  {_dim('Diagnose:')} {_bold(f'docker logs {CORE_CONTAINER_NAME} --tail 30')}")


def _defer_core_db() -> None:
    """Record the deferral and tell the operator how to finish later."""
    CORE_STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    warn("Core not launched — no database configured yet.")
    print(f"  {_dim('Add a database later, then launch:')}")
    print(f"    {_bold('studio-console launch-core')}   {_dim('(re-run and pick a DB)')}")
    print(f"  {_dim('Or set the URL directly in console state, then re-run launch-core.')}")


def cmd_launch_core(context: str, tag: str | None = None, workspace: Path | None = None) -> bool:
    """Launch the core single-image deployment against an external Postgres."""
    if context != "host":
        error("launch-core runs on the host — you appear to be inside a container.")
        return False
    if run_quiet(["docker", "version"])[0] != 0:
        error("Docker is not available. Install Docker and retry.")
        return False

    heading("Launch Studio (core)")
    tag = tag or DEFAULT_TAG
    image_ref = f"{CORE_IMAGE}:{tag}"

    existing = run_quiet(["docker", "ps", "-aq", "--filter", f"name=^{CORE_CONTAINER_NAME}$"])[1]
    if existing:
        warn(f"Container '{CORE_CONTAINER_NAME}' already exists.")
        print(f"  {_dim('Connect:')} {_bold(f'docker exec -it {CORE_CONTAINER_NAME} studio-console')}")
        return False

    manifest = _read_manifest(image_ref, shape="core")
    if manifest is None:
        return False

    workspace = workspace or (Path.home() / ".studio-core")
    _ensure_workspace(workspace)

    # Supervisor creds via the shared flow; DB via the dedicated 3-way prompt.
    creds = _ensure_creds(
        manifest,
        CORE_STATE_DIR,
        CORE_STATE_FILE,
        only=["SHS_SUPERVISOR_USER", "SHS_SUPERVISOR_PASSWORD"],
    )

    db_url = _resolve_core_db_url(manifest.get("_engine", {}).get("image", ""), workspace)
    if db_url is None:
        return False  # deferred or aborted
    creds["SHS_DATABASE_URL"] = db_url
    # Restricted runtime role: the API provisions shs_app from this URL on
    # boot and fails closed if it can't. Older images ignore the var.
    creds["SHS_DATABASE_APP_URL"] = _ensure_core_app_db_url(db_url)

    _seed_first_boot_vars(workspace, creds)
    cf_token = _cf_token_mount(workspace, CORE_STATE_DIR)
    mounts = [f"{cf_token}:{CF_TOKEN_MOUNT}:ro"] if cf_token else None

    # If the sidecar is on the core network, core must join it to reach the DB.
    network = CORE_NETWORK if _sidecar_running() else None
    cmd = _build_run_cmd(
        manifest, image_ref, creds, workspace,
        container_name=CORE_CONTAINER_NAME,
        network=network,
        publish_internal=False,
        mounts=mounts,
    )

    info(f"Starting {image_ref} (data: {workspace}) ...")
    if run(cmd, check=False).returncode != 0:
        error("docker run failed.")
        return False

    ok(f"{CORE_CONTAINER_NAME} started.")
    nginx_port = next((p["container"] for p in manifest["ports"] if p.get("name") == "nginx"), 80)
    print(f"  {_dim('UI:')} {_cyan(f'http://localhost:{nginx_port}')}")
    print(f"  {_dim('Console:')} {_bold(f'docker exec -it {CORE_CONTAINER_NAME} studio-console')}")
    print()

    api_healthy = _wait_healthy(manifest, container_name=CORE_CONTAINER_NAME)
    if api_healthy:
        from .commands import _core_plan

        _bootstrap_admin(workspace, _core_plan(CORE_CONTAINER_NAME, CORE_PG_CONTAINER))
    elif CORE_PG_CONTAINER not in db_url:
        _print_byo_provision_help(db_url, creds["SHS_DATABASE_APP_URL"])

    info("Opening in-container console ...")
    run(["docker", "exec", "-it", CORE_CONTAINER_NAME, "studio-console"], check=False, timeout=None)
    return True


def cmd_set_core_db_url(context: str, url: str | None = None) -> bool:
    """Set/replace core's SHS_DATABASE_URL in host console state.

    Core reads the DB URL from process env at launch (never from the workspace
    .env), and the launcher owns that persistence — so this writes to console
    state, and the next `launch-core` injects it via `docker run -e`.
    """
    if context != "host":
        error("core-db-url runs on the host — you appear to be inside a container.")
        return False

    url = (url or "").strip() or _prompt_password(
        "SHS_DATABASE_URL (postgresql+asyncpg://user:pass@host:5432/db)"
    ).strip()
    if not url:
        error("No URL provided.")
        return False

    state = _load_state(CORE_STATE_FILE)
    state["SHS_DATABASE_URL"] = url
    _save_state(state, CORE_STATE_DIR, CORE_STATE_FILE)
    _ensure_core_app_db_url(url)  # keep the derived shs_app URL on the same DSN
    ok("Saved core database URL.")

    if run_quiet(["docker", "ps", "-q", "--filter", f"name=^{CORE_CONTAINER_NAME}$"])[1]:
        warn("A core container is running — recreate it to apply:")
        print(f"    {_bold(f'docker rm -f {CORE_CONTAINER_NAME} && studio-console launch-core')}")
    else:
        print(f"  {_dim('Launch:')} {_bold('studio-console launch-core')}")
    return True


def _bootstrap_admin(workspace: Path, plan: "object") -> None:
    """First-boot super admin creation, using the given shape's exec plan.

    *plan* is a _BootstrapPlan: full execs one container for both API and psql;
    core execs the API container for hashing but psql against the DB sidecar.
    """
    env_file = workspace / ".env"
    if not env_file.exists():
        warn("Workspace .env not found — skipping admin bootstrap.")
        return

    from .commands import _bootstrap_first_admin

    _bootstrap_first_admin(env_file, plan)


def _curl_status(url: str) -> int:
    """Return HTTP status code for url, or 0 if unreachable."""
    rc, out = run_quiet(["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=5)
    try:
        return int(out.strip())
    except ValueError:
        return 0


def _wait_healthy(
    manifest: dict,
    attempts: int = 30,
    interval: int = 3,
    container_name: str = CONTAINER_NAME,
) -> bool:
    """Poll API + UI on first boot; flag a non-200 UI. Returns API health.

    When api/ui are host-published (full), poll them directly. When they are
    ``internal: true`` (core), poll through the nginx front door instead —
    ``/health`` (served by nginx) and ``/`` — since those ports aren't
    reachable from the host.
    """
    import time

    api_p = next((p for p in manifest["ports"] if p.get("name") == "api"), {})
    ui_p = next((p for p in manifest["ports"] if p.get("name") == "ui"), {})
    nginx_port = next((p["container"] for p in manifest["ports"] if p.get("name") == "nginx"), 80)

    if api_p.get("internal") or ui_p.get("internal"):
        # nginx serves `location = /health` directly; /api/* proxies to the API.
        api_url = f"http://localhost:{nginx_port}/health"
        ui_url = f"http://localhost:{nginx_port}/"
        api_label = ui_label = f":{nginx_port}"
    else:
        api_port = api_p.get("container", 8000)
        ui_port = ui_p.get("container", 3000)
        api_url = f"http://localhost:{api_port}/health"
        ui_url = f"http://localhost:{ui_port}"
        api_label, ui_label = f":{api_port}", f":{ui_port}"

    info("Waiting for the stack to come up (first boot runs bootstrap) ...")
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
        ok(f"API healthy ({api_label}).")
    else:
        warn(f"API not healthy ({api_label}) — check: docker exec {container_name} supervisorctl status")
    if ui_status == 200:
        ok(f"UI healthy ({ui_label}).")
    else:
        warn(f"UI returned {ui_status or 'no response'} ({ui_label}) — SSR may be failing.")
        print(f"  {_dim('Logs:')} {_bold(f'docker exec {container_name} supervisorctl tail ui stderr')}")
    return api_ok == 200
