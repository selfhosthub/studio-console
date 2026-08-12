# studio_console/commands_container.py
"""Container-mode menu for Core / Full images."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .cloudflare.cf_wizard import update_domain, update_ip_rules
from .commands import (
    _submenu_backup,
    cmd_db_role,
    cmd_health,
    cmd_logs,
    cmd_reset_password,
    cmd_restart,
    cmd_show_config,
)
from .env import (
    derive_url_vars,
    detect_shape,
    promote_runpod_secrets,
    read_env,
    run_quiet,
    set_env_value,
)
from .tui import (
    NavBack,
    NavExit,
    _bold,
    _cyan,
    _dim,
    _red,
    _interactive_single,
    _interactive_yn,
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
    actions.append((f"Worker kit      {_dim('setup commands for a GPU/remote worker')}", "workerkit"))
    actions.append((f"DB role         {_dim('restricted runtime role (RLS)')}", "dbrole"))
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
                _cmd_workers()
            elif action == "workerkit":
                from .commands_kit import cmd_worker_kit

                cmd_worker_kit(context, env_file)
            elif action == "dbrole":
                cmd_db_role(context, env_file)
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

# Supervisor conf fragment per worker group. A literal numprocs can be rescaled
# in place; an %(ENV_...)s one needs a container restart to re-source .env.
_WORKER_CONF = {
    "worker-general": "/etc/supervisor/conf.d/worker-general.conf",
    "worker-transfer": "/etc/supervisor/conf.d/worker-transfer.conf",
}
_NUMPROCS_RE = re.compile(r"^numprocs=(.*)$", re.MULTILINE)


def _fragment_is_literal(group: str) -> bool | None:
    """True if numprocs is a literal, False if env-expanded, None if unreadable."""
    try:
        text = Path(_WORKER_CONF[group]).read_text()
    except OSError:
        return None
    m = _NUMPROCS_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().isdigit()


def _configured_numprocs(group: str) -> str | None:
    """The literal numprocs from the conf fragment (the source of truth), or
    None if unreadable/env-expanded."""
    try:
        text = Path(_WORKER_CONF[group]).read_text()
    except OSError:
        return None
    m = _NUMPROCS_RE.search(text)
    if m and m.group(1).strip().isdigit():
        return m.group(1).strip()
    return None


def _rewrite_numprocs(group: str, count: int) -> bool:
    """Rewrite the numprocs= line in place, preserving the fragment's mode/owner."""
    path = Path(_WORKER_CONF[group])
    try:
        text = path.read_text()
    except OSError:
        return False
    new_text, n = _NUMPROCS_RE.subn(f"numprocs={count}", text)
    if n != 1:
        return False
    try:
        path.write_text(new_text)
    except OSError:
        return False
    return True


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


def _apply_worker_numprocs(group: str, count: int) -> None:
    """Rescale a group in place (rewrite fragment + reread/update + verify), or
    fall back to the container-restart notice when the fragment is env-expanded."""
    from .env import run_quiet

    if _fragment_is_literal(group) is not True:
        _print_container_restart_notice()
        return

    if not _rewrite_numprocs(group, count):
        error(f"{group}: could not update {_WORKER_CONF[group]} — skipped.")
        _print_container_restart_notice()
        return

    run_quiet(["supervisorctl", "reread"], timeout=15)
    run_quiet(["supervisorctl", "update"], timeout=30)

    # New procs pass through STARTING (startsecs) before RUNNING — poll briefly.
    running = _poll_worker_count(group, count)
    if running == count:
        ok(f"{group}: now {count}")
    else:
        error(
            f"{group}: expected {count} but {running} running after reread/update — "
            f"check `supervisorctl status` and {_WORKER_CONF[group]}."
        )


def _poll_worker_count(group: str, target: int, timeout: float = 12.0) -> int:
    """Poll supervisorctl until the group's RUNNING count hits target or timeout."""
    import time

    from .env import run_quiet

    deadline = time.monotonic() + timeout
    running = -1
    while True:
        _, status_out = run_quiet(["supervisorctl", "status"], timeout=10)
        running = _running_proc_count(status_out, group)
        if running == target or time.monotonic() >= deadline:
            return running
        time.sleep(1)


def _print_container_restart_notice() -> None:
    container = os.environ.get("HOSTNAME", "").strip() or "<container>"
    print(f"  {_red('Restart the whole container to apply — the count is frozen at boot.')}")
    print(f"  {_red('Any in-flight jobs on these workers will be dropped.')}")
    print(f"  {_red('From the host (or your pod controls):')} {_red(_bold(f'docker restart {container}'))}")


def _cmd_workers() -> None:
    """View and set general/transfer worker counts. The conf fragment's literal
    numprocs is the source of truth; applies in place via reread/update when
    supported, else prints the container-restart notice."""
    from .env import run_quiet

    _, status_out = run_quiet(["supervisorctl", "status"], timeout=10)

    heading("Workers")
    print(f"  {'worker':<16}{'configured':<12}{'running'}")
    for group, _var in _WORKER_GROUPS:
        configured = _configured_numprocs(group) or "?"
        running = _running_proc_count(status_out, group)
        note = "  (restart to apply)" if str(running) != configured else ""
        print(f"  {group:<16}{configured:<12}{running}{_dim(note)}")
    print()

    pending: list[tuple[str, int]] = []
    for group, _var in _WORKER_GROUPS:
        current = _configured_numprocs(group) or "1"
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
        pending.append((group, count))

    if not pending:
        return

    if not _interactive_yn(
        "Apply now? in-flight jobs on removed workers will be dropped.",
        default=True,
        nav=False,
    ):
        print(_dim("  No change — worker counts persist in each conf fragment."))
        return

    for group, count in pending:
        _apply_worker_numprocs(group, count)


def _kick_cloudflared(env_file: Path) -> None:
    """Start cloudflared and restart UI so __env.js picks up new public URLs."""
    import time

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
    urls = derive_url_vars(
        env_data.get("SHS_PUBLIC_BASE_URL", ""),
        env_data.get("CONSOLE_PUBLIC_API_BASE_URL", ""),
        env_data.get("SHS_NGINX_PORT", "80"),
    )
    for key, value in urls.items():
        set_env_value(env_file, key, value)


def _cloudflared_state() -> str:
    """Live cloudflared process state from supervisord (not marker/wizard state)."""
    _, out = run_quiet(["supervisorctl", "status", "cloudflared"], timeout=10)
    parts = out.split()
    return parts[1] if len(parts) >= 2 else ""


def _print_cf_status(env_data: dict) -> None:
    """Render tunnel + runtime state, sourced only from .env and supervisord."""
    token = env_data.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    domain = env_data.get("SHS_PUBLIC_BASE_URL", "").strip()
    if not token:
        print(f"  {_dim('Tunnel:')} not configured")
        return
    state = _cloudflared_state()
    label = {
        "RUNNING": _cyan("running"),
        "STOPPED": _dim("stopped"),
        "STARTING": _dim("starting"),
    }.get(state, _bold(f"{state or 'unknown'} — check: supervisorctl tail cloudflared stderr"))
    print(f"  {_dim('Tunnel:')} token set · cloudflared {label}")
    if domain:
        print(f"  {_dim('Domain:')} {domain}")
    print()


def _cloudflare_menu(env_file: Path) -> None:
    """Cloudflare tunnel + domain + IP rules submenu (in-container)."""
    if not env_file.exists():
        error(f"No .env at {env_file} — entrypoint hasn't run.")
        return

    env_data = read_env(env_file)
    _print_cf_status(env_data)
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
