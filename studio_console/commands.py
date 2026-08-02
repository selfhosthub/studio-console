# studio_console/commands.py
"""Command functions, submenus, and config menu."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .constants import (
    COMPONENT_TO_IMAGE,
    COMPONENT_TO_PROFILE,
    ENV_SECTIONS,
    IMAGE_BUILD_CONFIG,
    SCALE_PROFILES,
    SCALE_VARS,
    THIRD_PARTY_IMAGES,
)
from . import major_version as mv
from .env import (
    _find_repo_root,
    _get_running_services,
    _images_from_env,
    _validate_env,
    backup_root,
    compose_cmd,
    derive_app_db_url,
    detect_context,
    env_path,
    fatal,
    mask_value,
    read_env,
    run,
    run_quiet,
    set_env_value,
    unset_env_values,
    storage_root,
    validate_password,
    write_env,
)
from .tui import (
    NavBack,
    NavExit,
    _bold,
    _cyan,
    _dim,
    _green,
    _interactive_single,
    _interactive_yn,
    _prompt,
    _prompt_password,
    _red,
    _yellow,
    error,
    heading,
    info,
    ok,
    warn,
    warn_header,
)
from .wizard import (
    SetupState,
    _section_api_ui_scaling,
    _write_override_and_nginx,
    wizard,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _apply_scale_flags(up_cmd: list[str], env_data: dict) -> list[str]:
    """Append --scale flags for any *active* worker with a count != 1.

    Only scales services whose profile is in COMPOSE_PROFILES, compose errors
    'no such service: X: disabled' if you --scale a profile that isn't active.
    """
    active = {p for p in env_data.get("COMPOSE_PROFILES", "").split(",") if p}
    for var, service in SCALE_PROFILES.items():
        if service not in active:
            continue
        count = env_data.get(var, "")
        if count and count != "1":
            up_cmd += ["--scale", f"{service}={count}"]
    return up_cmd


def _stop_api_ui_containers() -> None:
    """Remove all studio-api-*, studio-ui-*, studio-nginx-* containers directly.

    Uses 'ps -a' so Created/Exited containers (not just running ones) are caught
    otherwise a leftover 'studio-api-2' in Created state collides with compose on
    recreate ('container name already in use'). 'docker rm -f' force-removes
    running ones too; '-s' is NOT a valid rm flag.
    """
    ctrs: list[str] = []
    for pattern in ("studio-api-", "studio-ui-", "studio-nginx-"):
        _, out = run_quiet(["docker", "ps", "-aq", "--filter", f"name={pattern}"])
        ctrs.extend(c.strip() for c in out.strip().splitlines() if c.strip())
    ctrs = list(dict.fromkeys(ctrs))
    if ctrs:
        info(f"Stopping {len(ctrs)} API/UI/nginx container(s)...")
        run_quiet(["docker", "rm", "-f"] + ctrs, timeout=30)


def _log_picker(context: str, env_file: Path, follow: bool) -> None:
    """Interactive loop to pick a service and show/stream its logs."""
    prompt = "Stream logs for" if follow else "View logs for"
    while True:
        services = _get_running_services(context, env_file)
        labels, targets = _build_log_options(services)
        try:
            pick = _interactive_single(prompt, labels, default=0)
        except NavBack:
            break
        cmd_logs(context, env_file, targets[pick], follow=follow)


def _missing_images(env_file: Path) -> list[str]:
    """Return configured image names that are not present locally."""
    env_data = read_env(env_file)
    tag = env_data.get("SHS_STUDIO_VERSION", "latest")
    missing: list[str] = []
    for name in _images_from_env(env_file):
        found = any(
            run_quiet(["docker", "image", "inspect", f"{prefix}/{name}:{tag}"])[0] == 0
            for prefix in ("ghcr.io/selfhosthub", "selfhosthub")
        )
        if not found:
            missing.append(name)
    return missing


def _pull_images(names: list[str], tag: str) -> bool:
    """Pull the given studio images from the registry. Returns True iff all succeed.

    Tries the canonical ghcr.io/selfhosthub prefix first, then the bare
    selfhosthub fallback (mirrors _missing_images' lookup order).
    """
    all_ok = True
    for name in names:
        pulled = False
        for prefix in ("ghcr.io/selfhosthub", "selfhosthub"):
            ref = f"{prefix}/{name}:{tag}"
            rc, _ = run_quiet(["docker", "pull", ref], timeout=300)
            if rc == 0:
                ok(f"{ref}")
                pulled = True
                break
        if not pulled:
            all_ok = False
    return all_ok


def _fail_missing_images(missing: list[str], tag: str) -> None:
    """Loud failure for absent images: the exact pull commands, never a build."""
    error(f"Missing images: {', '.join(missing)}")
    info("Pull them from the registry:")
    for name in missing:
        info(f"  docker pull ghcr.io/selfhosthub/{name}:{tag}")
    warn("Check the tag (SHS_STUDIO_VERSION) and registry access.")
    warn("Running from source? Images are never auto-built with a release tag;")
    warn("use Images → Build to build them explicitly.")


# ---------------------------------------------------------------------------
# Config menu (main interactive menu when .env exists)
# ---------------------------------------------------------------------------


def config_menu(context: str, env_file: Path) -> None:
    """Interactive config menu when .env already exists."""
    heading("Studio")

    menu_options = [
        f"Services        {_dim('start · stop · restart · health · logs · links')}",
        f"Setup           {_dim('wizard: components · secrets · domain · cloudflare')}",
        f"Images          {_dim('build · upgrade · rollback')}",
        f"Advanced        {_dim('scale API/UI · per-service ops · cloudflare')}",
        f"Backup          {_dim('backup · restore')}",
        f"Update console  {_dim('upgrade studio-console to latest')}",
        f"Exit",
    ]

    while True:
        # Quick status check before showing menu
        env_data = read_env(env_file)
        nginx_port = env_data.get("SHS_NGINX_PORT", "80")
        health_url = f"http://localhost:{nginx_port}/health"
        rc, _ = run_quiet(["curl", "-sf", health_url])
        if rc == 0:
            public_url = env_data.get("SHS_PUBLIC_BASE_URL", "")
            local = (
                f"http://localhost:{nginx_port}"
                if nginx_port != "80"
                else "http://localhost"
            )
            print(f"  Status:     {_green('running')}")
            print(f"  Local URL:  {local}")
            if public_url.startswith("https://"):
                print(f"  Public URL: {public_url}")
        else:
            rc2, ps_out = run_quiet(
                compose_cmd(env_file) + ["ps", "--format", "json"], timeout=10
            )
            running = []
            if rc2 == 0 and ps_out.strip():
                import json as _json

                for line in ps_out.strip().splitlines():
                    try:
                        svc = _json.loads(line)
                        state = svc.get("State", "")
                        if state == "running":
                            running.append(svc.get("Service", ""))
                    except _json.JSONDecodeError:
                        pass
            if not running:
                print(f"  Status: {_cyan('configured')}  |  Services → Start all")
            elif mv.scrape_guardrail_failure(env_file, context):
                print(
                    f"  Status: {_red('blocked')}  |  major-version boundary, see Services → Health"
                )
            else:
                print(f"  Status: {_yellow('starting...')}")
        print()

        idx = _interactive_single("Studio", menu_options, default=0, nav=False)

        try:
            if idx == 0:
                _submenu_services(context, env_file)
            elif idx == 1:
                _submenu_setup(context, env_file)
            elif idx == 2:
                _submenu_images(context, env_file)
            elif idx == 3:
                _submenu_advanced(context, env_file)
            elif idx == 4:
                _submenu_backup(context, env_file)
            elif idx == 5:
                cmd_self_update(context)
            elif idx == 6:
                return
        except (NavBack, NavExit):
            pass  # return to main menu

        print()


# ---------------------------------------------------------------------------
# Restart helper
# ---------------------------------------------------------------------------


def _restart_for_setup(
    context: str,
    env_file: Path,
    include_api: bool = False,
    old_profiles: str = "",
) -> None:
    """Restart services after a settings change.

    Stops containers using the *old* profile set (captured before wizard wrote
    the new .env) so we catch workers that were just deselected and would
    otherwise become orphans. Then starts using the *new* profile set.

    When include_api=False only worker containers are touched; API/UI/nginx
    are left running throughout.
    """
    env_data = read_env(env_file)
    base = compose_cmd(env_file)
    new_profiles = {p for p in env_data.get("COMPOSE_PROFILES", "").split(",") if p}

    # Resolve missing images BEFORE stopping anything, a missing image must not
    # leave a running stack torn down. Missing images are never auto-built: a
    # local build must not carry a release tag. Fail loud with the pull command.
    missing = _missing_images(env_file)
    if missing:
        tag = env_data.get("SHS_STUDIO_VERSION", "latest")
        if _find_repo_root(env_file):
            _fail_missing_images(missing, tag)
            warn("No changes applied, services left as they were.")
            return
        info(f"Pulling missing images: {', '.join(missing)}")
        if not _pull_images(missing, tag):
            _fail_missing_images(missing, tag)
            warn("No changes applied, services left as they were.")
            return

    # Stop workers directly, catches orphans whose profiles were removed from .env.
    # 'ps -a' so Created/Exited workers are removed too (avoids name collisions on
    # recreate); 'rm -f' force-removes running ones ('-s' is not a valid rm flag).
    _, ps_out = run_quiet(["docker", "ps", "-aq", "--filter", "name=studio-worker-"])
    all_worker_ctrs = [c.strip() for c in ps_out.strip().splitlines() if c.strip()]
    if all_worker_ctrs:
        info(f"Stopping {len(all_worker_ctrs)} worker container(s)...")
        run_quiet(["docker", "rm", "-f"] + all_worker_ctrs, timeout=30)

    if include_api:
        _stop_api_ui_containers()

    info("Starting services...")
    try:
        if include_api:
            run(
                _apply_scale_flags(base + ["up", "-d", "--remove-orphans"], env_data),
                timeout=120,
            )
        else:
            # Workers only, explicit service names so API/UI/nginx are never touched
            worker_services = [
                svc for svc in SCALE_PROFILES.values() if svc in new_profiles
            ]
            if worker_services:
                run(
                    _apply_scale_flags(
                        base + ["up", "-d"] + list(dict.fromkeys(worker_services)),
                        env_data,
                    ),
                    timeout=120,
                )
            else:
                info("No active worker profiles, nothing to start.")
        ok("Done")
    except Exception as e:
        error(f"Docker Compose failed: {e}")


# ---------------------------------------------------------------------------
# Submenus
# ---------------------------------------------------------------------------


def _submenu_setup(context: str, env_file: Path) -> None:
    """Setup submenu, runs the wizard then offers Apply now / Skip."""
    old_profiles = (
        read_env(env_file).get("COMPOSE_PROFILES", "") if env_file.exists() else ""
    )
    if wizard(context, env_file):
        apply_options = [
            f"Apply now  {_dim('restarts changed services, brief downtime possible')}",
            f"Skip       {_dim('apply on next manual restart')}",
        ]
        pick = _interactive_single(
            "Apply changes?", apply_options, default=0, nav=False
        )
        if pick == 0:
            _restart_for_setup(
                context, env_file, include_api=True, old_profiles=old_profiles
            )


def _cmd_scale_api_ui(env_file: Path) -> None:
    """Interactively change API/UI replica counts and regenerate nginx config."""
    state = SetupState(env_file)
    _section_api_ui_scaling(state)

    env_data = read_env(env_file)
    old_api = int(env_data.get("CONSOLE_API_REPLICAS", "1"))
    old_ui = int(env_data.get("CONSOLE_UI_REPLICAS", "1"))
    if state.api_replicas == old_api and state.ui_replicas == old_ui:
        info("No changes")
        return

    set_env_value(env_file, "CONSOLE_API_REPLICAS", str(state.api_replicas))
    set_env_value(env_file, "CONSOLE_UI_REPLICAS", str(state.ui_replicas))
    set_env_value(env_file, "SHS_NGINX_PORT", str(state.nginx_port))
    _write_override_and_nginx(state)
    ok("Scaling updated")
    if not _interactive_yn("Restart API + UI to apply?", default=True):
        return
    warn_header("This will restart API, UI, and nginx. Workers will keep running")
    _stop_api_ui_containers()
    env_data = read_env(env_file)
    run(
        _apply_scale_flags(
            compose_cmd(env_file) + ["up", "-d", "--remove-orphans"], env_data
        ),
        timeout=120,
    )
    ok("Done")


def _submenu_services(context: str, env_file: Path) -> None:
    """Services submenu, daily ops: start/stop/restart all, health, logs, links."""
    options = [
        f"Start all      {_dim('pull/build if needed, then start')}",
        f"Stop all       {_dim('stop all containers')}",
        f"Restart all    {_dim('restart every service')}",
        f"Health         {_dim('API + worker status')}",
        f"View logs      {_dim('recent logs')}",
        f"Stream logs    {_dim('follow live (Ctrl-C to stop)')}",
        f"Links          {_dim('UI, API, docs URLs')}",
    ]
    idx = _interactive_single("Services", options, default=0)
    if idx == 0:
        cmd_start(context, env_file)
    elif idx == 1:
        cmd_stop(context, env_file)
    elif idx == 2:
        cmd_restart(context, env_file, None)
    elif idx == 3:
        cmd_health(context, env_file)
    elif idx == 4:
        _log_picker(context, env_file, follow=False)
    elif idx == 5:
        _log_picker(context, env_file, follow=True)
    elif idx == 6:
        cmd_links(context, env_file)


def _build_log_options(
    services: list[str],
) -> tuple[list[str], list[str | list[str] | None]]:
    """Build log menu labels and corresponding service targets.

    When multiple api-N or ui-N instances are running, injects group entries
    ("All API", "All UI") so the user can tail all replicas at once.

    Returns (labels, targets) where each target is:
      None         → all services
      str          → single service name
      list[str]    → group of service names passed together to docker compose logs
    """
    labels: list[str] = ["All services"]
    targets: list[str | list[str] | None] = [None]

    api_replicas = [
        s for s in services if s == "api" or (s.startswith("api-") and s[4:].isdigit())
    ]
    ui_replicas = [
        s for s in services if s == "ui" or (s.startswith("ui-") and s[3:].isdigit())
    ]

    if len(api_replicas) > 1:
        labels.append(f"All API        {_dim(f'{len(api_replicas)} replicas')}")
        targets.append(api_replicas)
    if len(ui_replicas) > 1:
        labels.append(f"All UI         {_dim(f'{len(ui_replicas)} replicas')}")
        targets.append(ui_replicas)

    for svc in services:
        labels.append(svc)
        targets.append(svc)

    return labels, targets


def _submenu_images(context: str, env_file: Path) -> None:
    """Images submenu - build, upgrade, rollback."""
    options = [
        f"Build          {_dim('build configured images + pull postgres')}",
        f"Upgrade        {_dim('pull newer version from registry')}",
        f"Rollback       {_dim('switch to an older version')}",
    ]
    idx = _interactive_single("Images", options, default=0)
    if idx == 0:
        cmd_build(env_file, None)
    elif idx in (1, 2):
        cmd_upgrade(context, env_file)


def _owns_postgres(env_data: dict) -> bool:
    """True if SHS_DATABASE_URL points at a postgres instance we manage.

    External DBs (CloudSQL, RDS, etc., or a CloudSQL Auth Proxy sidecar) own
    their own backup tooling, console refuses backup/restore in those cases
    rather than producing dumps that can't be restored back to the source.
    """
    url = env_data.get("SHS_DATABASE_URL", "")
    m = re.search(r"@([^:/]+)", url)
    if not m:
        return False
    host = m.group(1).lower()
    return host in ("postgres", "localhost", "127.0.0.1")


def _submenu_backup(context: str, env_file: Path) -> None:
    """Archive submenu - backup and restore."""
    if not _owns_postgres(read_env(env_file)):
        warn("This Studio uses an external database (CloudSQL, RDS, etc.).")
        info("Use your database provider's backup and restore tooling.")
        info(
            _dim(
                "(Org file backup/restore is not yet supported for external DB setups.)"
            )
        )
        return

    options = [
        f"Backup all     {_dim('database + .env + org files (potentially large files)')}",
        f"Backup DB      {_dim('pg_dump + .env')}",
        f"Backup orgs    {_dim('organization files only')}",
        f"Restore DB     {_dim('restore database from backup')}",
        f"Restore orgs   {_dim('restore organization files from backup')}",
    ]
    br_idx = _interactive_single("Backup", options, default=0)
    if br_idx == 0:
        cmd_backup(context, env_file, what="all")
    elif br_idx == 1:
        cmd_backup(context, env_file, what="db")
    elif br_idx == 2:
        cmd_backup(context, env_file, what="orgs")
    elif br_idx == 3:
        db_file = _pick_db_file(context, env_file)
        if db_file:
            cmd_restore_db(context, env_file, db_file)
    elif br_idx == 4:
        path = _prompt("Backup path (enter for latest)", "")
        if _interactive_yn(
            "Restore org files? This will overwrite current files.", default=False
        ):
            cmd_restore(context, env_file, path or None, what="orgs")


def cmd_db_role(context: str, env_file: Path) -> None:
    """Show/enable the restricted runtime DB role (shs_app cutover).

    Console only writes SHS_DATABASE_APP_URL, the API's bootstrap provisions
    the role, grants, and RLS posture from it on every boot. SHS_DATABASE_URL
    stays privileged; console's own psql/dump/restore tooling keeps using it.
    """
    env_data = read_env(env_file)

    if _app_db_url(env_data):
        ok("Restricted DB role is configured (SHS_DATABASE_APP_URL is set).")
        info("The API provisions the role and re-applies RLS policies on every boot.")
        info("Check Services → Health for the live posture.")
        return

    db_url = env_data.get("SHS_DATABASE_URL", "") or os.environ.get(
        "SHS_DATABASE_URL", ""
    )
    if not db_url:
        error("No SHS_DATABASE_URL found. Configure the database first.")
        return

    warn_header("The API currently connects as the privileged DB role; RLS is inert")
    info("Enabling writes SHS_DATABASE_APP_URL (role shs_app) next to SHS_DATABASE_URL.")
    info("The API provisions the role itself on next boot; console runs no SQL.")
    info(_dim("Requires a studio image with restricted-role support; older images ignore it."))
    if not _owns_postgres(env_data):
        warn(
            "External database: the SHS_DATABASE_URL role must have CREATEROLE, "
            "or the API's boot fails closed while provisioning shs_app."
        )
    print()

    if not _interactive_yn("Enable the restricted DB role?", default=False):
        info("Skipped, no changes made.")
        return

    set_env_value(env_file, "SHS_DATABASE_APP_URL", derive_app_db_url(db_url))
    ok("SHS_DATABASE_APP_URL written to .env")

    if context == "host":
        env_data = read_env(env_file)
        if _interactive_yn(
            "Apply now? Restarts services so the API boots on the restricted role.",
            default=True,
        ):
            run(
                _apply_scale_flags(
                    compose_cmd(env_file) + ["up", "-d", "--remove-orphans"], env_data
                ),
                timeout=120,
            )
            ok("Services restarted. Check Services → Health for the RLS posture.")
        else:
            info("Takes effect on the next restart of the API.")
    else:
        warn(
            "Restart this container from the host to apply, provisioning runs in "
            "the container entrypoint, not under supervisord."
        )
        print(f"    {_bold('docker restart <container>')}   {_dim('(or stop/start the pod on RunPod)')}")


def _submenu_advanced(context: str, env_file: Path) -> None:
    """Advanced submenu, scale API/UI, per-service ops, Cloudflare ops."""
    options = [
        f"Scale API/UI      {_dim('set replica count, enable/disable nginx LB')}",
        f"Worker kit        {_dim('setup commands for a worker on another machine')}",
        f"Start one         {_dim('start a stopped service')}",
        f"Stop one          {_dim('stop a running service')}",
        f"Restart one       {_dim('restart a single service')}",
        f"Show .env         {_dim('current configuration values')}",
        f"DB role           {_dim('restricted runtime role (RLS): status · enable')}",
        f"Cloudflare        {_dim('tunnel · routes · IP rules · Access')}",
    ]
    idx = _interactive_single("Advanced", options, default=0)
    if idx == 0:
        _cmd_scale_api_ui(env_file)
    elif idx == 1:
        from .commands_kit import cmd_worker_kit

        cmd_worker_kit(context, env_file)
    elif idx in (2, 3, 4):
        # Service names (not container names): each action targets a whole
        # service type, restarting/stopping all its replicas at once.
        services = _get_running_services(context, env_file)
        if not services:
            warn("No services found")
        else:
            action = {2: "Start", 3: "Stop", 4: "Restart"}[idx]
            pick = _interactive_single(f"{action} which service?", services, default=0)
            svc = services[pick]
            if idx == 2:
                run(compose_cmd(env_file) + ["start", svc], timeout=60)
            elif idx == 3:
                run(compose_cmd(env_file) + ["stop", svc], timeout=60)
            else:
                cmd_restart(context, env_file, svc)
    elif idx == 5:
        cmd_show_config(context, env_file)
    elif idx == 6:
        cmd_db_role(context, env_file)
    elif idx == 7:
        _submenu_cloudflare(context, env_file)


def _submenu_cloudflare(context: str, env_file: Path) -> None:
    """Cloudflare ops submenu."""
    from .cloudflare.cf_wizard import cf_full_setup, update_domain, update_ip_rules

    env_data = read_env(env_file)
    tunnel_token = env_data.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    api_token = env_data.get("CLOUDFLARE_API_TOKEN", "")
    domain = env_data.get("SHS_PUBLIC_BASE_URL", "")
    api_domain = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "")
    ip_mode = env_data.get("CONSOLE_IP_RESTRICT_MODE", "none")
    profiles = env_data.get("COMPOSE_PROFILES", "")
    is_docker_mode = bool(tunnel_token) or "cloudflared" in profiles
    is_split = api_domain.startswith("https://") and api_domain != domain

    options = [
        f"Full setup (API)  {_dim('create tunnel + routes + Access app + IP policy')}",
        f"Status            {_dim('token, domain, tunnel running?')}",
        f"Test              {_dim('curl public URL health endpoint')}",
        f"Update IP rules   {_dim('add/remove IPs from bypass policy')}",
        f"Update token      {_dim('change tunnel token (manual)')}",
        f"Update domain     {_dim('change public domain + cleanup stale DNS')}",
        f"Start tunnel      {_dim('compose up cloudflared')}",
        f"Stop tunnel       {_dim('compose stop cloudflared')}",
        f"Tunnel logs       {_dim('follow cloudflared container logs')}",
    ]
    idx = _interactive_single("Cloudflare", options, default=0)

    if idx == 0:
        cf_full_setup(env_file)

    elif idx == 1:
        print()
        print(
            f"  {_bold('API token:')}    "
            f"{mask_value('CLOUDFLARE_API_TOKEN', api_token) if api_token else _dim('not set')}"
        )
        print(
            f"  {_bold('Tunnel token:')} "
            f"{mask_value('CLOUDFLARE_TUNNEL_TOKEN', tunnel_token) if tunnel_token else _dim('not set')}"
        )
        print(
            f"  {_bold('Tunnel ID:')}    {env_data.get('CLOUDFLARE_TUNNEL_ID', _dim('not set'))}"
        )
        if is_split:
            print(f"  {_bold('UI domain:')}    {domain}")
            print(f"  {_bold('API domain:')}   {api_domain}")
            print(
                f"  {_bold('UI app:')}       {env_data.get('CLOUDFLARE_ACCESS_APP_ID', _dim('not set'))}"
            )
            print(
                f"  {_bold('API app:')}      {env_data.get('CLOUDFLARE_ACCESS_API_APP_ID', _dim('not set'))}"
            )
        else:
            print(f"  {_bold('Domain:')}       {domain if domain else _dim('not set')}")
            print(
                f"  {_bold('Access app:')}   {env_data.get('CLOUDFLARE_ACCESS_APP_ID', _dim('not set'))}"
            )
        print(f"  {_bold('IP restrict:')}  {ip_mode}")
        if is_docker_mode:
            rc, out = run_quiet(
                compose_cmd(env_file) + ["ps", "cloudflared", "--format", "{{.State}}"],
                timeout=10,
            )
            if rc == 0 and "running" in out.lower():
                tunnel_status = _green("running")
            else:
                tunnel_status = _yellow(
                    "not running  (use Start tunnel to bring it up)"
                )
            print(f"  {_bold('cloudflared:')}  {tunnel_status}")
        else:
            print(f"  {_bold('cloudflared:')}  {_dim('not configured')}")
        print()

    elif idx == 2:
        if not domain or not domain.startswith("https://"):
            warn("No public domain configured")
            return
        base = domain.rstrip("/")
        routes = [
            ("UI health (api)", f"{base}/health"),
            ("UI WebSocket", f"{base}/ws"),
            ("UI", base),
        ]
        if is_split:
            api_base = api_domain.rstrip("/")
            routes.extend(
                [
                    ("API health", f"{api_base}/health"),
                    ("API WebSocket", f"{api_base}/ws"),
                ]
            )
        for name, url in routes:
            curl_cmd = [
                "curl",
                "-sf",
                "--max-time",
                "10",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                url,
            ]
            rc, code = run_quiet(curl_cmd, timeout=15)
            if rc == 0 and code and code[0] in ("2", "3"):
                ok(f"{name:14s} {url}  → {code}")
            else:
                warn(f"{name:14s} {url}  → failed ({code or 'no response'})")

    elif idx == 3:
        update_ip_rules(env_file)

    elif idx == 4:
        import getpass

        try:
            new_token = getpass.getpass(f"▸ New tunnel token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if new_token:
            set_env_value(env_file, "CLOUDFLARE_TUNNEL_TOKEN", new_token)
            if "cloudflared" not in profiles:
                new_profiles = (profiles + ",cloudflared").strip(",")
                set_env_value(env_file, "COMPOSE_PROFILES", new_profiles)
            ok("Token updated")
            if _interactive_yn("Restart tunnel to apply?", default=True):
                base_cmd = compose_cmd(env_file)
                run_quiet(base_cmd + ["rm", "-sf", "cloudflared"], timeout=30)
                run(base_cmd + ["up", "-d", "cloudflared"], timeout=60)
        else:
            warn("No token entered")

    elif idx == 5:
        if api_token:
            update_domain(env_file)
        else:
            new_domain = _prompt(
                "New public domain (e.g. https://app.yourdomain.com)", domain
            )
            while new_domain and not new_domain.startswith("https://"):
                warn("Must start with https://")
                new_domain = _prompt("New public domain", domain)
            if new_domain:
                set_env_value(env_file, "SHS_PUBLIC_BASE_URL", new_domain.rstrip("/"))
                from .commands_container import _sync_derived_urls

                _sync_derived_urls(env_file)
                ok(f"Domain updated to {new_domain.rstrip('/')}")
                warn("API token not set, DNS records and Access app not updated")
            else:
                warn("No domain entered")

    elif idx == 6:
        if not tunnel_token:
            warn("No tunnel token set. Run 'Full setup (API)' first.")
            return
        from .cloudflare.cf_wizard import ingress_shape_mismatch
        from .env import detect_shape

        mismatch = ingress_shape_mismatch(detect_shape(env_file) or "split", env_data)
        if mismatch:
            error(mismatch)
            return
        if "cloudflared" not in profiles:
            new_profiles = (profiles + ",cloudflared").strip(",")
            set_env_value(env_file, "COMPOSE_PROFILES", new_profiles)
        info("Starting cloudflared...")
        run(compose_cmd(env_file) + ["up", "-d", "cloudflared"], timeout=60)
        ok("Tunnel started")

    elif idx == 7:
        info("Stopping cloudflared...")
        run_quiet(compose_cmd(env_file) + ["stop", "cloudflared"], timeout=30)
        ok("Tunnel stopped")

    elif idx == 8:
        info("Streaming cloudflared logs  (Ctrl-C to stop)")
        try:
            run(
                compose_cmd(env_file) + ["logs", "-f", "cloudflared"],
                timeout=None,
                check=False,
            )
        except KeyboardInterrupt:
            print()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_build(env_file: Path, images: list[str] | None, confirm: bool = True) -> None:
    """Build Docker images from local source + pull third-party."""
    repo = _find_repo_root(env_file)
    if repo is None:
        warn("Building from source requires CONSOLE_REPO_ROOT to be set.")
        warn("Add it to ~/.studio/.env or export it in your shell:")
        warn("  CONSOLE_REPO_ROOT=/path/to/studio")
        warn("In a standard install, Studio images are pulled from the registry.")
        return

    env_data = read_env(env_file) if env_file.exists() else {}
    tag = env_data.get("SHS_STUDIO_VERSION", "latest")
    prefix = "ghcr.io/selfhosthub"

    # Pull third-party images (postgres etc.)
    for img in THIRD_PARTY_IMAGES:
        rc, _ = run_quiet(["docker", "image", "inspect", img])
        if rc != 0:
            info(f"Pulling {img}...")
            run(["docker", "pull", img], timeout=120)
            ok(f"{img}")
        else:
            ok(f"{img} (already local)")

    # Determine which images to build
    if images:
        # Normalize: accept "api" or "studio-api", commas or spaces
        raw: list[str] = []
        for img in images:
            raw.extend(part.strip() for part in img.split(",") if part.strip())
        targets: list[str] = []
        for img in raw:
            canonical = img if img.startswith("studio-") else f"studio-{img}"
            if canonical not in IMAGE_BUILD_CONFIG:
                fatal(
                    f"Unknown image: {img}. Available: {', '.join(IMAGE_BUILD_CONFIG)}"
                )
            targets.append(canonical)
    else:
        # Build only what was configured
        targets = _images_from_env(env_file)

    total = len(targets)

    if confirm:
        info(f"Images to build ({total}):  {', '.join(targets)}  [tag: {tag}]")
        info(f"Source: {repo}")
        warn("This may take several minutes.")
        if not _interactive_yn("Build now?", default=True, nav=False):
            info("Cancelled.")
            return

    info(f"Building {total} image(s), tag={tag}")
    print()

    for i, name in enumerate(targets, 1):
        dockerfile, context_dir = IMAGE_BUILD_CONFIG[name]
        full_tag = f"{prefix}/{name}:{tag}"
        print(f"  [{i}/{total}] {_bold(full_tag)}")
        env_data = read_env(env_file)
        build_args = []
        if name == "ui":
            public_api = env_data.get("SHS_PUBLIC_API_URL", "") or env_data.get(
                "SHS_API_BASE_URL", ""
            )
            for var in (
                "SHS_API_BASE_URL",
                "SHS_WS_URL",
                "NEXT_PUBLIC_WS_URL",
            ):
                val = env_data.get(var, "")
                if val:
                    build_args += ["--build-arg", f"{var}={val}"]
            if public_api:
                build_args += ["--build-arg", f"NEXT_PUBLIC_API_URL={public_api}"]
        try:
            run(
                [
                    "docker",
                    "build",
                    "--build-context",
                    f"contracts={repo / 'contracts'}",
                    "-f",
                    str(repo / dockerfile),
                    "-t",
                    full_tag,
                    *build_args,
                    str(repo / context_dir),
                ],
                timeout=600,
            )
        except Exception as e:
            error(f"Build failed for {name}: {e}")
            return
        ok(f"  {full_tag}")

    print()
    ok(f"All {total} image(s) built")

    # Offer to restart running services so they pick up the new images
    base = compose_cmd(env_file)
    rc, out = run_quiet(base + ["ps", "--format", "{{.Names}}"])
    if rc == 0 and out.strip():
        running = [line.strip() for line in out.strip().splitlines() if line.strip()]
        if running:
            warn_header(
                "Restart required to apply new images. Studio will be briefly unavailable"
            )
            info(f"Running services: {', '.join(running)}")
            if _interactive_yn("Restart now?", default=True, nav=False):
                run(base + ["up", "-d", "--force-recreate"], timeout=120)
                ok("Services restarted")
            else:
                info("Images built. Restart manually when ready: Services → Restart")
    else:
        info("No running services to restart")


def _get_repo_version(env_file: Path | None = None) -> str | None:
    """Read SHS_STUDIO_VERSION from the repo's deploy/.env.example."""
    repo = _find_repo_root(env_file)
    if not repo:
        return None
    example = repo / "deploy" / ".env.example"
    if not example.exists():
        return None
    for line in example.read_text().splitlines():
        line = line.strip()
        if line.startswith("SHS_STUDIO_VERSION="):
            return line.partition("=")[2].strip()
    return None


def _get_latest_registry_version() -> str | None:
    """Get the latest semver tag from GHCR."""
    versions = _fetch_available_versions(limit=1)
    return versions[0] if versions else None


def cmd_start(context: str, env_file: Path) -> None:
    """Start services. Pulls missing images; never builds them implicitly."""
    if context == "host":
        _validate_env(env_file)
        env_data = read_env(env_file)

        if "cloudflared" in env_data.get("COMPOSE_PROFILES", ""):
            from .cloudflare.cf_wizard import ingress_shape_mismatch
            from .env import detect_shape

            mismatch = ingress_shape_mismatch(detect_shape(env_file) or "split", env_data)
            if mismatch:
                error(mismatch)
                return False

        target_tag = env_data.get("SHS_STUDIO_VERSION", "")
        if mv.check_and_block(
            env_file, mv.parse_target_major(target_tag), "install", context, target_tag
        ):
            return False

        missing = _missing_images(env_file)
        if missing:
            version = env_data.get("SHS_STUDIO_VERSION", "")
            if _find_repo_root(env_file):
                _fail_missing_images(missing, version or "latest")
                return False
            info(
                f"Pulling missing images from registry"
                + (f" (v{version})" if version else "")
                + f": {', '.join(missing)}"
            )
            try:
                run(compose_cmd(env_file) + ["pull"], timeout=600)
            except Exception:
                error("The download was interrupted before it finished.")
                warn("Run Start again, it should pick up where it left off.")
                return False
            ok("Images pulled")

        # Ensure nginx:alpine is available (always used)
        rc_ng, _ = run_quiet(["docker", "image", "inspect", "nginx:alpine"])
        if rc_ng != 0:
            info("Pulling nginx:alpine...")
            run(["docker", "pull", "nginx:alpine"], timeout=120)
            ok("nginx:alpine")

        info("Starting Studio via Docker Compose...")
        up_cmd = _apply_scale_flags(
            compose_cmd(env_file) + ["up", "-d", "--remove-orphans"], env_data
        )

        try:
            run(up_cmd, timeout=120)
        except Exception as e:
            error(f"Docker Compose failed: {e}")
            warn(
                "If images are missing, use Images → Upgrade to pull from the registry."
            )
            warn("If running from source, use Images → Build to build locally.")
            return False

        # Health check always via nginx
        import time

        nginx_port = env_data.get("SHS_NGINX_PORT", "80")
        health_url = f"http://localhost:{nginx_port}/health"

        info("Waiting for API...")
        healthy = False
        for _ in range(60):
            rc, _ = run_quiet(["curl", "-sf", health_url])
            if rc == 0:
                healthy = True
                break
            time.sleep(2)

        if healthy:
            ok("Studio started")
        else:
            warn(
                "API not responding yet - check logs: studio-console → Configure → View logs"
            )

        # First boot: create super admin account directly in Postgres
        if healthy:
            _bootstrap_first_admin(env_file)

        return healthy
    else:
        info("Starting all services via supervisorctl...")
        run(["supervisorctl", "start", "all"], timeout=30)
        ok("All services started")
        return True


def cmd_stop(context: str, env_file: Path) -> None:
    """Stop services."""
    if context == "host":
        _validate_env(env_file)
        info("Stopping Studio...")
        run(compose_cmd(env_file) + ["stop"], timeout=60)
        ok("Studio stopped")
    else:
        info("Stopping all services...")
        run(["supervisorctl", "stop", "all"], timeout=30)
        ok("All services stopped")


def _api_service_name(env_data: dict) -> str:
    """Return the running API service name: api-1 in multi-replica mode, api otherwise."""
    return "api-1" if int(env_data.get("CONSOLE_API_REPLICAS", "1")) > 1 else "api"


class _BootstrapPlan:
    """Exec plumbing for admin bootstrap, per deployment shape."""

    def __init__(self, base, api_svc, pg_svc, api_base, exec_flags):
        self.base = base
        self.api_svc = api_svc
        self.pg_svc = pg_svc
        self.api_base = api_base
        self.exec_flags = exec_flags  # compose needs -T; docker exec rejects it


def _split_plan(env_file: Path, env_data: dict) -> "_BootstrapPlan":
    return _BootstrapPlan(
        base=compose_cmd(env_file),
        api_svc=_api_service_name(env_data),
        pg_svc="postgres",
        api_base=f"http://localhost:{env_data.get('SHS_NGINX_PORT', '80')}/api/v1",
        exec_flags=["-T"],
    )


def _full_plan(container: str) -> "_BootstrapPlan":
    # base omits "exec"; helpers append it.
    return _BootstrapPlan(
        base=["docker"],
        api_svc=container,
        pg_svc=container,
        api_base="http://localhost:8000/api/v1",
        exec_flags=[],
    )


def _core_plan(
    container: str, pg_container: str, nginx_port: int | str = 80
) -> "_BootstrapPlan":
    # Core's API runs in `container`, but Postgres is an external sidecar
    # (`pg_container`), so password hashing execs into the API container while
    # every psql runs against the sidecar. base omits "exec"; helpers append it.
    # Core launches with publish_internal=False, so port 8000 is not published:
    # reach the API through the front door.
    return _BootstrapPlan(
        base=["docker"],
        api_svc=container,
        pg_svc=pg_container,
        api_base=f"http://localhost:{nginx_port}/api/v1",
        exec_flags=[],
    )


def _read_env_for_bootstrap(env_file: Path, plan: "_BootstrapPlan | None") -> dict:
    """Read .env for the bootstrap flow, from the container for exec plans.

    full/core write .env root-owned 0600 *inside* the container, so a host user
    (non-root on Ubuntu/runpod/vast; UID-namespaced on Mac/Win) can't read it.
    docker-exec plans (base == ["docker"]) cat it from the API container; split
    reads the user-owned host file directly.
    """
    if plan is not None and plan.base == ["docker"]:
        rc, out = run_quiet(
            ["docker", "exec", plan.api_svc, "cat", "/workspace/.env"], timeout=10
        )
        if rc != 0:
            return {}
        result: dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
        return result
    return read_env(env_file)


def _unset_env_for_bootstrap(
    env_file: Path, keys: list[str], plan: "_BootstrapPlan | None"
) -> None:
    """Strip keys from .env, inside the container for exec plans (root-owned file)."""
    if plan is not None and plan.base == ["docker"]:
        pattern = "|".join(k for k in keys)
        run_quiet(
            [
                "docker", "exec", plan.api_svc, "sh", "-c",
                f"grep -Ev '^({pattern})=' /workspace/.env > /workspace/.env.tmp "
                f"&& mv /workspace/.env.tmp /workspace/.env && chmod 0600 /workspace/.env",
            ],
            timeout=10,
        )
        return
    unset_env_values(env_file, keys)


def _super_admin_exists(plan: "_BootstrapPlan", env_data: dict) -> bool:
    """True if a super_admin user is already in the DB, the bootstrap gate."""
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    rc, out = run_quiet(
        plan.base
        + [
            "exec",
            *plan.exec_flags,
            plan.pg_svc,
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-tA",
            "-c",
            "SELECT 1 FROM users WHERE role = 'super_admin' LIMIT 1",
        ],
        timeout=10,
    )
    return rc == 0 and out.strip() == "1"


def _bootstrap_first_admin(
    env_file: Path, plan: "_BootstrapPlan | None" = None
) -> bool:
    """First-boot super admin + default org admin. Idempotent."""
    # Skip if a super_admin already exists.
    env_data = _read_env_for_bootstrap(env_file, plan)
    check_plan = plan or _split_plan(env_file, env_data)
    if _super_admin_exists(check_plan, env_data):
        return True

    print()
    info("First boot - create your super admin account (username: super_admin)")
    admin_email = os.environ.get("SHS_ADMIN_EMAIL", "")
    admin_password = os.environ.get("SHS_ADMIN_PASSWORD", "")
    if not admin_email:
        while not admin_email or "@" not in admin_email:
            admin_email = _prompt("super_admin email", "super-admin@example.com")
            if not admin_email or "@" not in admin_email:
                warn("Enter a valid email address")
    if not admin_password:
        admin_password = _prompt_password("super_admin password")

    print()
    info("Create the default org's admin account (username: admin)")
    default_admin_email = os.environ.get("CONSOLE_DEFAULT_ADMIN_EMAIL", "")
    default_admin_password = os.environ.get("CONSOLE_DEFAULT_ADMIN_PASSWORD", "")
    default_email_default = (
        "" if admin_email == "admin@example.com" else "admin@example.com"
    )
    if not default_admin_email:
        while True:
            default_admin_email = _prompt("admin email", default_email_default)
            if not default_admin_email or "@" not in default_admin_email:
                warn("Enter a valid email address")
                continue
            if default_admin_email == admin_email:
                warn("Org admin email must differ from the super admin email")
                continue
            break
    elif default_admin_email == admin_email:
        error(
            "CONSOLE_DEFAULT_ADMIN_EMAIL must differ from SHS_ADMIN_EMAIL "
            f"({admin_email})"
        )
        return False
    if not default_admin_password:
        default_admin_password = _prompt_password("admin password")

    # Prompt for entitlement token; _create_admin_direct reads it from env.
    if not os.environ.get("SHS_ENTITLEMENT_TOKEN") and not _read_env_for_bootstrap(
        env_file, plan
    ).get("SHS_ENTITLEMENT_TOKEN"):
        print()
        info("Entitlement token (enables the Plus catalog; leave blank to skip)")
        token = _prompt("Entitlement token", "").strip()
        if token:
            os.environ["SHS_ENTITLEMENT_TOKEN"] = token

    info("Creating super admin account...")
    super_admin_ok = False
    try:
        _create_admin_direct(env_file, admin_email, admin_password, plan)
        ok(f"Super admin account created: {admin_email} (username: super_admin)")
        super_admin_ok = True
    except Exception as e:
        error(str(e))
        error("Try: Settings → Reset password")

    info("Creating default org + admin account...")
    org_admin_ok = False
    try:
        _create_default_org_admin(
            env_file, default_admin_email, default_admin_password, plan
        )
        ok(
            f"Admin account created: {default_admin_email} (username: admin, org: default)"
        )
        org_admin_ok = True
    except Exception as e:
        error(str(e))

    if super_admin_ok and org_admin_ok:
        # Strip transient bootstrap inputs from .env.
        _unset_env_for_bootstrap(
            env_file,
            [
                "SHS_ADMIN_EMAIL",
                "SHS_ADMIN_PASSWORD",
                "CONSOLE_DEFAULT_ADMIN_EMAIL",
                "CONSOLE_DEFAULT_ADMIN_PASSWORD",
                "SHS_ENTITLEMENT_TOKEN",
            ],
            plan,
        )
        return True
    warn("Bootstrap incomplete, will retry account creation on next start.")
    return False


def _create_admin_direct(
    env_file: Path, email: str, password: str, plan: "_BootstrapPlan | None" = None
) -> None:
    """Create super admin: hash via API container, insert via psql."""
    import uuid

    env_data = _read_env_for_bootstrap(env_file, plan)
    plan = plan or _split_plan(env_file, env_data)
    base = plan.base
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    api_svc = plan.api_svc

    # 1. Hash password via bcrypt in the API container
    rc, hashed = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            "-e",
            f"_PW={password}",
            api_svc,
            "python",
            "-c",
            "import bcrypt,os; pw=os.environ['_PW'].encode(); "
            "print(bcrypt.hashpw(pw,bcrypt.gensalt()).decode())",
        ],
        timeout=15,
    )
    if rc != 0 or not hashed.strip():
        raise RuntimeError(f"Failed to hash password (rc={rc}): {hashed}")
    hashed_pw = hashed.strip()

    # 2. Get system org ID
    rc, org_out = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            plan.pg_svc,
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-t",
            "-A",
            "-c",
            "SELECT id FROM organizations WHERE slug = 'system' LIMIT 1",
        ],
        timeout=10,
    )
    if rc != 0 or not org_out.strip():
        raise RuntimeError(f"System org not found (rc={rc}): {org_out}")
    org_id = org_out.strip()

    # 2b. Ensure system org is flagged as staging
    rc, upd_out = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            plan.pg_svc,
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-c",
            "UPDATE organizations SET is_staging = TRUE WHERE slug = 'system'",
        ],
        timeout=10,
    )
    if rc != 0:
        raise RuntimeError(
            f"Failed to set is_staging on system org (rc={rc}): {upd_out}. "
            "The Studio schema may be missing the is_staging column; upgrade Studio."
        )

    # 3. Insert admin user
    admin_id = str(uuid.uuid4())
    sql = (
        "INSERT INTO users "
        "(id, username, email, hashed_password, role, "
        "is_active, is_public, first_name, last_name, "
        "organization_id, created_at, updated_at) "
        "VALUES ("
        f"'{admin_id}', 'super_admin', '{email}', "
        f"$hash${hashed_pw}$hash$, 'super_admin', true, false, "
        f"'System', 'Administrator', '{org_id}', NOW(), NOW()"
        ") ON CONFLICT (username) DO NOTHING"
    )
    rc, out = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            plan.pg_svc,
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-c",
            sql,
        ],
        timeout=10,
    )
    if rc != 0:
        raise RuntimeError(f"INSERT failed (rc={rc}): {out}")

    # 4. Create ENTITLEMENT_TOKEN secret via API (encrypted correctly by repository)
    api_base = plan.api_base
    entitlement_token = os.environ.get("SHS_ENTITLEMENT_TOKEN") or env_data.get(
        "SHS_ENTITLEMENT_TOKEN", ""
    )

    rc, login_out = run_quiet(
        [
            "curl",
            "-sf",
            "-X",
            "POST",
            f"{api_base}/auth/token",
            "-d",
            f"username={email}&password={password}",
        ],
        timeout=15,
    )
    if rc != 0 or not login_out.strip():
        warn(
            "Could not log in to create ENTITLEMENT_TOKEN secret - add it manually via Settings → Secrets"
        )
        return

    try:
        access_token = json.loads(login_out).get("access_token", "")
    except Exception:
        access_token = ""

    if not access_token:
        warn(
            "Login response missing access_token - ENTITLEMENT_TOKEN secret not created"
        )
        return

    secret_data: dict = {"token": entitlement_token} if entitlement_token else {}
    payload = json.dumps(
        {
            "name": "ENTITLEMENT_TOKEN",
            "secret_type": "bearer",
            "secret_data": secret_data,
            "is_active": bool(entitlement_token),
        }
    )
    rc, secret_out = run_quiet(
        [
            "curl",
            "-sf",
            "-X",
            "POST",
            f"{api_base}/organizations/secrets",
            "-H",
            "Content-Type: application/json",
            "-H",
            f"Authorization: Bearer {access_token}",
            "-d",
            payload,
        ],
        timeout=15,
    )
    if rc != 0:
        warn(
            "Failed to create ENTITLEMENT_TOKEN secret - add it manually via Settings → Secrets"
        )


def _create_default_org_admin(
    env_file: Path, email: str, password: str, plan: "_BootstrapPlan | None" = None
) -> None:
    """Create the 'default' org (is_staging=TRUE) and its immutable 'admin' user."""
    import uuid

    env_data = _read_env_for_bootstrap(env_file, plan)
    plan = plan or _split_plan(env_file, env_data)
    base = plan.base
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    api_svc = plan.api_svc

    # 1. Hash password via bcrypt in the API container
    rc, hashed = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            "-e",
            f"_PW={password}",
            api_svc,
            "python",
            "-c",
            "import bcrypt,os; pw=os.environ['_PW'].encode(); "
            "print(bcrypt.hashpw(pw,bcrypt.gensalt()).decode())",
        ],
        timeout=15,
    )
    if rc != 0 or not hashed.strip():
        raise RuntimeError(f"Failed to hash password (rc={rc}): {hashed}")
    hashed_pw = hashed.strip()

    # 2. Insert 'default' org with is_staging=TRUE (idempotent)
    org_id = str(uuid.uuid4())
    org_sql = (
        "INSERT INTO organizations "
        "(id, name, slug, is_active, is_staging, status, settings, created_at, updated_at) "
        f"VALUES ('{org_id}', 'Default', 'default', TRUE, TRUE, 'active', '{{}}', NOW(), NOW()) "
        "ON CONFLICT (slug) DO UPDATE SET is_staging = TRUE "
        "RETURNING id"
    )
    rc, org_out = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            plan.pg_svc,
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-t",
            "-A",
            "-c",
            org_sql,
        ],
        timeout=10,
    )
    if rc != 0 or not org_out.strip():
        raise RuntimeError(
            f"Default org INSERT failed (rc={rc}): {org_out}. "
            "The organizations schema may differ from what console expects; "
            "upgrade Studio."
        )
    org_id = org_out.strip().splitlines()[0]

    # 3. Insert admin user
    admin_id = str(uuid.uuid4())
    sql = (
        "INSERT INTO users "
        "(id, username, email, hashed_password, role, "
        "is_active, is_public, first_name, last_name, "
        "organization_id, created_at, updated_at) "
        "VALUES ("
        f"'{admin_id}', 'admin', '{email}', "
        f"$hash${hashed_pw}$hash$, 'admin', true, false, "
        f"'Default', 'Administrator', '{org_id}', NOW(), NOW()"
        ") ON CONFLICT (username) DO NOTHING"
    )
    rc, out = run_quiet(
        base
        + [
            "exec",
            *plan.exec_flags,
            plan.pg_svc,
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-c",
            sql,
        ],
        timeout=10,
    )
    if rc != 0:
        raise RuntimeError(f"admin user INSERT failed (rc={rc}): {out}")


def cmd_restart(context: str, env_file: Path, service: str | None) -> None:
    """Restart a service or all."""
    if context == "host":
        _validate_env(env_file)
        if service:
            info(f"Restarting {service}...")
            run(compose_cmd(env_file) + ["restart", service], timeout=60)
            ok(f"{service} restarted")
        else:
            # Full restart = full apply: use 'up -d --remove-orphans' (not bare
            # 'restart') so deselected profiles are cleaned up and replica/scale
            # counts are honored. Scale flags must be reapplied or workers/API
            # collapse to 1 replica.
            info("Restarting all services...")
            env_data = read_env(env_file)
            run(
                _apply_scale_flags(
                    compose_cmd(env_file) + ["up", "-d", "--remove-orphans"],
                    env_data,
                ),
                timeout=120,
            )
            ok("All services restarted")
    else:
        if service:
            info(f"Restarting {service}...")
            run(["supervisorctl", "restart", service], timeout=30)
            ok(f"{service} restarted")
        else:
            info("Restarting all services...")
            run(["supervisorctl", "restart", "all"], timeout=30)
            ok("All services restarted")


def _app_db_url(env_data: dict) -> str:
    """The restricted-role URL, from .env or process env (process env wins)."""
    return os.environ.get("SHS_DATABASE_APP_URL", "") or env_data.get(
        "SHS_DATABASE_APP_URL", ""
    )


def _print_db_role_posture(env_data: dict, api_up: bool) -> None:
    """One-line RLS posture. A healthy API with the app URL set proves
    restricted mode, boot is fail-closed on an RLS-inert role."""
    if _app_db_url(env_data):
        if api_up:
            print(
                f"  {'DB role':24s} {_green('restricted')}  "
                f"{_dim('RLS enforced (shs_app)')}"
            )
        else:
            print(
                f"  {'DB role':24s} {_yellow('restricted (configured)')}  "
                f"{_dim('API down, an RlsInertError in its logs means SHS_DATABASE_APP_URL is misconfigured')}"
            )
    else:
        print(
            f"  {'DB role':24s} {_yellow('privileged')}  "
            f"{_dim('RLS inert, app-layer checks only, enable via the DB role menu')}"
        )


def cmd_health(context: str, env_file: Path) -> None:
    """Check health of API and workers."""
    env_data = read_env(env_file)

    # Container mode: no nginx, API binds 8000 directly. Host mode: nginx fronts API.
    if context == "host":
        _validate_env(env_file)  # friendly fatal beats a misleading all-DOWN report
        nginx_port = env_data.get("SHS_NGINX_PORT", "80")
        api_base = f"http://localhost:{nginx_port}"
        print(f"\n{_bold('Health:')}")
        rc, _ = run_quiet(["curl", "-sf", f"{api_base}/health"])
        print(f"  {'API (via nginx)':24s} {_green('UP') if rc == 0 else _red('DOWN')}")
        rc_ng, _ = run_quiet(
            ["curl", "-sf", f"http://localhost:{nginx_port}/nginx-health"]
        )
        print(f"  {'nginx':24s} {_green('UP') if rc_ng == 0 else _red('DOWN')}")
        _print_db_role_posture(env_data, api_up=rc == 0)

        # If the API is down, distinguish "guardrail tripped" from "unknown" so
        # the operator isn't told to "just check logs" when the cause is known.
        if rc != 0:
            target_tag = env_data.get("SHS_STUDIO_VERSION", "")
            target_major = mv.parse_target_major(target_tag)
            mv_result, mv_info = mv.classify_db(env_file, target_major, context)
            if mv_result in ("prior_major", "unknown_future"):
                mv.render_block(mv_result, mv_info, "run")
            elif mv.scrape_guardrail_failure(env_file, context):
                warn(
                    "API container logs show a major-version guardrail FATAL. "
                    "The database schema does not match the running image. "
                    "Check SHS_STUDIO_VERSION in ~/.studio/.env."
                )
    else:
        api_base = "http://localhost:8000"
        print(f"\n{_bold('Health:')}")
        rc, _ = run_quiet(["curl", "-sf", f"{api_base}/health"])
        print(f"  {'API':24s} {_green('UP') if rc == 0 else _red('DOWN')}")
        _print_db_role_posture(env_data, api_up=rc == 0)

    # Container/supervisor status
    if context == "host":
        rc2, out = run_quiet(
            compose_cmd(env_file) + ["ps", "--format", "json"],
            timeout=15,
        )
        if rc2 == 0 and out:
            try:
                # docker compose ps --format json can return one JSON per line
                for line in out.strip().splitlines():
                    svc = json.loads(line)
                    name = svc.get("Name", svc.get("Service", "?"))
                    state = svc.get("State", "unknown")
                    color = _green if state == "running" else _red
                    print(f"  {name:24s} {color(state)}")
            except json.JSONDecodeError:
                pass
    else:
        rc2, out = run_quiet(["supervisorctl", "status"], timeout=10)
        if rc2 == 0 or out:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    name, state = parts[0], parts[1]
                    color = _green if state == "RUNNING" else _red
                    print(f"  {name:30s} {color(state)}")

    # Worker health via API
    rc3, wout = run_quiet(
        ["curl", "-sf", f"{api_base}/api/v1/infrastructure/health/workers"],
    )
    if rc3 == 0 and wout:
        try:
            data = json.loads(wout)
            for w in data.get("workers", []):
                wtype = w.get("type", "?")
                healthy = w.get("healthy", False)
                pid = w.get("pid", "?")
                color = _green if healthy else _red
                status = "UP" if healthy else "DOWN"
                print(f"  {wtype:20s} {color(status)}  (pid {pid})")
        except json.JSONDecodeError:
            pass
    print()


def cmd_show_config(context: str, env_file: Path) -> None:
    """Show .env grouped by section, mask secrets."""
    if not env_file.exists():
        fatal(f"No .env found at {env_file}. Run studio-console to create one.")

    data = read_env(env_file)
    shown_keys: set[str] = set()

    for section, keys in ENV_SECTIONS.items():
        section_data = {k: data[k] for k in keys if k in data}
        if not section_data:
            continue
        print(f"\n{_bold(section)}")
        for k, v in section_data.items():
            print(f"  {k:40s} {mask_value(k, v)}")
            shown_keys.add(k)

    # Show any keys not in known sections
    extra = {k: v for k, v in data.items() if k not in shown_keys}
    if extra:
        print(f"\n{_bold('Other')}")
        for k, v in extra.items():
            print(f"  {k:40s} {mask_value(k, v)}")


def cmd_config_set(env_file: Path, key: str, value: str) -> None:
    """Set a config value in .env."""
    if not env_file.exists():
        fatal(f"No .env found at {env_file}. Run studio-console first.")
    set_env_value(env_file, key, value)
    ok(f"Set {key}={mask_value(key, value)}")


def cmd_config_unset(env_file: Path, key: str) -> None:
    """Remove a config value from .env."""
    if not env_file.exists():
        fatal(f"No .env found at {env_file}. Run studio-console first.")
    if key not in read_env(env_file):
        warn(f"{key} not set; nothing to remove.")
        return
    unset_env_values(env_file, [key])
    ok(f"Removed {key}")


def cmd_logs(
    context: str,
    env_file: Path,
    services: str | list[str] | None,
    follow: bool = True,
) -> None:
    """View logs. follow=True streams live, follow=False shows recent.

    *services* may be None (all), a single service name string, or a list of
    service names (e.g. ["api-1", "api-2"] for the "All API" group).
    """
    if follow:
        print(_dim("  Press Ctrl-C to stop streaming\n"))
    if context == "host":
        _validate_env(env_file)
        cmd = compose_cmd(env_file) + ["logs", "--tail=200"]
        if follow:
            cmd.append("-f")
        if isinstance(services, list):
            cmd.extend(services)
        elif services:
            cmd.append(services)
        try:
            run(cmd, timeout=None, check=False)
        except KeyboardInterrupt:
            pass
        print()
    else:
        # supervisorctl only supports a single service name for tail -f
        target: str | None = None
        if isinstance(services, list):
            target = services[0] if services else None
        else:
            target = services
        try:
            tail_cmd = ["supervisorctl", "tail"]
            if follow:
                tail_cmd.append("-f")
            tail_cmd.append(target if target else "all")
            run(tail_cmd, timeout=None, check=False)
        except KeyboardInterrupt:
            pass
        print()


def cmd_workers(context: str, env_file: Path) -> None:
    """List workers - configured profiles and running state."""
    env_data = read_env(env_file)

    # Show configured profiles (source of truth)
    profiles = env_data.get("COMPOSE_PROFILES", "")
    if profiles:
        print(f"\n{_bold('Configured (COMPOSE_PROFILES):')}")
        for p in profiles.split(","):
            print(f"  {p.strip()}")
    else:
        print(f"\n{_bold('Configured (COMPOSE_PROFILES):')}")
        print(f"  {_dim('none')}")

    # Check running worker containers
    if context == "host":
        running = _get_running_services(context, env_file)
        worker_containers = [
            s for s in running if "worker" in s.lower() or s.startswith("w-")
        ]

        if worker_containers:
            print(f"\n{_bold('Running workers:')}")
            for w in worker_containers:
                print(f"  {w:30s} {_green('running')}")
        else:
            print(f"\n{_bold('Running workers:')}")
            print(f"  {_dim('none')}")

        # Also check API's worker registry
        workers_base = f"http://localhost:{env_data.get('SHS_NGINX_PORT', '80')}"
        rc, wout = run_quiet(
            [
                "curl",
                "-sf",
                f"{workers_base}/api/v1/infrastructure/health/workers",
            ],
        )
        if rc == 0 and wout:
            try:
                data = json.loads(wout)
                workers = data.get("workers", [])
                if workers:
                    print(f"\n{_bold('Registered with API:')}")
                    for w in workers:
                        wtype = w.get("type", "?")
                        healthy = w.get("healthy", False)
                        color = _green if healthy else _red
                        status = "UP" if healthy else "DOWN"
                        print(f"  {wtype:20s} {color(status)}")
            except json.JSONDecodeError:
                pass
    else:
        # Container/runpod - check supervisorctl
        rc, out = run_quiet(["supervisorctl", "status"], timeout=10)
        if out:
            worker_lines = [l for l in out.splitlines() if "worker" in l.lower()]
            if worker_lines:
                print(f"\n{_bold('Running workers:')}")
                for line in worker_lines:
                    parts = line.split()
                    if len(parts) >= 2:
                        name, state = parts[0], parts[1]
                        color = _green if state == "RUNNING" else _red
                        print(f"  {name:30s} {color(state)}")
            else:
                print(f"\n{_bold('Running workers:')}")
                print(f"  {_dim('none')}")

    # Only show scaling config if workers are configured
    components = env_data.get("CONSOLE_COMPONENTS", "")
    has_workers = "worker" in components.lower()
    if has_workers or context != "host":
        general = env_data.get("SHS_GENERAL_WORKERS", "1")
        transfer = env_data.get("SHS_TRANSFER_WORKERS", "1")
        print(f"\n{_bold('Scaling (supervisord):')}")
        print(
            f"  {_dim(f'SHS_GENERAL_WORKERS={general}  SHS_TRANSFER_WORKERS={transfer}')}"
        )
    print()


def cmd_reset_password(context: str, env_file: Path) -> None:
    """Reset super admin password. Prompts locally, passes to container."""
    new_password = _prompt_password("New super admin password")

    if context == "host":
        env_data = read_env(env_file)
        health_url = f"http://localhost:{env_data.get('SHS_NGINX_PORT', '80')}/health"
        rc, _ = run_quiet(["curl", "-sf", health_url])
        if rc != 0:
            error("API is not running. Start services first.")
            return
        api_svc = _api_service_name(env_data)
        info("Resetting admin password...")
        try:
            # SHS_ names are the app script's env contract (scripts/reset_admin_password.py
            # reads SHS_ADMIN_PASSWORD / SHS_FORCE_PRODUCTION), keep them, do NOT rename
            # to CONSOLE_. Transient injection only; never written to .env.
            run(
                compose_cmd(env_file)
                + [
                    "exec",
                    "-e",
                    "SHS_FORCE_PRODUCTION=true",
                    "-e",
                    f"SHS_ADMIN_PASSWORD={new_password}",
                    api_svc,
                    "python",
                    "scripts/reset_admin_password.py",
                ],
                timeout=60,
            )
        except subprocess.CalledProcessError:
            error("Failed to reset password. Check that the API container is healthy.")
    else:
        info("Resetting admin password...")
        # SHS_ names are the app script's env contract, keep, do NOT rename to CONSOLE_.
        os.environ["SHS_ADMIN_PASSWORD"] = new_password
        os.environ["SHS_FORCE_PRODUCTION"] = "true"
        try:
            run(
                ["python3", "/app/api/scripts/reset_admin_password.py"],
                timeout=60,
            )
        except subprocess.CalledProcessError:
            error("Failed to reset password.")


# Backup format is pinned: pg_dump plain-text + --inserts, with a leading
# "-- studio-console" comment block recording image tag and digest. Both
# _read_revision_from_dump and _parse_dump_header depend on this format.
# Do not switch to -Fc / -Fd or remove --inserts without updating both.
BACKUP_HEADER_VERSION = 1
PG_DUMP_FLAGS = ["--inserts"]

_DUMP_HEADER_RE = re.compile(r"^-- studio-console:([a-z_]+)=(.*)$")
_ALEMBIC_INSERT_RE = re.compile(
    r"^INSERT INTO (?:public\.)?alembic_version\s*(?:\([^)]*\)\s*)?VALUES\s*\('([^']+)'\)",
    re.MULTILINE,
)


def _api_image_digest() -> str | None:
    """Return the image digest of a running studio-api container, or None."""
    rc, out = run_quiet(
        ["docker", "ps", "--filter", "name=studio-api", "--format", "{{.ID}}"],
        timeout=5,
    )
    if rc != 0 or not out.strip():
        return None
    cid = out.strip().splitlines()[0].strip()
    rc, out = run_quiet(["docker", "inspect", "--format", "{{.Image}}", cid], timeout=5)
    if rc != 0:
        return None
    return out.strip() or None


def _encryption_key_fingerprint(env_data: dict) -> str:
    """sha256 of SHS_CREDENTIAL_ENCRYPTION_KEY, or "" if unset. Stored in the dump header so restore can verify the key without storing it."""
    import hashlib

    key = (env_data.get("SHS_CREDENTIAL_ENCRYPTION_KEY") or "").strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _build_dump_header(env_data: dict, timestamp_iso: str) -> str:
    """Build the leading SQL comment block prepended to every dump."""
    fields = {
        "header_version": str(BACKUP_HEADER_VERSION),
        "created_at": timestamp_iso,
        "studio_image_tag": env_data.get("SHS_STUDIO_VERSION") or "",
        "studio_image_digest": _api_image_digest() or "",
        "encryption_key_fp": _encryption_key_fingerprint(env_data),
    }
    lines = ["-- studio-console backup"]
    lines += [f"-- studio-console:{k}={v}" for k, v in fields.items()]
    lines.append("--")
    return "\n".join(lines) + "\n"


def _parse_dump_header(db_file: str) -> dict:
    """Read studio-console metadata from the dump's leading comment block.

    Returns {} for legacy dumps with no header. Stops scanning at the first
    non-comment line so we never read past the preamble.
    """
    meta: dict = {}
    try:
        with open(db_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("--"):
                    break
                m = _DUMP_HEADER_RE.match(line.rstrip())
                if m:
                    meta[m.group(1)] = m.group(2)
    except OSError:
        pass
    return meta


def _read_revision_from_dump(db_file: str) -> str | None:
    """Extract the alembic revision from a pg_dump --inserts plain-text dump."""
    try:
        text = Path(db_file).read_text(errors="replace")
    except OSError:
        return None
    m = _ALEMBIC_INSERT_RE.search(text)
    return m.group(1) if m else None


def _read_current_db_revision(context: str, env_file: Path) -> str | None:
    """Query the running DB's alembic_version. Returns None if unreachable or empty."""
    env_data = read_env(env_file)
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    sql = "SELECT version_num FROM alembic_version LIMIT 1;"
    if context == "host":
        cmd = compose_cmd(env_file) + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-tAc",
            sql,
        ]
    else:
        cmd = ["psql", "-U", pg_user, "-d", "selfhost_studio", "-tAc", sql]
    rc, out = run_quiet(cmd, timeout=10)
    if rc != 0:
        return None
    rev = out.strip()
    return rev or None


def _format_local_time(iso_string: str) -> tuple[str, str]:
    """Return (absolute, relative) labels for a header timestamp.

    Header timestamps are ISO 8601 with offset (e.g. 2026-05-07T08:56:17-07:00).
    We re-render in the operator's local zone so 11pm-incident reading is unambiguous.
    """
    import datetime

    try:
        dt = datetime.datetime.fromisoformat(iso_string)
    except ValueError:
        return iso_string, ""
    local = dt.astimezone()
    abs_label = local.strftime("%b %-d, %Y %-I:%M %p %Z").strip()
    delta = datetime.datetime.now(local.tzinfo) - local
    secs = int(delta.total_seconds())
    if secs < 60:
        rel = "just now"
    elif secs < 3600:
        rel = f"{secs // 60} min ago"
    elif secs < 86400:
        rel = f"{secs // 3600} hr ago"
    elif secs < 86400 * 30:
        rel = f"{secs // 86400} day{'s' if secs // 86400 != 1 else ''} ago"
    else:
        rel = local.strftime("%b %Y")
    return abs_label, rel


def _verify_post_restore(context: str, env_file: Path, db_file: str) -> None:
    """After restore, confirm the live DB's alembic_version matches the dump's.

    Mismatch means the apply succeeded but landed on the wrong schema, surface
    it loudly so the operator doesn't trust a silent success.
    """
    expected = _read_revision_from_dump(db_file)
    if not expected:
        return  # Nothing to verify against (legacy backup with no parseable rev).
    actual = _read_current_db_revision(context, env_file)
    if actual is None:
        warn("Could not verify post-restore schema (alembic_version unreadable).")
        return
    if actual != expected:
        error(
            f"Post-restore verification FAILED: expected schema {expected}, "
            f"got {actual}. The restore may be incomplete or corrupted."
        )
        error("Inspect the database before starting the API.")
        return
    ok(f"Schema verified: {actual}")


def _offer_key_recovery(env_file: Path, backup_key_fp: str) -> bool:
    """Paste/verify the backup's key against its fingerprint; persist to .env on match."""
    import hashlib

    print()
    info(_cyan("If you have the key this backup was taken under, paste it now to"))
    info(_cyan("verify and use it. Leave blank to cancel."))
    for _ in range(3):
        pasted = _prompt("Encryption key for this backup").strip()
        if not pasted:
            return False
        if hashlib.sha256(pasted.encode("utf-8")).hexdigest() == backup_key_fp:
            data = read_env(env_file)
            data["SHS_CREDENTIAL_ENCRYPTION_KEY"] = pasted
            write_env(env_file, data)
            ok("Encryption key verified and saved to .env")
            return True
        warn(_yellow("That key does not match this backup. Try again or leave blank."))
    warn(_yellow("No matching key entered."))
    return False


def _restore_preflight(context: str, env_file: Path, db_file: str) -> bool:
    """Show pre-flight summary, run schema check, prompt appropriately.

    Returns True if the operator confirms and the restore should proceed.
    Tiers:
      - prior_major / unknown_future → hard block (no override)
      - encryption key fingerprint differs → paste matching key or abort
      - schemas match → single y/N
      - schemas differ (both known) → require typing RESTORE
      - no header / unverifiable → require typing RESTORE
    """
    backup_dir = os.path.dirname(db_file)
    backup_name = os.path.basename(backup_dir)
    header = _parse_dump_header(db_file)
    backup_rev = _read_revision_from_dump(db_file)
    current_rev = _read_current_db_revision(context, env_file)

    # Major-version boundary check: if we restore this backup, the DB ends up at
    # backup_rev. Is backup_rev compatible with the running image's major?
    env_data = read_env(env_file)
    target_tag = env_data.get("SHS_STUDIO_VERSION", "")
    target_major = mv.parse_target_major(target_tag)
    mv_result, mv_info = mv.classify_revision(backup_rev, target_major)
    if mv_result in ("prior_major", "unknown_future"):
        mv.render_block(mv_result, mv_info, "restore")
        info("Aborted (incompatible backup).")
        return False

    # Encryption-key check: the dump's encrypted columns are only readable with
    # the key they were written under. We never store that key in the backup, so
    # we compare fingerprints, live key vs. the fp recorded in the header.
    backup_key_fp = header.get("encryption_key_fp", "")
    live_key_fp = _encryption_key_fingerprint(env_data)
    key_mismatch = bool(backup_key_fp and live_key_fp and backup_key_fp != live_key_fp)

    has_header = bool(header)
    created_iso = header.get("created_at", "")
    abs_time, rel_time = (
        _format_local_time(created_iso) if created_iso else ("unknown", "")
    )
    studio_tag = header.get("studio_image_tag") or "unknown"

    if not has_header and not backup_rev:
        tier = "unverifiable"
    elif backup_rev and current_rev and backup_rev == current_rev:
        tier = "match"
    elif backup_rev and current_rev and backup_rev != current_rev:
        tier = "mismatch"
    else:
        tier = "unverifiable"

    if tier == "match":
        schema_line = _green("✓ matches your current database")
    elif tier == "mismatch":
        schema_line = _yellow("⚠ DIFFERENT from your current database")
    else:
        schema_line = _yellow("⚠ unverifiable (legacy backup, no header)")

    print()
    print(_bold("About to restore database"))
    print("─" * 40)
    print(f"  Backup:          {backup_name}")
    if has_header:
        time_label = f"{abs_time}" + (f" ({rel_time})" if rel_time else "")
        print(f"  Created:         {time_label}")
        print(f"  Studio version:  {studio_tag}")
    if backup_rev:
        print(f"  Backup schema:   {_dim(backup_rev)}")
    if current_rev:
        print(f"  Current schema:  {_dim(current_rev)}")
    print(f"  Schema check:    {schema_line}")
    if key_mismatch:
        print(f"  Encryption key:  {_yellow('⚠ does NOT match this backup')}")
    print()
    print("This will:")
    print(f"  • Take a pre-restore snapshot to {_backups_root(context, env_file)}")
    print("  • Replace the database with the backup")
    print("  • Keep your current .env (the encryption key is not changed)")
    print()

    if key_mismatch:
        warn(
            _yellow("Your live SHS_CREDENTIAL_ENCRYPTION_KEY differs from the one this backup was")
        )
        warn(
            _yellow(
                "written under. Restoring will leave the encrypted data UNREADABLE."
            )
        )
        if _offer_key_recovery(env_file, backup_key_fp):
            return True  # key now matches; fall through to restore
        info(_cyan("Restore needs the matching key. The data is unreadable without it."))
        info("Aborted (no valid encryption key).")
        return False

    if tier == "match":
        return _interactive_yn("Restore this database?", default=False)

    if tier == "mismatch":
        warn(
            _yellow(
                "This backup was taken with a different schema than what's installed."
            )
        )
        warn(_yellow("Restoring it may corrupt data, crash the API, or both."))
        info(
            _cyan(
                "If unsure, cancel and check which Studio version produced this backup."
            )
        )
    else:  # unverifiable
        warn(
            _yellow(
                "This backup has no version metadata, so schema compatibility "
                "can't be verified."
            )
        )
        info(
            _cyan(
                "If unsure, cancel and check which Studio version produced this backup."
            )
        )

    print()
    typed = _prompt("Type RESTORE to proceed")
    if typed != "RESTORE":
        info("Aborted.")
        return False
    return True


def cmd_backup(
    context: str,
    env_file: Path,
    what: str = "all",
    name_prefix: str = "studio",
) -> str:
    """Backup database and/or workspace files. Returns the backup directory path."""
    import datetime

    env_data = read_env(env_file)
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    timestamp_iso = now.astimezone().isoformat(timespec="seconds")
    backup_name = f"{name_prefix}-{timestamp}"

    backup_dir = str(backup_root(context, env_file) / backup_name)
    os.makedirs(backup_dir, exist_ok=True)

    if what in ("all", "db"):
        info("Backing up database...")
        db_file = os.path.join(backup_dir, "database.sql")
        pg_user = env_data.get("POSTGRES_USER", "postgres")
        dump_args = ["pg_dump", *PG_DUMP_FLAGS, "-U", pg_user, "selfhost_studio"]
        if context == "host":
            cmd = compose_cmd(env_file) + ["exec", "-T", "postgres", *dump_args]
        else:
            cmd = dump_args
        result = run(cmd, capture=True, timeout=120)
        header = _build_dump_header(env_data, timestamp_iso)
        Path(db_file).write_text(header + result.stdout)
        ok(f"Database: {db_file}")

    if what in ("all", "orgs"):
        orgs_dir = str(storage_root(context, env_file) / "orgs")
        if os.path.isdir(orgs_dir):
            info("Archiving org files...")
            archive = os.path.join(backup_dir, "orgs.tar.gz")
            run(
                ["tar", "czf", archive, "-C", os.path.dirname(orgs_dir), "orgs"],
                timeout=300,
            )
            ok(f"Orgs: {archive}")
        else:
            warn("No orgs directory found")

    ok(f"Backup complete: {backup_dir}")

    # The encryption key is deliberately NOT stored in the backup, co-locating
    # it with the ciphertext would let anyone holding the backup decrypt it.
    # Skip the notice for internal pre-restore snapshots (not operator-facing).
    if what in ("all", "db") and name_prefix != "pre-restore":
        _print_encryption_key_notice(env_data)

    return backup_dir


def _print_encryption_key_notice(env_data: dict) -> None:
    """Remind the operator to store SHS_CREDENTIAL_ENCRYPTION_KEY separately from the backup."""
    key = (env_data.get("SHS_CREDENTIAL_ENCRYPTION_KEY") or "").strip()
    print()
    print(_bold("⚠ Record your encryption key separately"))
    print("─" * 40)
    if key:
        print("This backup does NOT contain your encryption key, by design, so a")
        print("stolen backup can't be decrypted. The dump is unrecoverable without it.")
        print()
        print("Copy SHS_CREDENTIAL_ENCRYPTION_KEY from your .env into a password manager or")
        print("secrets vault, kept apart from these backup files:")
        print()
        print(f"    SHS_CREDENTIAL_ENCRYPTION_KEY={key}")
    else:
        print(_yellow("No SHS_CREDENTIAL_ENCRYPTION_KEY is set in .env. Nothing to record."))
    print()


def _backups_root(context: str, env_file: Path) -> str:
    """Return the directory where backups live for the current context."""
    return str(backup_root(context, env_file))


def _check_snapshot_space(context: str, env_file: Path) -> None:
    """Abort if the backup volume can't hold a pre-restore snapshot.

    Heuristic: require max(1.5 * pg_database_size, db_size + 100 MB).
    pg_dump --inserts is verbose for small DBs (often >1× on-disk size) and
    leaner than the live DB for large DBs (no indexes/bloat). The 1.5×
    factor is a safe upper bound for typical Studio installs.
    """
    import shutil

    env_data = read_env(env_file)
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    sql = "SELECT pg_database_size('selfhost_studio')"
    if context == "host":
        cmd = compose_cmd(env_file) + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            pg_user,
            "-d",
            "selfhost_studio",
            "-tAc",
            sql,
        ]
    else:
        cmd = ["psql", "-U", pg_user, "-d", "selfhost_studio", "-tAc", sql]
    rc, out = run_quiet(cmd, timeout=10)
    if rc != 0 or not out.strip().isdigit():
        return  # Can't size the DB; skip the precheck rather than block.
    db_bytes = int(out.strip())
    required = max(int(db_bytes * 1.5), db_bytes + 100 * 1024 * 1024)

    backups_root = _backups_root(context, env_file)
    os.makedirs(backups_root, exist_ok=True)
    free = shutil.disk_usage(backups_root).free
    if free < required:
        fatal(
            f"Pre-restore snapshot needs ~{required // (1024 * 1024)} MB, "
            f"only {free // (1024 * 1024)} MB free on {backups_root}.\n"
            f"Free up space, or set CONSOLE_SKIP_PRE_RESTORE_SNAPSHOT=1 to skip "
            f"(not recommended)."
        )


def _take_pre_restore_snapshot(context: str, env_file: Path) -> str | None:
    """Take a forced backup of current DB before restore. Returns dir path or None if skipped."""
    if os.environ.get("CONSOLE_SKIP_PRE_RESTORE_SNAPSHOT", "").strip() in (
        "1",
        "true",
        "yes",
    ):
        warn("Pre-restore snapshot skipped (CONSOLE_SKIP_PRE_RESTORE_SNAPSHOT set)")
        return None
    _check_snapshot_space(context, env_file)
    info("Taking pre-restore snapshot of current database...")
    return cmd_backup(context, env_file, what="db", name_prefix="pre-restore")


def _pick_db_file(context: str, env_file: Path) -> str | None:
    """Prompt for a directory then let the user pick a .sql file from it."""
    default_dir = str(backup_root(context, env_file))
    raw = _prompt(f"Backup directory [{default_dir}]", "").strip()
    search_dir = os.path.expanduser(raw) if raw else default_dir

    if not os.path.isdir(search_dir):
        error(f"Directory not found: {search_dir}")
        return None

    import glob

    sql_files = sorted(
        glob.glob(os.path.join(search_dir, "**", "*.sql"), recursive=True)
    )
    if not sql_files:
        error(f"No .sql files found in {search_dir}")
        return None

    labels = []
    for f in sql_files:
        rel = os.path.relpath(f, search_dir)
        meta = _parse_dump_header(f)
        rev = _read_revision_from_dump(f)
        if meta or rev:
            rev_label = (rev or "no-rev")[:12]
            tag = meta.get("studio_image_tag") or "no-tag"
            labels.append(f"{rel}  {_dim(f'rev={rev_label} tag={tag}')}")
        else:
            labels.append(f"{rel}  {_dim('(legacy, no header)')}")
    idx = _interactive_single("Select database file to restore", labels, default=0)
    return sql_files[idx]


def cmd_restore_db(
    context: str, env_file: Path, db_file: str, confirm: bool = True
) -> bool:
    """Restore a single .sql file into the database. Returns True on success."""
    import time

    env_data = read_env(env_file)
    pg_user = env_data.get("POSTGRES_USER", "postgres")
    db_name = "selfhost_studio"

    if confirm:
        if not _restore_preflight(context, env_file, db_file):
            return False

    _take_pre_restore_snapshot(context, env_file)

    if context == "host":
        info("Waiting for postgres...")
        base = compose_cmd(env_file) + ["exec", "-T", "postgres"]
        for _ in range(15):
            rc, _ = run_quiet(base + ["pg_isready", "-U", pg_user])
            if rc == 0:
                break
            time.sleep(2)
        else:
            error("Postgres not ready after 30s")
            return False

    try:
        if context == "host":
            # Stop API/UI before dropping the schema, a live API reconnects
            # to postgres mid-restore (pg_terminate_backend only kills current
            # connections) and reads/writes a half-restored schema. The
            # pre-restore snapshot above is the recovery net if this fails.
            info("Stopping API/UI for exclusive DB access...")
            _stop_api_ui_containers()
            base = compose_cmd(env_file) + ["exec", "-T", "postgres"]
            run_quiet(
                base
                + [
                    "psql",
                    "-q",
                    "-U",
                    pg_user,
                    "-c",
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();",
                ],
                timeout=30,
            )
            run_quiet(
                base
                + [
                    "psql",
                    "-q",
                    "-U",
                    pg_user,
                    "-c",
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
                    db_name,
                ],
                timeout=30,
            )
            info("Applying dump...")
            with open(db_file) as f:
                subprocess.run(
                    compose_cmd(env_file)
                    + ["exec", "-T", "postgres", "psql", "-q", "-U", pg_user, db_name],
                    stdin=f,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=True,
                )
        else:
            run_quiet(
                [
                    "psql",
                    "-q",
                    "-U",
                    pg_user,
                    "-c",
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();",
                ],
                timeout=30,
            )
            run_quiet(
                [
                    "psql",
                    "-q",
                    "-U",
                    pg_user,
                    "-c",
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
                    db_name,
                ],
                timeout=30,
            )
            info("Applying dump...")
            with open(db_file) as f:
                subprocess.run(
                    ["psql", "-q", "-U", pg_user, db_name],
                    stdin=f,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=True,
                )
    except subprocess.CalledProcessError as e:
        error(f"Database restore failed: {e}")
        if e.stderr:
            error(
                e.stderr.decode("utf-8", errors="replace")
                if isinstance(e.stderr, bytes)
                else e.stderr
            )
        error("Is postgres running? Try: studio-console start")
        return False
    except Exception as e:
        error(f"Database restore failed: {e}")
        error("Is postgres running? Try: studio-console start")
        return False

    ok("Database restored")
    _verify_post_restore(context, env_file, db_file)
    # .env (and its encryption key) is intentionally left untouched, the key
    # is never stored in the backup. The preflight already gated key mismatch.

    # API/UI were stopped for exclusive DB access, bring them back up.
    if context == "host":
        info("Restarting API/UI...")
        env_data2 = read_env(env_file)
        run(
            _apply_scale_flags(
                compose_cmd(env_file) + ["up", "-d", "--remove-orphans"], env_data2
            ),
            timeout=120,
        )
        ok("API/UI restarted")
        if _app_db_url(env_data2):
            info(
                "Grants and RLS policies for the restricted role re-apply during "
                "boot; requests may fail with sanitized 500s until it finishes."
            )
    else:
        # Provisioning/grants/RLS re-apply in the container ENTRYPOINT, so the
        # restore flow ends with a container restart, supervisorctl restart
        # is not enough. Until then the restricted role authenticates but every
        # table access is permission-denied → sanitized 500s (fail-closed).
        warn("Restart this container from the host now to finish the restore.")
        print(f"    {_bold('docker restart <container>')}   {_dim('(or stop/start the pod on RunPod)')}")
        if _app_db_url(env_data):
            warn(
                "Until the restart, API requests fail with sanitized 500s, "
                "the restore dropped the restricted role's grants; boot re-applies them."
            )

    return True


def cmd_restore(
    context: str, env_file: Path, path: str | None, what: str = "all"
) -> None:
    """Restore database and/or org files from backup."""
    env_data = read_env(env_file)

    if path:
        restore_dir = path
    else:
        backup_base = str(backup_root(context, env_file))
        if not os.path.isdir(backup_base):
            fatal("No backups found")
        entries = sorted(os.listdir(backup_base), reverse=True)
        if not entries:
            fatal("No backups found")
        restore_dir = os.path.join(backup_base, entries[0])

    info(f"Restoring from: {restore_dir}")

    if what in ("all", "db"):
        db_file = os.path.join(restore_dir, "database.sql")
        if os.path.isfile(db_file):
            print()
            if not _restore_preflight(context, env_file, db_file):
                return

            _take_pre_restore_snapshot(context, env_file)

            pg_user = env_data.get("POSTGRES_USER", "postgres")
            db_name = "selfhost_studio"

            if context == "host":
                # Stop API/UI before dropping the schema, see cmd_restore_db.
                # A live API reconnects mid-restore and reads a half-restored
                # schema; the pre-restore snapshot above is the recovery net.
                info("Stopping API/UI for exclusive DB access...")
                _stop_api_ui_containers()
                base = compose_cmd(env_file) + ["exec", "-T", "postgres"]
                run_quiet(
                    base
                    + [
                        "psql",
                        "-q",
                        "-U",
                        pg_user,
                        "-c",
                        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();",
                    ],
                    timeout=30,
                )
                run_quiet(
                    base
                    + [
                        "psql",
                        "-q",
                        "-U",
                        pg_user,
                        "-c",
                        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
                        db_name,
                    ],
                    timeout=30,
                )
                info("Applying dump...")
                with open(db_file) as f:
                    subprocess.run(
                        compose_cmd(env_file)
                        + [
                            "exec",
                            "-T",
                            "postgres",
                            "psql",
                            "-q",
                            "-U",
                            pg_user,
                            db_name,
                        ],
                        stdin=f,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=300,
                        check=True,
                    )
            else:
                run_quiet(
                    [
                        "psql",
                        "-q",
                        "-U",
                        pg_user,
                        "-c",
                        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();",
                    ],
                    timeout=30,
                )
                run_quiet(
                    [
                        "psql",
                        "-q",
                        "-U",
                        pg_user,
                        "-c",
                        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
                        db_name,
                    ],
                    timeout=30,
                )
                info("Applying dump...")
                with open(db_file) as f:
                    subprocess.run(
                        ["psql", "-q", "-U", pg_user, db_name],
                        stdin=f,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=300,
                        check=True,
                    )
            ok("Database restored")
            _verify_post_restore(context, env_file, db_file)
            # .env (and its encryption key) is intentionally left untouched, the
            # key is never stored in the backup. Preflight gated key mismatch.
        else:
            warn(f"No database.sql in {restore_dir}")

    if what in ("all", "orgs"):
        orgs_archive = os.path.join(restore_dir, "orgs.tar.gz")
        if os.path.isfile(orgs_archive):
            # Archive root is `orgs/...` so extract into shared/ (the parent of orgs/).
            target = str(storage_root(context, env_file))
            info("Restoring organization files...")
            run(["tar", "xzf", orgs_archive, "-C", target], timeout=300)
            ok("Org files restored")
        else:
            warn(f"No orgs.tar.gz in {restore_dir}")

    # API/UI were stopped for exclusive DB access, bring them back up.
    if context == "host":
        print()
        info("Restarting API/UI...")
        run(
            _apply_scale_flags(
                compose_cmd(env_file) + ["up", "-d", "--remove-orphans"], env_data
            ),
            timeout=120,
        )
        ok("API/UI restarted")


def _fetch_available_versions(limit: int = 10) -> list[str]:
    """Fetch available version tags from GHCR."""
    import urllib.request
    import urllib.error

    # GHCR requires a token even for public repos
    token_url = "https://ghcr.io/token?scope=repository:selfhosthub/studio-api:pull"
    try:
        with urllib.request.urlopen(token_url, timeout=10) as resp:
            token = json.loads(resp.read().decode()).get("token", "")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []

    url = "https://ghcr.io/v2/selfhosthub/studio-api/tags/list"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []

    # Filter to semver tags (X.Y.Z), skip "latest" and digests
    versions: list[str] = []
    for name in data.get("tags", []):
        if re.match(r"^\d+\.\d+\.\d+$", name):
            versions.append(name)

    # Sort descending by semver
    versions.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return versions[:limit]


def cmd_upgrade(context: str, env_file: Path) -> None:
    """Upgrade or rollback Studio to a specific version."""
    env_data = read_env(env_file)
    current = env_data.get("SHS_STUDIO_VERSION", "unknown")

    if context != "host":
        warn("In-container upgrades are not supported.")
        print(f"\n  Current version: {_bold(current)}")
        print()
        if context == "runpod":
            print("  To upgrade:")
            print("  1. Update the pod template to use the new image tag")
            print("  2. Stop and restart the pod")
            print("  Your data is safe on the network volume.")
        else:
            from .env import detect_shape

            shape = detect_shape(env_file) or "core"
            image = "studio-full" if shape == "full" else "studio-core"
            print("  To upgrade:")
            print(
                f"  1. Pull the new image: docker pull ghcr.io/selfhosthub/{image}:<tag>"
            )
            print("  2. Stop the current container")
            print("  3. Start a new container with the updated image")
        return

    print(f"\n  Current version: {_bold(current)}")

    info("Checking available versions...")
    versions = _fetch_available_versions()

    if not versions:
        warn("Could not fetch versions from Docker Hub")
        target = _prompt("Enter version to install (e.g. 1.1.0)")
    else:
        print()
        for i, v in enumerate(versions, 1):
            marker = " ← current" if v == current else ""
            print(f"  {_bold(str(i))}. {v}{marker}")
        print(f"  {_bold(str(len(versions) + 1))}. Enter manually")

        while True:
            raw = _prompt("Select version", "1")
            try:
                pick = int(raw.strip())
                if 1 <= pick <= len(versions):
                    target = versions[pick - 1]
                    break
                elif pick == len(versions) + 1:
                    target = _prompt("Enter version (e.g. 1.1.0)")
                    break
            except ValueError:
                pass
            print(f"  {_red('✗')} Enter a number between 1 and {len(versions) + 1}")

    if target == current:
        info(f"Already on {current}, nothing to do")
        return

    if mv.check_and_block(
        env_file, mv.parse_target_major(target), "upgrade to", context, target
    ):
        return

    direction = "Upgrading" if target > current else "Rolling back"
    info(f"{direction}: {current} → {_bold(target)}")

    # Update version in .env
    set_env_value(env_file, "SHS_STUDIO_VERSION", target)
    ok(f"SHS_STUDIO_VERSION set to {target}")

    # Pull new images
    info("Pulling images...")
    run(compose_cmd(env_file) + ["pull"], timeout=300)
    ok("Images pulled")

    # Restart, reapply scale flags + --remove-orphans so replica counts survive
    # the upgrade and stale containers from the old version are cleaned up.
    info("Restarting services...")
    env_data = read_env(env_file)
    run(
        _apply_scale_flags(
            compose_cmd(env_file) + ["up", "-d", "--remove-orphans"], env_data
        ),
        timeout=120,
    )
    ok(f"Studio {direction.lower()} to {target}")


def cmd_links(context: str, env_file: Path) -> None:
    """Print service URLs."""
    env_data = read_env(env_file)
    _print_links(env_data, context)


def _print_links(env_data: dict[str, str], context: str) -> None:
    """Print service URLs from env data."""
    public_url = env_data.get("SHS_PUBLIC_BASE_URL", "")
    api_url = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "")
    nginx_port = env_data.get("SHS_NGINX_PORT", "80")
    has_public = public_url.startswith("https://")

    print(f"\n{_bold('Links:')}")
    print(f"  Studio:      http://localhost:{nginx_port}")
    print(f"  API:         http://localhost:{nginx_port}/api")
    print(f"  Health:      http://localhost:{nginx_port}/health")
    print(f"  Nginx:       http://localhost:{nginx_port}/nginx-health")

    if has_public:
        print()
        print(f"  Public:      {public_url}")
        if api_url.startswith("https://"):
            print(f"  Public API:  {api_url}")

    if context in ("container", "runpod"):
        print(f"  Supervisor:  http://localhost:9001")

    print()


def _detect_install_method() -> str:
    """Detect how studio-console was installed.

    Returns one of: "uv", "pip", "brew", "curl", "dev"
    """
    from pathlib import Path

    pkg_dir = Path(__file__).resolve().parent.parent

    # Marker written by install.sh
    marker = pkg_dir / ".install-method"
    if marker.exists():
        return marker.read_text().strip()

    # uv tool install, path contains uv/tools, no pip module available
    if "uv/tools" in str(pkg_dir).replace("\\", "/"):
        return "uv"

    # pip install puts the package inside site-packages
    if "site-packages" in str(pkg_dir):
        return "pip"

    # Homebrew installs under Cellar
    if "Cellar" in str(pkg_dir) or "homebrew" in str(pkg_dir).lower():
        return "brew"

    # Inside a git repo, dev mode
    if (pkg_dir / ".git").exists() or (pkg_dir.parent / ".git").exists():
        return "dev"

    return "curl"


def _baked_console_version(release_json: "Path | None" = None) -> str:
    """Version baked into the container image, from the build-time release metadata."""
    import json
    from pathlib import Path

    path = release_json or Path("/tmp/.console-release.json")
    try:
        return json.loads(path.read_text()).get("tag_name", "").lstrip("v")
    except (OSError, json.JSONDecodeError):
        return ""


def cmd_self_update(context: str) -> None:
    """Update studio-console, delegates to the correct mechanism for the install method."""
    import json
    import os
    import shutil
    import subprocess
    import tarfile
    import tempfile
    import urllib.error
    import urllib.request
    from pathlib import Path

    from . import __version__

    if context in ("container", "runpod"):
        info(f"Current version: {__version__}  (installed in the container)")
        if shutil.which("uv") is None:
            warn("uv not found in this container; update by pulling a new image tag.")
            return
        info("Updating via uv tool...")
        run(["uv", "tool", "install", "--force", "studio-console"], timeout=120)
        ok("Updated. Re-run studio-console to use the new version.")
        baked = _baked_console_version()
        if baked:
            warn(f"Recreating the container reverts to the baked console ({baked}).")
        else:
            warn("Recreating the container reverts to the baked console version.")
        return

    method = _detect_install_method()
    info(f"Current version: {__version__}  (installed via {method})")

    if method == "dev":
        warn("Running from source. Use git pull to update.")
        return

    if method == "brew":
        info("Updating via Homebrew...")
        run(["brew", "upgrade", "selfhosthub/studio/studio-console"], timeout=120)
        ok("Updated")
        return

    if method == "pip":
        info("Updating via pip...")
        run(
            [os.sys.executable, "-m", "pip", "install", "--upgrade", "studio-console"],
            timeout=120,
        )
        ok("Updated. Restart studio-console to use the new version.")
        return

    if method == "uv":
        info("Updating via uv tool...")
        run(["uv", "tool", "install", "--force", "studio-console"], timeout=120)
        ok("Updated. Restart studio-console to use the new version.")
        return

    # curl installs resolve the latest GitHub release and extract its tarball
    info("Checking for updates...")
    api_url = "https://api.github.com/repos/selfhosthub/studio-console/releases/latest"
    try:
        req = urllib.request.Request(
            api_url, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        fatal(f"Could not reach GitHub: {e}")

    latest_tag = data.get("tag_name", "")
    latest_version = latest_tag.lstrip("v")
    if not latest_version:
        fatal("Could not determine latest version from GitHub response")

    if latest_version == __version__:
        ok(f"Already up to date ({__version__})")
        return

    info(f"Updating {__version__} → {latest_version}")

    tarball_url = f"https://github.com/selfhosthub/studio-console/archive/refs/tags/{latest_tag}.tar.gz"
    install_dir = Path(__file__).resolve().parent.parent

    tmp = tempfile.mkdtemp()
    try:
        tarball_path = os.path.join(tmp, "release.tar.gz")
        info("Downloading...")
        urllib.request.urlretrieve(tarball_url, tarball_path)

        extract_dir = os.path.join(tmp, "extracted")
        os.makedirs(extract_dir)
        with tarfile.open(tarball_path, "r:gz") as tf:
            members = tf.getmembers()
            prefix = members[0].name.split("/")[0] + "/" if members else ""
            for member in members:
                if member.name.startswith(prefix):
                    member.name = member.name[len(prefix) :]
                    if member.name:
                        tf.extract(member, extract_dir)

        for item in os.listdir(extract_dir):
            src = os.path.join(extract_dir, item)
            dst = os.path.join(str(install_dir), item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        # Preserve the install method marker
        (install_dir / ".install-method").write_text("curl")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok(f"Updated to {latest_version}")
