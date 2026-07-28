# studio_console/cloudflare/cf_wizard.py
"""Cloudflare PAT-driven setup wizard.

Covers:
  1. API token collection + account discovery
  2. Tunnel create/reuse
  3. Published application routes (ingress rules) — nginx-LB aware
  4. DNS CNAME records
  5. Zero Trust Access application
  6. IP bypass policy
  7. Update IP rules (standalone, callable from submenu)
  8. Update domain (with stale DNS + Access app cleanup)
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from urllib.parse import urlparse

from ..env import detect_shape, read_env, run_quiet, set_env_value
from ..tui import (
    NavBack,
    _bold,
    _cyan,
    _dim,
    _green,
    _red,
    _yellow,
    error,
    fatal,
    info,
    ok,
    warn,
    _interactive_single,
    _interactive_yn,
    _prompt,
)
from .cf_api import CloudflareAPI, CloudflareError

# Set by cf_full_setup(non_interactive=True) — skips prompts that have a
# sensible default. Requested values still come from env / .env as usual.
_NON_INTERACTIVE = False


# .env key recording the origin the wizard last pushed to the tunnel.
INGRESS_ORIGIN_KEY = "CLOUDFLARE_INGRESS_ORIGIN"


def expected_ingress_origin(shape: str, nginx_port: str = "") -> str:
    """Ingress origin a tunnel must target for *shape*."""
    host = "localhost" if shape in ("core", "full") else "nginx"
    return f"http://{host}:{nginx_port.strip() or '80'}"


def ingress_shape_mismatch(shape: str, env: dict) -> str | None:
    """Refusal text when the recorded tunnel ingress origin does not fit *shape*.

    Returns None when there is no recorded origin, no tunnel token, or the
    origins match. Never re-syncs ingress: a boot on one machine would silently
    steal the tunnel from another.
    """
    recorded = (env.get(INGRESS_ORIGIN_KEY) or "").strip()
    token = (env.get("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
    if not recorded or not token:
        return None
    expected = expected_ingress_origin(shape, env.get("SHS_NGINX_PORT", ""))
    if recorded == expected:
        return None
    recorded_shape = "split" if urlparse(recorded).hostname == "nginx" else "core/full"
    return (
        f"Refusing to launch: the reused tunnel's ingress targets {recorded} "
        f"(written for {recorded_shape}), but {shape} needs {expected}. "
        f"Booting anyway would 502 on the public hostname. "
        f"Rerun the Cloudflare wizard for {shape}, or use the cf-reuse profile "
        f"that matches this tunnel."
    )


def _ingress_target(env_file: Path, hostname: str = "") -> str:
    """Tunnel ingress origin. Every shape targets the nginx front door, which
    path-routes api/ws vs ui. Split: nginx service. Core/full: localhost (nginx
    shares the container with cloudflared)."""
    nginx_port = read_env(env_file).get("SHS_NGINX_PORT", "80")
    if detect_shape(env_file) in ("core", "full"):
        return f"http://localhost:{nginx_port}"
    return f"http://nginx:{nginx_port}"


def _api_base_url(env_file: Path) -> str:
    """Public API base URL. Split: CONSOLE_PUBLIC_API_BASE_URL only. Core/full:
    same var, falling back to the derived SHS_PUBLIC_API_URL."""
    env_data = read_env(env_file)
    url = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").rstrip("/")
    if url.startswith("https://"):
        return url
    if detect_shape(env_file) in ("core", "full"):
        url = env_data.get("SHS_PUBLIC_API_URL", "").rstrip("/")
        if url.startswith("https://"):
            return url
    return ""


def _is_non_interactive() -> bool:
    return _NON_INTERACTIVE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_api(env_file: Path) -> CloudflareAPI | None:
    """Load a CloudflareAPI instance from .env. Returns None if token missing."""
    env_data = read_env(env_file)
    token = env_data.get("CLOUDFLARE_API_TOKEN", "")
    account_id = env_data.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token:
        if _is_non_interactive():
            return None
        info("No Cloudflare API token configured — let's set one up.")
        return _step_token(env_file)
    return CloudflareAPI(token, account_id)


def _detect_home_ip() -> str:
    """Best-effort home IP detection via ifconfig.me."""
    rc, out = run_quiet(["curl", "-sf", "--max-time", "5", "https://ifconfig.me"])
    if rc == 0 and out.strip():
        return out.strip()
    # Fallback
    rc, out = run_quiet(["curl", "-sf", "--max-time", "5", "https://api.ipify.org"])
    if rc == 0 and out.strip():
        return out.strip()
    return ""


def _normalise_cidr(raw: str) -> str:
    """Append /32 to a bare IPv4 address. Pass CIDRs through unchanged."""
    raw = raw.strip()
    if raw and "/" not in raw:
        return f"{raw}/32"
    return raw


def _warn_broad_cidr(cidr: str) -> None:
    """Warn if a CIDR prefix is broad enough to cover a large IP range."""
    if "/" not in cidr:
        return
    try:
        prefix = int(cidr.split("/")[1])
    except (ValueError, IndexError):
        return
    if prefix < 24:
        from ..tui import warn
        warn(f"{cidr} covers {2 ** (32 - prefix):,} IPs — double-check this is intentional")


def _parse_domain(public_url: str) -> tuple[str, str, str]:
    """Return (hostname, subdomain, root_domain) from a public URL.

    e.g. "https://studio.example.com" → ("studio.example.com", "studio", "example.com")
    """
    parsed = urlparse(public_url)
    hostname = parsed.hostname or ""
    parts = hostname.split(".", 1)
    if len(parts) == 2:
        return hostname, parts[0], parts[1]
    return hostname, "", hostname


def _resolve_zone(cf: CloudflareAPI, root_domain: str) -> str | None:
    """Find the Cloudflare zone ID matching root_domain. Returns None if not found.

    Distinguishes a permissions failure (403 — token lacks Zone:Read) from a
    genuine absence, so the operator isn't wrongly told their zone doesn't exist
    when the real fix is a token scope.
    """
    try:
        zones = cf.list_zones()
    except CloudflareError as e:
        if e.status_code == 403:
            warn(
                "Could not list zones (403 — token likely lacks Zone:Read on "
                "this zone). Add the scope or enter the zone ID manually."
            )
        else:
            warn(f"Could not list zones: {e}")
        return None
    for zone in zones:
        if zone.get("name") == root_domain:
            return zone.get("id", "")
    return None


def _upsert_ip_policy(
    cf: CloudflareAPI,
    app_id: str,
    policy_name: str,
    ip_ranges: list[str],
    existing_policy_id: str = "",
) -> None:
    """Create or update a named IP bypass policy on an Access app."""
    if existing_policy_id:
        info("Updating IP bypass policy...")
        try:
            cf.update_ip_bypass_policy(app_id, existing_policy_id, policy_name, ip_ranges)
            ok(f"Bypass policy updated: {', '.join(ip_ranges)}")
        except CloudflareError as e:
            error(f"Failed to update policy: {e}")
    else:
        info("Creating IP bypass policy...")
        try:
            cf.create_ip_bypass_policy(app_id, policy_name, ip_ranges)
            ok(f"Bypass policy created: {', '.join(ip_ranges)}")
        except CloudflareError as e:
            error(f"Failed to create policy: {e}")



# ---------------------------------------------------------------------------
# Step 1 — API token + account
# ---------------------------------------------------------------------------


def _print_token_instructions() -> None:
    """Print step-by-step Cloudflare API token creation instructions."""
    print()
    print(f"  {_bold('Before you start — grab your Account ID:')}")
    print(f"  Go to {_cyan('https://dash.cloudflare.com')} (Account home)")
    print(f"  - In the URL: dash.cloudflare.com/{_bold('<account-id>')}/...")
    print(f"  - Or: click the {_bold('⋮')} menu (top right) → {_bold('Copy account ID')}")
    print()
    try:
        input(f"  Press Enter when you have your Account ID ready...")
    except (EOFError, KeyboardInterrupt):
        print()
        return
    print()
    print(f"  {_bold('Now create a Cloudflare API token:')}")
    print()
    print(f"  1. Go to: {_cyan('https://dash.cloudflare.com/profile/api-tokens')}")
    print(f"  2. Click {_bold('Create Token')} → {_bold('Custom token')} → {_bold('Get started')}")
    print(f"  3. Give it a name, e.g. {_bold('studio-console')}")
    print(f"  4. Under {_bold('Permissions')}, add these three:")
    print(f"       Account  →  Cloudflare Tunnel         →  Edit")
    print(f"       Account  →  Access: Apps and Policies →  Edit")
    print(f"       Zone     →  DNS                       →  Edit")
    print(f"  5. Under {_bold('Account Resources')}: Include → All accounts  {_dim('(or your specific account)')}")
    print(f"  6. Under {_bold('Zone Resources')}: Include → Specific zone → your domain")
    print(f"  7. {_dim('Client IP filtering and TTL — leave blank (console sets up IP rules separately)')}")
    print(f"  8. Click {_bold('Continue to summary')} → {_bold('Create Token')}")
    print(f"  9. Copy the token — {_bold('it is only shown once')}")
    print()
    try:
        input(f"  Press Enter when you have your token ready...")
    except (EOFError, KeyboardInterrupt):
        print()
        return
    print()


def _step_token(env_file: Path) -> CloudflareAPI | None:
    """Collect account ID + API token, validate, save to .env."""
    env_data = read_env(env_file)
    existing_token = env_data.get("CLOUDFLARE_API_TOKEN", "")
    existing_account_id = env_data.get("CLOUDFLARE_ACCOUNT_ID", "").strip()

    print()
    if existing_token and _is_non_interactive():
        # Non-interactive: trust the values that env / .env supplied and move on.
        token = existing_token
        account_id = existing_account_id
        if not account_id:
            error("Account ID is required (set CLOUDFLARE_ACCOUNT_ID)")
            return None
    elif existing_token:
        # Re-run: just confirm or replace existing credentials
        hint = f"{_dim('[set]')} (enter=keep, paste new to replace)"
        raw = _prompt(f"Cloudflare API token {hint}", "").strip()
        token = raw if raw else existing_token

        account_id = existing_account_id
        if not account_id:
            account_id = _prompt("Account ID", "").strip()
            if not account_id:
                error("Account ID is required")
                return None
    else:
        # First run: show instructions or proceed if they're ready
        idx = _interactive_single(
            "Cloudflare API token",
            ["I have a token and my Account ID ready", "Show me how to create a token"],
            default=0,
        )
        if idx == 1:
            _print_token_instructions()

        # Account ID first — they already have it from the instructions
        print()
        print(f"  {_dim('Find your Account ID:')}")
        print(f"  - In the URL: dash.cloudflare.com/{_bold('<account-id>')}/...")
        print(f"  - Or: Account home → click the {_bold('⋮')} menu (top right) → {_bold('Copy account ID')}")
        print()
        account_id = _prompt("Account ID", existing_account_id).strip()
        if not account_id:
            error("Account ID is required")
            return None

        try:
            token = getpass.getpass(f"▸ Cloudflare API token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not token:
            warn("No token entered")
            return None

    info("Validating token...")
    cf = CloudflareAPI(token, account_id)
    try:
        valid = cf.verify_token()
    except CloudflareError as e:
        if e.status_code in (401, 403):
            error(
                "Token rejected (401/403) — it's invalid, expired, or missing "
                "scopes. Tunnel setup needs: Account:Cloudflare Tunnel, "
                "Account:Access: Apps and Policies, Zone:DNS, Zone:Zone (Read)."
            )
        else:
            error(f"Could not validate token: {e}")
        return None
    if not valid:
        error("Token validation failed — check that the token is correct and not expired")
        return None

    set_env_value(env_file, "CLOUDFLARE_API_TOKEN", token)
    set_env_value(env_file, "CLOUDFLARE_ACCOUNT_ID", account_id)
    ok(f"Token valid  (account: {account_id})")
    return cf


# ---------------------------------------------------------------------------
# Step 2 — Tunnel create/reuse
# ---------------------------------------------------------------------------


def _fetch_tunnel_token(cf: CloudflareAPI, tunnel_id: str) -> str | None:
    """Fetch tunnel token, return None on failure."""
    info("Fetching tunnel token...")
    try:
        return cf.get_tunnel_token(tunnel_id)
    except CloudflareError as e:
        error(f"Failed to get tunnel token: {e}")
        return None


def _save_tunnel(env_file: Path, tunnel_id: str, tunnel_token: str) -> None:
    """Persist tunnel ID, token, and cloudflared compose profile to .env."""
    set_env_value(env_file, "CLOUDFLARE_TUNNEL_ID", tunnel_id)
    set_env_value(env_file, "CLOUDFLARE_TUNNEL_TOKEN", tunnel_token)
    env_data = read_env(env_file)
    profiles = env_data.get("COMPOSE_PROFILES", "")
    if "cloudflared" not in profiles:
        set_env_value(env_file, "COMPOSE_PROFILES", (profiles + ",cloudflared").strip(","))


def _confirm_tunnel_overwrite(cf: CloudflareAPI, tunnel: dict) -> bool:
    """Warn before reusing a tunnel that already has ingress for other hostnames."""
    try:
        existing = cf.get_tunnel_config(tunnel["id"])
    except CloudflareError as e:
        warn(f"Could not read tunnel config: {e} — proceeding anyway")
        return True
    if not existing:
        return True

    print()
    warn(f"Tunnel '{tunnel['name']}' currently routes:")
    for rule in existing:
        host = rule.get("hostname", "?")
        svc = rule.get("service", "?")
        print(f"  - {host} → {svc}")
    print(f"  {_dim('Continuing replaces these rules with the hostnames you configure now.')}")
    return _interactive_yn("Replace existing ingress?", default=False)


def _step_tunnel(cf: CloudflareAPI, env_file: Path) -> tuple[str, str] | None:
    """Select or create a tunnel. Returns (tunnel_id, tunnel_token) or None on abort."""
    env_data = read_env(env_file)
    existing_id = env_data.get("CLOUDFLARE_TUNNEL_ID", "")
    existing_token = env_data.get("CLOUDFLARE_TUNNEL_TOKEN", "")

    print()
    info("Fetching tunnels...")
    try:
        tunnels = cf.list_tunnels()
    except CloudflareError as e:
        warn(f"Could not list tunnels: {e}")
        tunnels = []

    # If stored tunnel still exists, offer to keep it
    if existing_id and any(t.get("id") == existing_id for t in tunnels):
        name = next((t["name"] for t in tunnels if t["id"] == existing_id), existing_id)
        ok(f"Existing tunnel: {name} ({existing_id})")
        if _is_non_interactive() or not _interactive_yn(
            "Reconfigure? (No = keep current)", default=False
        ):
            return existing_id, existing_token

    # Non-interactive: jump straight to "create new tunnel" with default name.
    if _is_non_interactive():
        idx = len(tunnels)  # = "Create new tunnel"
    else:
        # Build selection list
        options = [
            f"{t['name']}  {(_green if t.get('status') == 'healthy' else _dim)(t.get('status', ''))}  {_dim(t['id'][:8] + '...')}"
            for t in tunnels
        ]
        options.append("Create new tunnel")
        idx = _interactive_single("Select tunnel to use", options, default=len(tunnels))

    tunnel_map: dict[int, dict] = {i: t for i, t in enumerate(tunnels)}
    create_idx = len(tunnels)

    if idx < create_idx:
        # Reuse selected tunnel
        tunnel = tunnel_map[idx]
        tunnel_id = tunnel["id"]
        if tunnel_id != existing_id and not _confirm_tunnel_overwrite(cf, tunnel):
            return None
        tunnel_token = _fetch_tunnel_token(cf, tunnel_id)
        if not tunnel_token:
            return None
        ok(f"Using tunnel: {tunnel['name']} ({tunnel_id})")
        _save_tunnel(env_file, tunnel_id, tunnel_token)
        return tunnel_id, tunnel_token

    # Create new tunnel — loop to handle name collisions
    if _is_non_interactive():
        name = _tunnel_name(env_file)
    else:
        preset = read_env(env_file).get("CLOUDFLARE_TUNNEL_NAME", "") or "studio"
        name = _prompt("Tunnel name", preset)
    while True:
        info(f"Creating tunnel '{name}'...")
        try:
            tunnel = cf.create_tunnel(name)
            tunnel_id = tunnel["id"]
            tunnel_token = _fetch_tunnel_token(cf, tunnel_id)
            if not tunnel_token:
                return None
            ok(f"Tunnel created: {name} ({tunnel_id})")
            _save_tunnel(env_file, tunnel_id, tunnel_token)
            return tunnel_id, tunnel_token
        except CloudflareError as e:
            if "already have a tunnel" in str(e).lower() or "already exists" in str(e).lower():
                warn(f"A tunnel named '{name}' already exists.")
                if _is_non_interactive():
                    # Non-interactive: silently reuse the existing tunnel.
                    match = next((t for t in tunnels if t.get("name") == name), None)
                    if not match:
                        error(f"Could not find '{name}' in tunnel list")
                        return None
                    tunnel_id = match["id"]
                    tunnel_token = _fetch_tunnel_token(cf, tunnel_id)
                    if not tunnel_token:
                        return None
                    ok(f"Reusing tunnel: {name} ({tunnel_id})")
                    _save_tunnel(env_file, tunnel_id, tunnel_token)
                    return tunnel_id, tunnel_token
                action = _interactive_single(
                    "What would you like to do?",
                    [f"Use the existing '{name}' tunnel", "Choose a different name", "Cancel"],
                    default=0,
                )
                if action == 0:
                    match = next((t for t in tunnels if t.get("name") == name), None)
                    if not match:
                        error(f"Could not find '{name}' in tunnel list")
                        return None
                    tunnel_id = match["id"]
                    tunnel_token = _fetch_tunnel_token(cf, tunnel_id)
                    if not tunnel_token:
                        return None
                    ok(f"Using tunnel: {name} ({tunnel_id})")
                    _save_tunnel(env_file, tunnel_id, tunnel_token)
                    return tunnel_id, tunnel_token
                elif action == 1:
                    name = _prompt("New tunnel name", "")
                    if not name:
                        return None
                else:
                    return None
            else:
                error(f"Failed to create tunnel: {e}")
                return None


# ---------------------------------------------------------------------------
# Step 3 — Public domain + zone
# ---------------------------------------------------------------------------


def _step_domain(cf: CloudflareAPI, env_file: Path) -> tuple[str, str, str, str] | None:
    """Collect/confirm public domain and resolve zone ID.

    Returns (public_url, hostname, zone_id, root_domain) or None on abort.
    """
    env_data = read_env(env_file)
    current_url = env_data.get("SHS_PUBLIC_BASE_URL", "")

    print()
    domain_url = _prompt(
        "Public domain (e.g. https://studio.example.com)",
        current_url if current_url.startswith("https://") else "",
    ).strip().rstrip("/")

    if not domain_url.startswith("https://"):
        warn("Must start with https://")
        return None

    hostname, subdomain, root_domain = _parse_domain(domain_url)

    # Resolve zone
    info(f"Looking up zone for {root_domain}...")
    zone_id = _resolve_zone(cf, root_domain)
    if not zone_id:
        warn(f"Zone '{root_domain}' not found in your Cloudflare account")
        zone_id = _prompt("Enter zone ID manually (or leave blank to skip DNS setup)", "").strip()
        if not zone_id:
            warn("DNS records will not be created automatically")

    if zone_id:
        set_env_value(env_file, "CLOUDFLARE_ZONE_ID", zone_id)

    set_env_value(env_file, "SHS_PUBLIC_BASE_URL", domain_url)
    return domain_url, hostname, zone_id, root_domain


# ---------------------------------------------------------------------------
# Step 4 — Routes + ingress config
# ---------------------------------------------------------------------------


def _confirm_dns_overwrite(
    cf: CloudflareAPI, zone_id: str, hostname: str, tunnel_id: str
) -> bool:
    """Return True if it's safe to upsert the tunnel CNAME at `hostname`.

    Silent when there's no existing record, or when the existing record is
    already pointing at this exact tunnel (re-running the wizard). Prompts the
    operator only when a conflicting record exists — anything that isn't our
    tunnel CNAME — so they can decide whether to overwrite.
    """
    try:
        records = cf.list_dns_records(zone_id, name=hostname)
    except CloudflareError as e:
        warn(f"Could not check existing DNS for {hostname}: {e}")
        return True  # don't block — let upsert_dns_record surface the real error

    if not records:
        return True

    expected = f"{tunnel_id}.cfargotunnel.com"
    matches_us = all(
        r.get("type") == "CNAME" and r.get("content") == expected
        for r in records
    )
    if matches_us:
        return True  # already ours — no-op overwrite, don't prompt

    # Surface the conflict in operator-readable form.
    warn(f"{hostname} already has DNS records that aren't pointing at this tunnel:")
    for r in records:
        rtype = r.get("type", "?")
        content = r.get("content", "?")
        print(f"    {rtype:5s} {hostname}  →  {content}")
    if _is_non_interactive():
        # Don't silently stomp existing records when running scripted; skip and warn.
        warn(f"Refusing to overwrite {hostname} in non-interactive mode")
        return False
    return _interactive_yn(
        f"Overwrite with tunnel CNAME ({expected})?",
        default=False,
    )


def _step_routes(
    cf: CloudflareAPI,
    env_file: Path,
    tunnel_id: str,
    hostnames: list[str],
    zone_id: str,
    root_domain: str,
) -> list[str] | None:
    """Configure tunnel ingress rules and DNS records for one or more hostnames.

    nginx is the sole entry point — every public hostname maps to the nginx
    front door and nginx splits internally by path.
    """
    print()
    ingress: list[dict] = [
        {"hostname": h, "service": _ingress_target(env_file, h)} for h in hostnames
    ]

    info("Pushing tunnel ingress config...")
    try:
        cf.put_tunnel_config(tunnel_id, ingress)
        for rule in ingress:
            ok(f"Ingress: {rule['hostname']} → {rule['service']}")
    except CloudflareError as e:
        error(f"Failed to configure ingress: {e}")
        return None
    # Record the origin this push wrote (all rules share it).
    set_env_value(env_file, INGRESS_ORIGIN_KEY, _ingress_target(env_file))

    # DNS records — one CNAME per hostname. Existing non-tunnel records
    # trigger a confirm prompt before overwrite (operators may have other
    # services on the same subdomain we shouldn't silently stomp).
    # Track each hostname's outcome so we can report a summary — a half-applied
    # route set with no report of what landed is exactly the silent dead-end
    # (hostname resolves nowhere → 404/DNS error the operator can't diagnose).
    dns_ok: list[str] = []
    dns_failed: list[str] = []
    dns_manual: list[str] = []  # declined overwrite or no zone — operator must act
    for h in hostnames:
        if zone_id:
            if not _confirm_dns_overwrite(cf, zone_id, h, tunnel_id):
                warn(f"Skipping DNS for {h} — operator declined overwrite")
                dns_manual.append(h)
                continue
            info(f"Creating DNS record: {h} → tunnel")
            try:
                cf.upsert_dns_record(zone_id, h, tunnel_id)
                ok(f"DNS: {h}")
                dns_ok.append(h)
            except CloudflareError as e:
                warn(f"DNS record failed for {h}: {e}")
                dns_failed.append(h)
        else:
            warn(f"Skipping DNS for {h} — create this CNAME manually:")
            print(f"    {h}  →  {tunnel_id}.cfargotunnel.com  (proxied)")
            dns_manual.append(h)

    # Summary — surface exactly which hostnames are live vs. need action.
    if dns_failed or dns_manual:
        print()
        warn("DNS not fully applied — these hostnames will not resolve yet:")
        for h in dns_failed:
            print(f"    {h}  (FAILED — retry 'Update domain' or check Zone:DNS scope)")
        for h in dns_manual:
            print(f"    {h}  →  {tunnel_id}.cfargotunnel.com  (create this CNAME, proxied)")
        if dns_ok:
            print(f"  {_dim('Applied OK: ' + ', '.join(dns_ok))}")

    # A genuine API failure (not an operator-declined/manual skip) means the
    # route set is partially applied — signal it so the caller warns and the
    # operator knows to re-run rather than assuming success.
    if dns_failed:
        return None

    return hostnames


# ---------------------------------------------------------------------------
# Step 5 — Zero Trust Access application
# ---------------------------------------------------------------------------


# Env vars storing each Access app id, keyed by role.
_ACCESS_APP_ENV_VAR = {
    "ui": "CLOUDFLARE_ACCESS_APP_ID",
    "api": "CLOUDFLARE_ACCESS_API_APP_ID",
}


def _default_access_app_name(hostname: str) -> str:
    """Suggest a CF Access app name based on the public hostname.

    "app-mac.self-hoststudio.com" -> "Studio - app-mac"
    "api.example.com"             -> "Studio - api"
    "example.com"                 -> "Studio - example.com"  (rare, apex)
    """
    parts = hostname.split(".")
    if len(parts) >= 3:
        return f"Studio - {parts[0]}"
    return f"Studio - {hostname}"


def _ensure_access_app(
    cf: CloudflareAPI,
    env_file: Path,
    role: str,
    hostnames: list[str],
    apps: list[dict] | None = None,
) -> str | None:
    """Create or reuse a Zero Trust Access app for the given role ("ui" or "api").

    Stores the resulting app id in the role-specific env var. If an app already
    exists for this role and is still present in Cloudflare, its domains are
    updated to match `hostnames`. The interactive selection menu is only shown
    on first setup for this role.
    """
    env_var = _ACCESS_APP_ENV_VAR[role]
    primary_host = hostnames[0] if hostnames else ""
    default_name = _default_access_app_name(primary_host)

    env_data = read_env(env_file)
    existing_app_id = env_data.get(env_var, "")

    if apps is None:
        info("Fetching Access applications...")
        try:
            apps = cf.list_access_apps()
        except CloudflareError as e:
            warn(f"Could not list Access apps: {e}")
            apps = []

    # If stored app still exists, offer to keep it as-is.
    if existing_app_id and any(a.get("id") == existing_app_id for a in apps):
        name = next((a["name"] for a in apps if a["id"] == existing_app_id), existing_app_id)
        ok(f"Existing {role.upper()} Access app: {name} ({existing_app_id})")
        if _is_non_interactive() or not _interactive_yn(
            f"Reconfigure {role.upper()} Access app? (No = keep current)", default=False
        ):
            info(f"Updating domains on {role.upper()} app...")
            try:
                cf.update_access_app_domains(existing_app_id, hostnames)
                ok(f"{role.upper()} Access app domains updated")
            except CloudflareError as e:
                warn(f"Could not update domains: {e}")
            return existing_app_id

    # Non-interactive: jump straight to "create new" with the suggested default name.
    if _is_non_interactive():
        idx = len(apps)  # = create new
    else:
        # Build selection list — let the operator reuse an existing app if they want.
        options = [f"{a['name']}  {_dim(a['id'][:8] + '...')}" for a in apps]
        options.append(f"Create new Access app ({default_name})")
        idx = _interactive_single(
            f"Select Access application for the {role.upper()} hostname",
            options,
            default=len(apps),
        )

    app_map: dict[int, dict] = {i: a for i, a in enumerate(apps)}
    create_idx = len(apps)

    if idx < create_idx:
        app = app_map[idx]
        app_id = app["id"]
        info(f"Updating domains on existing app...")
        try:
            cf.update_access_app_domains(app_id, hostnames)
            ok(f"Using Access app: {app['name']} ({app_id})")
        except CloudflareError as e:
            warn(f"Could not update domains: {e}")
        set_env_value(env_file, env_var, app_id)
        return app_id

    # Create new — but first handle name collisions gracefully so the operator
    # gets a 3-way choice (reuse / rename / cancel) instead of a raw API error.
    app_name = (
        default_name
        if _is_non_interactive()
        else _prompt("Access application name", default_name).strip()
    )
    while True:
        existing = next((a for a in apps if a.get("name") == app_name), None)
        if existing:
            warn(f"An Access app named '{app_name}' already exists.")
            if _is_non_interactive():
                # Reuse silently — same tunnel-collision policy.
                app_id = existing["id"]
                info(f"Updating domains on existing app...")
                try:
                    cf.update_access_app_domains(app_id, hostnames)
                    ok(f"Using Access app: {app_name} ({app_id})")
                except CloudflareError as e:
                    warn(f"Could not update domains: {e}")
                set_env_value(env_file, env_var, app_id)
                return app_id
            choice = _interactive_single(
                "What would you like to do?",
                [
                    f"Use the existing '{app_name}' app",
                    "Pick a different name",
                    "Cancel",
                ],
                default=0,
            )
            if choice == 0:
                app_id = existing["id"]
                info(f"Updating domains on existing app...")
                try:
                    cf.update_access_app_domains(app_id, hostnames)
                    ok(f"Using Access app: {app_name} ({app_id})")
                except CloudflareError as e:
                    warn(f"Could not update domains: {e}")
                set_env_value(env_file, env_var, app_id)
                return app_id
            if choice == 1:
                app_name = _prompt("Access application name", default_name).strip()
                continue
            return None

        info(f"Creating {role.upper()} Access application...")
        try:
            app = cf.create_access_app(app_name, hostnames)
            app_id = app.get("id", "") if isinstance(app, dict) else ""
            if not app_id:
                error("Access app created but no ID returned")
                return None
            set_env_value(env_file, env_var, app_id)
            ok(f"Access app created: {app_name} ({app_id})")
            return app_id
        except CloudflareError as e:
            error(f"Failed to create Access app: {e}")
            return None


# ---------------------------------------------------------------------------
# Step 6 — IP bypass policy
# ---------------------------------------------------------------------------


_IP_POLICY_NAME = "Studio Console - IP Bypass"


def _apply_ip_policy(cf: CloudflareAPI, app_id: str, ip_ranges: list[str], role: str) -> None:
    """Upsert console's IP bypass policy on a single Access app."""
    existing_policy_id = ""
    try:
        policies = cf.list_access_policies(app_id)
        for p in policies:
            if p.get("name") == _IP_POLICY_NAME:
                existing_policy_id = p.get("id", "")
                break
    except CloudflareError:
        pass
    info(f"Applying IP policy to {role.upper()} app...")
    _upsert_ip_policy(cf, app_id, _IP_POLICY_NAME, ip_ranges, existing_policy_id)


def _remove_ip_policy(cf: CloudflareAPI, app_id: str, role: str) -> None:
    """Delete console's IP bypass policy from an Access app, if present.

    Used when the operator changes ip_restrict_mode such that this app should
    no longer be gated. We only ever touch policies we created (matched by name).
    """
    try:
        policies = cf.list_access_policies(app_id)
    except CloudflareError as e:
        warn(f"Could not list policies on {role.upper()} app: {e}")
        return
    for p in policies:
        if p.get("name") == _IP_POLICY_NAME:
            try:
                cf.delete_access_policy(app_id, p["id"])
                ok(f"Removed IP policy from {role.upper()} app")
            except CloudflareError as e:
                warn(f"Could not remove IP policy from {role.upper()} app: {e}")
            return


def _prompt_ip_restrict_mode(current: str, is_split: bool) -> str:
    """Ask which roles to gate by IP. Returns 'none', 'ui', or 'both'."""
    if _is_non_interactive():
        return current
    both_label = "UI + API" if is_split else "UI"
    modes = ["none", "ui", "both"] if is_split else ["none", "both"]
    labels = (
        [
            "None — public, no IP gating",
            "UI only — gate UI, leave API public",
            f"{both_label} — gate all hostnames",
        ]
        if is_split
        else ["None — public, no IP gating", f"{both_label} — gate with IP bypass"]
    )
    default = modes.index(current) if current in modes else 0
    idx = _interactive_single("Restrict access by IP?", labels, default=default)
    return modes[idx]


def _step_ip_policy(
    cf: CloudflareAPI,
    env_file: Path,
    gated_apps: list[tuple[str, str]],
    ungated_apps: list[tuple[str, str]],
) -> None:
    """Create/update the IP bypass policy on every gated app; remove from ungated.

    `gated_apps` and `ungated_apps` are each lists of (role, app_id) tuples.
    The same IP set is applied to all gated apps. Ungated apps have any
    console-managed policy removed so they remain public.
    """
    if not gated_apps and not ungated_apps:
        return

    if not gated_apps:
        # ip_restrict_mode == "none" — make sure no stale policy remains.
        for role, app_id in ungated_apps:
            _remove_ip_policy(cf, app_id, role)
        return

    print()
    if len(gated_apps) > 1:
        info(f"Configuring IP allowlist for: {', '.join(r.upper() for r, _ in gated_apps)}")

    # Pre-set allowlist via env / .env? Skip the prompts entirely when present.
    # Comma-separated CIDRs; bare IPs auto-promoted to /32 by _normalise_cidr.
    env_data = read_env(env_file)
    preset_raw = (
        os.environ.get("CONSOLE_IP_ALLOWLIST", "") or env_data.get("CONSOLE_IP_ALLOWLIST", "")
    ).strip()
    if preset_raw:
        ip_ranges = [
            _normalise_cidr(p) for p in preset_raw.split(",") if p.strip()
        ]
        for cidr in ip_ranges:
            _warn_broad_cidr(cidr)
        info(f"Using CONSOLE_IP_ALLOWLIST: {', '.join(ip_ranges)}")
        for role, app_id in gated_apps:
            _apply_ip_policy(cf, app_id, ip_ranges, role)
        for role, app_id in ungated_apps:
            _remove_ip_policy(cf, app_id, role)
        return

    # Prompt path — used when CONSOLE_IP_ALLOWLIST isn't set (the preset_raw branch
    # above), even in non-interactive mode. Catch-all for "operator forgot,"
    # VPN-but-secrets-file-has-old-IP, new location, etc. The fully-specified
    # CONSOLE_IP_ALLOWLIST path stays prompt-free.
    detected_ip = _detect_home_ip()
    if detected_ip:
        info(f"Detected your IP: {detected_ip}")
        print(f"  {_dim('If your ISP assigns dynamic IPs, this rule will break when your IP changes.')}")
        print(f"  {_dim('Use a CIDR range or update the rule after each IP change (Cloudflare → Update IP rules).')}")
        print(f"  {_dim('Press enter to use detected IP, or type skip to skip.')}")
    else:
        warn("Could not detect your IP — enter it manually or type skip to skip")

    home_ip_raw = _prompt(
        "Your IP for bypass",
        detected_ip,
    ).strip()

    if not home_ip_raw or home_ip_raw.lower() == "skip":
        warn("Skipping bypass policy")
        return

    ip_ranges: list[str] = [_normalise_cidr(home_ip_raw)]
    _warn_broad_cidr(ip_ranges[0])

    extra = _prompt("Additional IPs? (comma-separated CIDRs, leave blank to skip)", "").strip()
    if extra:
        for part in extra.split(","):
            cidr = _normalise_cidr(part)
            if cidr and cidr not in ip_ranges:
                _warn_broad_cidr(cidr)
                ip_ranges.append(cidr)

    for role, app_id in gated_apps:
        _apply_ip_policy(cf, app_id, ip_ranges, role)
    for role, app_id in ungated_apps:
        _remove_ip_policy(cf, app_id, role)


# ---------------------------------------------------------------------------
# Update IP rules (standalone, called from submenu option 4)
# ---------------------------------------------------------------------------


def _read_bypass_ips(cf: CloudflareAPI, app_id: str) -> tuple[list[str], dict | None]:
    """Return (ips, policy_dict) for the console-managed bypass policy on app_id.

    Returns ([], None) if the policy doesn't exist or the API call fails.
    """
    try:
        policies = cf.list_access_policies(app_id)
    except CloudflareError as e:
        error(f"Could not fetch policies: {e}")
        return [], None
    bypass = next((p for p in policies if p.get("name") == _IP_POLICY_NAME), None)
    if not bypass:
        return [], None
    ips: list[str] = []
    for rule in bypass.get("include", []):
        ip_rule = rule.get("ip", {})
        if ip_rule.get("ip"):
            ips.append(ip_rule["ip"])
    return ips, bypass


def _hostname_for_role(env_file: Path, role: str) -> str:
    """Bare hostname for an Access role, derived from .env URLs."""
    env_data = read_env(env_file)
    url_var = "SHS_PUBLIC_BASE_URL" if role == "ui" else "CONSOLE_PUBLIC_API_BASE_URL"
    url = env_data.get(url_var, "")
    if not url.startswith("https://"):
        return ""
    return url[len("https://") :].split("/", 1)[0]


def update_ip_rules(env_file: Path) -> None:
    """Interactively add/remove/replace IPs on the bypass policy.

    Lazy-creates an Access app for any role the operator wants to gate but
    doesn't yet have an app for. Conversely, removing all IPs from a role
    deletes that role's Access app so the hostname becomes public again
    (an Access app with no policy blocks everyone).
    """
    cf = _load_api(env_file)
    if not cf:
        warn("Cloudflare API token required — cannot update IP rules.")
        return

    env_data = read_env(env_file)
    ui_app_id = env_data.get("CLOUDFLARE_ACCESS_APP_ID", "")
    api_app_id = env_data.get("CLOUDFLARE_ACCESS_API_APP_ID", "")
    has_ui_host = bool(env_data.get("SHS_PUBLIC_BASE_URL", "").startswith("https://"))
    has_api_host = bool(env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").startswith("https://"))

    if not has_ui_host:
        warn("No public UI domain configured. Run 'Full setup (API)' first.")
        return

    # Decide which role(s) to operate on.
    available_roles: list[str] = ["ui"]
    if has_api_host:
        available_roles.append("api")

    if len(available_roles) == 1:
        target_roles = ["ui"]
    else:
        labels = [
            f"UI       {_dim('gates the UI hostname (browser sessions)')}",
            f"API      {_dim('gates the API hostname (webhooks, OAuth callbacks)')}",
            f"Both     {_dim('apply same edit to UI and API — keeps them in sync')}",
        ]
        idx = _interactive_single("Which hostname's IP rule do you want to edit?", labels, default=2)
        target_roles = {0: ["ui"], 1: ["api"], 2: ["ui", "api"]}[idx]

    # Resolve current state per role: existing app (or empty) + current IPs.
    role_app: dict[str, str] = {"ui": ui_app_id, "api": api_app_id}
    info("Fetching current policies...")
    per_role_state: list[tuple[str, str, list[str], dict | None]] = []
    for role in target_roles:
        app_id = role_app[role]
        if app_id:
            ips, policy = _read_bypass_ips(cf, app_id)
        else:
            ips, policy = [], None
        per_role_state.append((role, app_id, ips, policy))

    union_ips: list[str] = []
    for _, _, ips, _ in per_role_state:
        for ip in ips:
            if ip not in union_ips:
                union_ips.append(ip)

    print()
    if union_ips:
        print(f"  {_bold('Current bypass IPs:')}")
        for ip in union_ips:
            print(f"    {_green('●')} {ip}")
    else:
        warn("No bypass policy found — will create one on each selected hostname")

    print()
    idx = _interactive_single(
        "What would you like to do?",
        ["Add IP(s)", "Remove IP(s)", "Replace all IPs"],
        default=0,
    )

    if idx == 0:
        raw = _prompt("IP(s) to add (comma-separated)").strip()
        new_ips = [_normalise_cidr(p) for p in raw.split(",") if p.strip()]
        for cidr in new_ips:
            _warn_broad_cidr(cidr)
        updated = list(dict.fromkeys(union_ips + new_ips))
    elif idx == 1:
        if not union_ips:
            warn("No IPs to remove")
            return
        pick = _interactive_single("Remove which IP?", union_ips, default=0)
        removed = union_ips[pick]
        updated = [ip for ip in union_ips if ip != removed]
    else:
        raw = _prompt("New IP list (comma-separated CIDRs)").strip()
        updated = [_normalise_cidr(p) for p in raw.split(",") if p.strip()]
        for cidr in updated:
            _warn_broad_cidr(cidr)

    # Empty IP list — refuse to apply, warn the operator that they need to
    # delete the Access app manually if they want a truly public hostname.
    # An app with no policy blocks everyone (Cloudflare login code page),
    # so silently leaving the policy empty would break the hostname.
    if not updated:
        warn("Refusing to apply an empty IP list — an Access app with no rule blocks everyone.")
        print()
        print(f"  {_dim('To make the hostname public, delete the Access app in the Cloudflare')}")
        print(f"  {_dim('dashboard manually. Then re-run studio-console init to refresh state.')}")
        return

    # Apply the IPs. Lazy-create the Access app for any role missing one.
    fresh_apps: list[dict] | None = None
    for role, app_id, _, policy in per_role_state:
        if not app_id:
            host = _hostname_for_role(env_file, role)
            if not host:
                warn(f"Skipping {role.upper()}: no hostname configured for it")
                continue
            if fresh_apps is None:
                try:
                    fresh_apps = cf.list_access_apps()
                except CloudflareError:
                    fresh_apps = []
            info(f"No {role.upper()} Access app yet — creating one...")
            app_id = _ensure_access_app(cf, env_file, role, [host], apps=fresh_apps) or ""
            if not app_id:
                warn(f"Could not create {role.upper()} Access app — skipping")
                continue
            policy = None  # new app, no existing policy

        policy_id = policy["id"] if policy else ""
        info(f"Updating IP policy on {role.upper()} app...")
        _upsert_ip_policy(cf, app_id, _IP_POLICY_NAME, updated, policy_id)

    # Reflect newly-gated apps in CONSOLE_IP_RESTRICT_MODE so it stays consistent
    # with what's deployed. Lazy-create above may have added apps; this catches
    # those. We never downgrade the mode here — that would require deleting an
    # app, which we don't do automatically.
    after = read_env(env_file)
    has_ui_app = bool(after.get("CLOUDFLARE_ACCESS_APP_ID", ""))
    has_api_app = bool(after.get("CLOUDFLARE_ACCESS_API_APP_ID", ""))
    if has_ui_app and has_api_app:
        set_env_value(env_file, "CONSOLE_IP_RESTRICT_MODE", "both")
    elif has_ui_app:
        set_env_value(env_file, "CONSOLE_IP_RESTRICT_MODE", "both" if not has_api_host else "ui")


# ---------------------------------------------------------------------------
# Update domain (standalone, called from submenu option 6)
# ---------------------------------------------------------------------------


_DOMAIN_ROLE_CONFIG = {
    "ui": {
        "label": "UI",
        "url_var": "SHS_PUBLIC_BASE_URL",
        "app_var": "CLOUDFLARE_ACCESS_APP_ID",
    },
    "api": {
        "label": "API",
        "url_var": "CONSOLE_PUBLIC_API_BASE_URL",
        "app_var": "CLOUDFLARE_ACCESS_API_APP_ID",
    },
}


def _push_tunnel_ingress(
    cf: CloudflareAPI, env_file: Path, tunnel_id: str
) -> None:
    """Re-push the tunnel ingress config based on current SHS_PUBLIC_*_BASE_URL.

    Always called after a domain change so the tunnel rules match what the
    rest of the system thinks the public hostnames are.
    """
    env_data = read_env(env_file)

    hostnames: list[str] = []
    ui_url = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/")
    api_url = _api_base_url(env_file)
    if ui_url.startswith("https://"):
        hostnames.append(_parse_domain(ui_url)[0])
    if api_url.startswith("https://"):
        api_host = _parse_domain(api_url)[0]
        if api_host not in hostnames:
            hostnames.append(api_host)

    if not hostnames:
        warn("No public hostnames configured — skipping tunnel ingress update")
        return

    ingress = [{"hostname": h, "service": _ingress_target(env_file, h)} for h in hostnames]
    info("Pushing tunnel ingress config...")
    try:
        cf.put_tunnel_config(tunnel_id, ingress)
        for rule in ingress:
            ok(f"Ingress: {rule['hostname']} → {rule['service']}")
    except CloudflareError as e:
        warn(f"Failed to push ingress: {e}")
        return
    # Record the origin this push wrote (all rules share it).
    set_env_value(env_file, INGRESS_ORIGIN_KEY, _ingress_target(env_file))


def _update_one_domain(cf: CloudflareAPI, env_file: Path, role: str) -> bool:
    """Change the public hostname for a single role ("ui" or "api"). Returns
    True iff something changed (so the caller knows to re-push tunnel ingress).
    """
    cfg = _DOMAIN_ROLE_CONFIG[role]
    label = cfg["label"]
    url_var = cfg["url_var"]
    app_var = cfg["app_var"]

    env_data = read_env(env_file)
    old_url = env_data.get(url_var, "")
    zone_id = env_data.get("CLOUDFLARE_ZONE_ID", "")
    tunnel_id = env_data.get("CLOUDFLARE_TUNNEL_ID", "")
    app_id = env_data.get(app_var, "")

    print()
    new_url = _prompt(
        f"New {label} domain (e.g. https://{role}.example.com)",
        old_url if old_url.startswith("https://") else "",
    ).strip().rstrip("/")

    if not new_url.startswith("https://"):
        warn("Must start with https://")
        return False

    if new_url == old_url:
        info(f"{label} domain unchanged")
        return False

    new_hostname, _, new_root = _parse_domain(new_url)
    old_hostname = _parse_domain(old_url)[0] if old_url else ""

    if zone_id and old_hostname and old_hostname != new_hostname:
        if _interactive_yn(f"Delete old DNS records for {old_hostname}?", default=True):
            info(f"Removing DNS records for {old_hostname}...")
            try:
                records = cf.list_dns_records(zone_id, name=old_hostname)
                for rec in records:
                    if rec.get("type") == "CNAME":
                        cf.delete_dns_record(zone_id, rec["id"])
                        ok(f"Deleted: {rec.get('name')}")
            except CloudflareError as e:
                warn(f"DNS cleanup error: {e}")

    new_zone_id = zone_id
    old_root = _parse_domain(old_url)[2] if old_url else ""
    if not old_root or new_root != old_root:
        info(f"Looking up zone for {new_root}...")
        resolved = _resolve_zone(cf, new_root)
        if resolved:
            new_zone_id = resolved
            set_env_value(env_file, "CLOUDFLARE_ZONE_ID", new_zone_id)
        else:
            warn(f"Zone '{new_root}' not found — DNS records must be created manually")
            new_zone_id = ""

    set_env_value(env_file, url_var, new_url)
    ok(f"{label} domain updated to {new_url}")

    if new_zone_id and tunnel_id:
        info(f"Creating DNS record for {new_hostname}...")
        try:
            cf.upsert_dns_record(new_zone_id, new_hostname, tunnel_id)
            ok(f"DNS: {new_hostname} → tunnel")
        except CloudflareError as e:
            warn(f"DNS record failed: {e}")

    if app_id and new_hostname:
        info(f"Updating {label} Access app domain...")
        try:
            cf.update_access_app_domains(app_id, [new_hostname])
            ok(f"{label} Access app updated: {new_hostname}")
        except CloudflareError as e:
            warn(f"Access app update failed: {e}")

    return True


def update_domain(env_file: Path) -> None:
    """Change one or both public domains — updates DNS, Access apps, and ingress."""
    cf = _load_api(env_file)
    if not cf:
        warn("Cloudflare API token required — cannot update domain.")
        return

    env_data = read_env(env_file)
    has_api = bool(env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").startswith("https://"))
    tunnel_id = env_data.get("CLOUDFLARE_TUNNEL_ID", "")

    if has_api:
        idx = _interactive_single(
            "Which domain do you want to change?",
            [
                f"UI domain   {_dim('the UI hostname (e.g. app.example.com)')}",
                f"API domain  {_dim('the API hostname (e.g. api.example.com)')}",
                f"Both        {_dim('change UI, then API')}",
            ],
            default=0,
        )
        roles = {0: ["ui"], 1: ["api"], 2: ["ui", "api"]}[idx]
    else:
        roles = ["ui"]

    changed = False
    for role in roles:
        if _update_one_domain(cf, env_file, role):
            changed = True

    if changed:
        # Recompute derived URL vars after domain change.
        from ..commands_container import _sync_derived_urls

        _sync_derived_urls(env_file)

    if changed and tunnel_id:
        # Re-push ingress so new hostnames take effect — old hostnames are
        # implicitly removed because put_tunnel_config replaces the whole list.
        _push_tunnel_ingress(cf, env_file, tunnel_id)

    print()
    if changed:
        warn("Restart the cloudflared container to pick up the new domain:")
        print(f"    studio-console → Cloudflare → Restart tunnel")


# ---------------------------------------------------------------------------
# Full setup wizard entry point
# ---------------------------------------------------------------------------


def cf_full_setup(env_file: Path, non_interactive: bool = False) -> bool:
    """Run the full PAT-driven Cloudflare setup wizard.

    Returns True on success, False on any abort (preflight conflict,
    unresolvable zone, missing creds, operator cancel). Callers should treat
    False as a hard stop — the install is in an indeterminate state and
    should not proceed.

    With non_interactive=True, prompts that have a sensible default (tunnel
    name, Access app name, DNS overwrite confirmation, etc.) are skipped and
    the default is used. Required values still come from env / .env.
    """
    global _NON_INTERACTIVE
    prev = _NON_INTERACTIVE
    _NON_INTERACTIVE = non_interactive
    try:
        return _cf_full_setup_impl(env_file)
    finally:
        _NON_INTERACTIVE = prev


def _tunnel_name(env_file: Path) -> str:
    """Resolve the required tunnel name from CLOUDFLARE_TUNNEL_NAME (env or .env).

    Both tunnel creation and preflight must agree on this, or preflight checks a
    different name than the one we create. No default — non-interactive callers
    must set it explicitly.
    """
    name = (
        os.environ.get("CLOUDFLARE_TUNNEL_NAME", "")
        or read_env(env_file).get("CLOUDFLARE_TUNNEL_NAME", "")
    )
    if not name:
        fatal("CLOUDFLARE_TUNNEL_NAME is not set — set it before running CF setup.")
    return name


def _preflight_conflicts(
    cf: CloudflareAPI,
    env_file: Path,
    hostnames: list[str],
    zone_id: str,
    gated_roles: list[str],
    ui_host: str,
    api_host: str,
) -> bool:
    """Check for Cloudflare resources that already exist before we touch anything.

    Returns True iff it's safe to proceed. On any conflict, prints the full
    list and returns False — non-interactive mode aborts so the operator
    cleans up in one pass instead of stumbling into conflicts piecemeal.

    Skipped entirely outside non-interactive mode; the existing prompts in
    each step handle conflicts there.
    """
    if not _is_non_interactive():
        return True

    env_data = read_env(env_file)
    conflicts: list[str] = []
    tunnel_name = _tunnel_name(env_file)

    # 1. Tunnel name — only a conflict if operator hasn't pinned a tunnel ID.
    if not env_data.get("CLOUDFLARE_TUNNEL_ID", ""):
        try:
            tunnels = cf.list_tunnels()
        except CloudflareError as e:
            warn(f"Could not list tunnels for preflight: {e}")
            tunnels = []
        if any(t.get("name") == tunnel_name for t in tunnels):
            conflicts.append(f"  Tunnel:        {tunnel_name}")

    # 2. Access app names — one per gated role, default name format.
    try:
        apps = cf.list_access_apps()
    except CloudflareError as e:
        warn(f"Could not list Access apps for preflight: {e}")
        apps = []
    pinned_ui_app = env_data.get("CLOUDFLARE_ACCESS_APP_ID", "")
    pinned_api_app = env_data.get("CLOUDFLARE_ACCESS_API_APP_ID", "")
    app_conflicts: list[str] = []
    for role in gated_roles:
        if role == "ui" and pinned_ui_app:
            continue
        if role == "api" and pinned_api_app:
            continue
        host = ui_host if role == "ui" else api_host
        wanted = _default_access_app_name(host)
        if any(a.get("name") == wanted for a in apps):
            app_conflicts.append(f"                 {wanted}")
    if app_conflicts:
        app_conflicts[0] = "  Access apps:  " + app_conflicts[0].lstrip()
        conflicts.extend(app_conflicts)

    # 3. DNS — non-tunnel records on any of our planned hostnames.
    if zone_id:
        expected_tunnel = env_data.get("CLOUDFLARE_TUNNEL_ID", "")
        # Tunnel ID isn't known yet (we run before tunnel creation) so any
        # CNAME pointing at *.cfargotunnel.com is treated as "ours" for the
        # purposes of preflight — we'd reuse it in non-interactive mode.
        dns_conflicts: list[str] = []
        for h in hostnames:
            try:
                records = cf.list_dns_records(zone_id, name=h)
            except CloudflareError:
                records = []
            for r in records:
                content = r.get("content", "")
                rtype = r.get("type", "")
                if rtype == "CNAME" and content.endswith(".cfargotunnel.com"):
                    continue
                dns_conflicts.append(f"                 {h}  ({rtype} → {content})")
        if dns_conflicts:
            dns_conflicts[0] = "  DNS records:  " + dns_conflicts[0].lstrip()
            conflicts.extend(dns_conflicts)

    if not conflicts:
        return True

    print()
    error("Cannot bootstrap — Cloudflare resources already exist:")
    print()
    for line in conflicts:
        print(line)
    print()
    print(f"  {_dim('Delete these in the Cloudflare dashboard before re-running, or set')}")
    print(f"  {_dim('CLOUDFLARE_TUNNEL_ID / CLOUDFLARE_ACCESS_APP_ID / CLOUDFLARE_ACCESS_API_APP_ID')}")
    print(f"  {_dim('to reuse them intentionally.')}")
    return False


def _cf_full_setup_impl(env_file: Path) -> bool:
    from ..tui import heading
    heading("Cloudflare Setup")

    # Step 1: API token + account ID
    cf = _step_token(env_file)
    if not cf:
        return False

    # Step 2: Resolve public domains + zone. Domains are written by the wizard's
    # network section before this runs. Split mode is enabled iff a separate
    # API hostname is set and differs from the UI hostname. We resolve before
    # creating anything so preflight can check all planned hostnames at once.
    env_data = read_env(env_file)
    ui_url = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/")
    api_url = _api_base_url(env_file)
    ip_mode = env_data.get("CONSOLE_IP_RESTRICT_MODE", "none")

    if not ui_url.startswith("https://"):
        error("No public HTTPS domain set — configure it in Settings → Public access first")
        return False

    ui_host, _, ui_root = _parse_domain(ui_url)
    api_host = ""
    api_root = ""
    if api_url.startswith("https://"):
        api_host, _, api_root = _parse_domain(api_url)
        if api_host == ui_host:
            api_host = ""  # not really split if hostnames coincide

    is_split = bool(api_host)
    hostnames = [ui_host, api_host] if is_split else [ui_host]

    ok(f"UI domain:  {ui_url}")
    if is_split:
        ok(f"API domain: {api_url}")

    # Resolve zone(s). For now we keep one CLOUDFLARE_ZONE_ID — both hostnames
    # are expected to live in the same zone for the common case (same root
    # domain). If the API hostname is in a different zone, surface a warning
    # and fall back to manual DNS for it.
    info(f"Looking up zone for {ui_root}...")
    zone_id = _resolve_zone(cf, ui_root) or ""
    if zone_id:
        set_env_value(env_file, "CLOUDFLARE_ZONE_ID", zone_id)
    else:
        warn(f"Zone '{ui_root}' not found — DNS records must be created manually")

    if is_split and api_root != ui_root:
        warn(
            f"API hostname is in a different root domain ({api_root}) — "
            "DNS for it must be set up separately."
        )

    # Ask which roles to gate by IP. Default to the stored mode; interactive so a
    # stale "none" can't silently skip Access-app creation.
    ip_mode = _prompt_ip_restrict_mode(ip_mode, is_split)
    set_env_value(env_file, "CONSOLE_IP_RESTRICT_MODE", ip_mode)
    if ip_mode == "both":
        gated_roles = ["ui", "api"] if is_split else ["ui"]
    elif ip_mode == "ui":
        gated_roles = ["ui"]
    else:
        gated_roles = []

    # Step 3: Preflight conflict check (non-interactive only). Aborts before
    # any creation so the operator gets one comprehensive list to clean up.
    if not _preflight_conflicts(
        cf, env_file, hostnames, zone_id, gated_roles, ui_host, api_host
    ):
        return False

    # Step 4: Tunnel — create or reuse.
    result = _step_tunnel(cf, env_file)
    if not result:
        return False
    tunnel_id, _ = result

    # Step 5: Ingress rules + DNS records (one per hostname).
    if not _step_routes(cf, env_file, tunnel_id, hostnames, zone_id, ui_root):
        return False

    # Step 6: Cloudflare Access apps + IP policy. Only create apps for roles
    # that need gating — an Access app with no policy blocks everyone, so
    # creating one for a role that should be public would defeat the purpose.
    # Operators can add gating later via "Update IP rules".
    if not gated_roles:
        info("Skipping Access app creation — ip_restrict_mode=none")
    else:
        info("Fetching Access applications...")
        try:
            existing_apps = cf.list_access_apps()
        except CloudflareError as e:
            warn(f"Could not list Access apps: {e}")
            existing_apps = []

        gated: list[tuple[str, str]] = []
        failed_roles: list[str] = []
        for role in gated_roles:
            host = ui_host if role == "ui" else api_host
            app_id = _ensure_access_app(cf, env_file, role, [host], apps=existing_apps)
            if app_id:
                gated.append((role, app_id))
            else:
                failed_roles.append(role)

        if gated:
            _step_ip_policy(cf, env_file, gated, [])

        # A role meant to be gated but with no Access app is left PUBLIC — surface
        # it loudly (likely a 403/scope issue) instead of silently exposing it.
        if failed_roles:
            warn(
                "Access app NOT created for: "
                + ", ".join(failed_roles)
                + " — these are currently PUBLIC. Check the token has "
                "Account:Access: Apps and Policies, then re-run 'Update IP rules'."
            )
            return False

    # Summary
    env_data = read_env(env_file)
    print()
    print(f"  {_bold('Cloudflare setup complete:')}")
    print(f"    Tunnel ID:    {env_data.get('CLOUDFLARE_TUNNEL_ID', '—')}")
    print(f"    UI domain:    {env_data.get('SHS_PUBLIC_BASE_URL', '—')}")
    if is_split:
        print(f"    API domain:   {env_data.get('CONSOLE_PUBLIC_API_BASE_URL', '—')}")
    print(f"    UI app:       {env_data.get('CLOUDFLARE_ACCESS_APP_ID', '—')}")
    if is_split:
        print(f"    API app:      {env_data.get('CLOUDFLARE_ACCESS_API_APP_ID', '—')}")
    print(f"    IP restrict:  {ip_mode}")
    print()
    if detect_shape(env_file) == "split":
        print(f"  {_dim('cloudflared will start with Services → Start.')}")
        print()
    print(f"  {_dim(f'Note: {ui_root} (the apex) is not configured — visitors there get a DNS')}")
    print(f"  {_dim('or 404 error. To redirect it to your UI, create a Cloudflare Bulk')}")
    print(f"  {_dim('Redirect manually in the dashboard. See docs/topology.md.')}")
    return True
