# studio_console/commands_container.py
"""Container-mode menu for Core / Full images."""

from __future__ import annotations

import os
from pathlib import Path

from .cloudflare.cf_wizard import update_domain, update_ip_rules
from .commands import (
    _submenu_backup,
    cmd_health,
    cmd_logs,
    cmd_reset_password,
    cmd_restart,
    cmd_show_config,
)
from .env import detect_shape, promote_runpod_secrets, read_env, set_env_value
from .tui import (
    NavBack,
    NavExit,
    _dim,
    _interactive_single,
    _prompt,
    error,
    heading,
    ok,
)
from .cloudflare.cf_wizard import cf_full_setup
from .wizard import (
    SetupState,
    _ask_api_hostname,
    _ask_ip_restrict_mode,
    _ask_root_domain,
    _ask_ui_subdomain,
)


def container_menu(context: str, env_file: Path) -> None:
    """Interactive menu for in-container operation."""
    promote_runpod_secrets(env_file)

    shape = detect_shape(env_file) or "core"
    heading(f"Studio  ({shape})")

    actions: list[tuple[str, str]] = [
        (f"Health          {_dim('supervisorctl status')}", "health"),
        (f"Restart         {_dim('restart a service or all')}", "restart"),
        (f"Logs            {_dim('tail a service log')}", "logs"),
        (f"Config          {_dim('show .env (secrets masked)')}", "config"),
    ]
    actions.append((f"Workers         {_dim('scale general / transfer')}", "workers"))
    if shape == "full":
        actions.append((f"Backup          {_dim('backup · restore')}", "backup"))
    actions += [
        (f"Cloudflare      {_dim('tunnel · domain · access rules')}", "cloudflare"),
        (f"Reset password  {_dim('super-admin password reset')}", "reset"),
        (f"Exit", "exit"),
    ]
    menu_options = [label for label, _ in actions]

    while True:
        if env_file.exists():
            env_data = read_env(env_file)
            public_url = env_data.get("SHS_PUBLIC_BASE_URL", "")
            if public_url.startswith("https://"):
                print(f"  Public URL: {public_url}")
        print()

        idx = _interactive_single("Studio", menu_options, default=0, nav=False)
        action = actions[idx][1]

        try:
            if action == "health":
                cmd_health(context, env_file)
            elif action == "restart":
                _restart_picker(context, env_file)
            elif action == "logs":
                _logs_picker(context, env_file)
            elif action == "config":
                if not env_file.exists():
                    error(f"No .env at {env_file} — entrypoint hasn't run.")
                else:
                    cmd_show_config(context, env_file)
            elif action == "workers":
                _cmd_workers(env_file)
            elif action == "backup":
                _submenu_backup(context, env_file)
            elif action == "cloudflare":
                _cloudflare_menu(env_file)
            elif action == "reset":
                cmd_reset_password(context, env_file)
            elif action == "exit":
                return
        except (NavBack, NavExit):
            pass

        print()


# Scalable workers in the full/core image: supervisord program group → env var
# holding its numprocs. Singleton services (api/ui/nginx/postgres) are pinned
# numprocs=1 in the image and are not listed here.
_WORKER_GROUPS: list[tuple[str, str]] = [
    ("worker-general", "SHS_GENERAL_WORKERS"),
    ("worker-transfer", "SHS_TRANSFER_WORKERS"),
]


def _running_proc_count(status_out: str, group: str) -> int:
    """Count RUNNING procs in a supervisord process group.

    numprocs groups list as `group:group_NN  RUNNING  pid ...`. Count ONLY
    procs whose state field is RUNNING — a proc in FATAL/STOPPED/STARTING/
    BACKOFF still appears in the listing, so matching the group name alone
    over-reports the live count.
    """
    count = 0
    prefix = f"{group}:"
    for line in status_out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(prefix) and parts[1] == "RUNNING":
            count += 1
    return count


def _cmd_workers(env_file: Path) -> None:
    """View and set general/transfer worker counts (applies on container restart).

    numprocs is parse-time and read from supervisord's PID-1 environment, frozen
    at boot by the entrypoint. We can only write /workspace/.env; the new count
    takes effect when the container restarts and PID 1 re-sources .env. The
    in-container console cannot restart its own container (it is a child of the
    supervisord it would kill), so we write the value and print the restart command.
    """
    from .env import run_quiet

    if not env_file.exists():
        error(f"No .env at {env_file} — entrypoint hasn't run.")
        return

    env_data = read_env(env_file)
    _, status_out = run_quiet(["supervisorctl", "status"], timeout=10)

    heading("Workers")
    print(f"  {'worker':<16}{'configured':<12}{'running'}")
    for group, var in _WORKER_GROUPS:
        configured = env_data.get(var, "1")
        running = _running_proc_count(status_out, group)
        note = "  (restart to apply)" if str(running) != configured else ""
        print(f"  {group:<16}{configured:<12}{running}{_dim(note)}")
    print()

    changed = False
    for group, var in _WORKER_GROUPS:
        current = env_data.get(var, "1")
        raw = _prompt(f"{group} count", current)
        if raw == current:
            continue
        try:
            count = int(raw)
            if count < 0:
                raise ValueError
        except ValueError:
            error(f"{group}: '{raw}' is not a non-negative integer — skipped.")
            continue
        set_env_value(env_file, var, str(count))
        changed = True

    if not changed:
        return

    container = os.environ.get("HOSTNAME", "").strip() or "<container>"
    ok("Saved.")
    print(f"  Restart to apply:  {_dim(f'docker restart {container}')}")


def _kick_cloudflared(env_file: Path) -> None:
    """Start cloudflared and restart UI so __env.js picks up new public URLs."""
    import time

    from .env import ok, run_quiet

    token = read_env(env_file).get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    if not token:
        return

    _, out = run_quiet(["supervisorctl", "status", "cloudflared"], timeout=10)
    state = out.split()[1] if len(out.split()) >= 2 else ""
    action = "restart" if state == "RUNNING" else "start"
    run_quiet(["supervisorctl", action, "cloudflared"], timeout=15)

    deadline = time.monotonic() + 30
    final_state = ""
    while time.monotonic() < deadline:
        _, out = run_quiet(["supervisorctl", "status", "cloudflared"], timeout=10)
        final_state = out.split()[1] if len(out.split()) >= 2 else ""
        if final_state == "RUNNING":
            break
        if final_state in ("FATAL", "EXITED", "BACKOFF"):
            break
        time.sleep(1)

    if final_state == "RUNNING":
        ok(f"cloudflared {action}ed")
    else:
        error(f"cloudflared is {final_state or 'unreachable'} — check supervisorctl tail cloudflared stderr")

    run_quiet(["supervisorctl", "restart", "ui"], timeout=15)
    deadline = time.monotonic() + 30
    final_state = ""
    while time.monotonic() < deadline:
        _, out = run_quiet(["supervisorctl", "status", "ui"], timeout=10)
        final_state = out.split()[1] if len(out.split()) >= 2 else ""
        if final_state == "RUNNING":
            break
        if final_state in ("FATAL", "EXITED", "BACKOFF"):
            break
        time.sleep(1)

    if final_state == "RUNNING":
        ok("ui restarted")
    else:
        error(f"ui is {final_state or 'unreachable'} — check supervisorctl tail ui stderr")


def _sync_derived_urls(env_file: Path) -> None:
    """Recompute API/WS/frontend/CORS after public_domain changes."""
    env_data = read_env(env_file)
    public_url = env_data.get("SHS_PUBLIC_BASE_URL", "").strip()
    api_public_url = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").strip()
    nginx_port = env_data.get("SHS_NGINX_PORT", "80")

    has_public = public_url.startswith("https://")
    localhost_origins = "http://localhost" if nginx_port == "80" else f"http://localhost:{nginx_port}"

    if has_public:
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

    set_env_value(env_file, "SHS_API_BASE_URL", api_url)
    set_env_value(env_file, "SHS_PUBLIC_API_URL", api_url)
    set_env_value(env_file, "SHS_WS_URL", ws_url)
    set_env_value(env_file, "SHS_FRONTEND_URL", frontend_url)
    set_env_value(env_file, "SHS_CORS_ORIGINS", cors_origins)


def _cloudflare_menu(env_file: Path) -> None:
    """Cloudflare tunnel + domain + IP rules submenu (in-container)."""
    if not env_file.exists():
        error(f"No .env at {env_file} — entrypoint hasn't run.")
        return

    env_data = read_env(env_file)
    has_tunnel = bool(env_data.get("CLOUDFLARE_TUNNEL_ID"))

    options = [
        f"Setup / re-run wizard  {_dim('full tunnel + DNS + Access setup')}",
        f"Update domain          {_dim('change public hostname')}" if has_tunnel else None,
        f"Update IP rules        {_dim('change Access allow/bypass list')}" if has_tunnel else None,
    ]
    options = [o for o in options if o is not None]

    while True:
        try:
            idx = _interactive_single("Cloudflare", options, default=0)
        except NavBack:
            return

        label = options[idx]
        try:
            if label.startswith("Setup"):
                state = SetupState(env_file)
                root = _ask_root_domain(state)
                ui_sub = _ask_ui_subdomain(state, root)
                state.public_domain = f"https://{ui_sub}.{root}"
                _ask_api_hostname(state, root, ui_sub, required=True)
                _ask_ip_restrict_mode(state)
                set_env_value(env_file, "SHS_PUBLIC_BASE_URL", state.public_domain)
                set_env_value(env_file, "CONSOLE_PUBLIC_API_BASE_URL", state.public_api_domain)
                set_env_value(env_file, "CONSOLE_IP_RESTRICT_MODE", state.ip_restrict_mode)
                cf_full_setup(env_file)
                _sync_derived_urls(env_file)
                _kick_cloudflared(env_file)
                env_data = read_env(env_file)
                has_tunnel = bool(env_data.get("CLOUDFLARE_TUNNEL_ID"))
                options = [
                    f"Setup / re-run wizard  {_dim('full tunnel + DNS + Access setup')}",
                    f"Update domain          {_dim('change public hostname')}" if has_tunnel else None,
                    f"Update IP rules        {_dim('change Access allow/bypass list')}" if has_tunnel else None,
                ]
                options = [o for o in options if o is not None]
            elif label.startswith("Update domain"):
                update_domain(env_file)
                _kick_cloudflared(env_file)
            elif label.startswith("Update IP rules"):
                update_ip_rules(env_file)
        except (NavBack, NavExit):
            return
        except Exception as exc:
            error(f"Cloudflare action failed: {exc}")


_SUPERVISORD_STATES = (
    "RUNNING", "STOPPED", "STARTING", "FATAL",
    "EXITED", "BACKOFF", "STOPPING", "UNKNOWN",
)


def _list_services() -> list[str] | None:
    """Return list of supervisord service names, or None if unreachable."""
    from .env import run_quiet

    _, out = run_quiet(["supervisorctl", "status"], timeout=10)
    if not out.strip() or "refused connection" in out.lower() or "no such file" in out.lower():
        return None
    services: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in _SUPERVISORD_STATES:
            services.append(parts[0])
    return services


def _restart_picker(context: str, env_file: Path) -> None:
    """Pick a service to restart, or restart all."""
    services = _list_services()
    if services is None:
        error("supervisord is not reachable — entrypoint hasn't started it.")
        return
    if not services:
        error("supervisorctl returned no services")
        return

    labels = ["All services"] + services
    try:
        pick = _interactive_single("Restart", labels, default=0)
    except NavBack:
        return

    try:
        if pick == 0:
            cmd_restart(context, env_file, None)
        else:
            cmd_restart(context, env_file, services[pick - 1])
    except Exception as exc:
        error(f"Restart failed: {exc}")


def _logs_picker(context: str, env_file: Path) -> None:
    """Loop: pick a service then mode, return to picker after each session."""
    from .env import run

    while True:
        services = _list_services()
        if services is None:
            error("supervisord is not reachable — entrypoint hasn't started it.")
            return
        if not services:
            error("supervisorctl returned no services")
            return

        try:
            pick = _interactive_single("Service", services, default=0)
        except NavBack:
            return
        service = services[pick]

        try:
            mode = _interactive_single(
                "Mode",
                [f"Recent  {_dim('recent stdout + stderr, then exit')}", f"Stream  {_dim('live stderr tail, Ctrl-C to stop')}"],
                default=0,
            )
        except NavBack:
            continue

        # Use supervisorctl tail — the canonical API — instead of guessing raw
        # log-file paths under /var/log/supervisor (fragile: depends on
        # supervisord's logfile config and naming). Pass the full service name
        # (group:service) so grouped programs resolve correctly.
        # NB: supervisorctl tail's byte arg is BYTES not lines, and defaults to
        # stdout — so for "Recent" we pull a generous byte window of BOTH
        # streams; for "Stream" we follow stderr (where failures surface).
        try:
            if mode == 0:
                print(_dim("  ── stdout ──"))
                run(["supervisorctl", "tail", "-8000", service, "stdout"], timeout=15, check=False)
                print(_dim("  ── stderr ──"))
                run(["supervisorctl", "tail", "-8000", service, "stderr"], timeout=15, check=False)
            else:
                try:
                    run(["supervisorctl", "tail", "-f", service, "stderr"], timeout=None, check=False)
                except KeyboardInterrupt:
                    pass
        except Exception as exc:
            error(f"Logs failed: {exc}")
