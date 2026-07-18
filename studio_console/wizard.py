# studio_console/wizard.py
"""Setup wizard - state, sections, setup menu, env generation."""

import os
import secrets
import sys
from pathlib import Path

from .constants import (
    ALL_COMPONENTS,
    COMPONENT_TO_PROFILE,
    CORE_DEFAULTS,
    SCALE_VARS,
    SCALE_VARS_REVERSE,
)
from .env import (
    _package_root,
    derive_app_db_url,
    detect_context,
    read_env,
    run_quiet,
    set_env_value,
    validate_password,
    write_env,
)
from .tui import (
    NavBack,
    NavExit,
    _ITEM_KEYS,
    _bold,
    _clear_lines,
    _cyan,
    _yellow,
    _dim,
    _green,
    _interactive_multi,
    _interactive_single,
    _interactive_yn,
    _prompt,
    _prompt_password,
    _read_key,
    heading,
    info,
    ok,
    warn,
    warn_header,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_secret() -> str:
    return secrets.token_hex(32)


def _generate_fernet_key() -> str:
    # SHS_CREDENTIAL_ENCRYPTION_KEY must be a Fernet key (32 random bytes,
    # urlsafe-base64-encoded). Plain hex satisfies length checks but Fernet
    # rejects it at runtime — see api/app/infrastructure/security/credential_encryption.py.
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _workspace_dir_default(getter=None) -> str:
    """Resolve the host-side workspace dir as an absolute path.

    Preference order:
      1. SHS_WORKSPACE_DIR (canonical)
      2. SHS_WORKSPACE_HOST (legacy — operators may still have it exported)
      3. ~/.studio

    *getter* is an optional callable taking (key, default) — used by the
    non-interactive wizard to read from existing env_data.
    """
    if getter is not None:
        raw = getter("SHS_WORKSPACE_DIR", "") or getter("SHS_WORKSPACE_HOST", "")
        if raw:
            return os.path.expanduser(raw)
    raw = os.environ.get("SHS_WORKSPACE_DIR") or os.environ.get("SHS_WORKSPACE_HOST")
    if raw:
        return os.path.expanduser(raw)
    return os.path.expanduser("~/.studio")


# ---------------------------------------------------------------------------
# Wizard state
# ---------------------------------------------------------------------------


class SetupState:
    """Holds all wizard selections. Pre-filled from existing .env if available."""

    def __init__(self, env_file: Path) -> None:
        self.env_file = env_file
        self.existing = read_env(env_file) if env_file.exists() else {}

        # Components & workers - restore from existing .env
        existing_components = self.existing.get("CONSOLE_COMPONENTS", "")
        self.components: list[str] = (
            [c.strip() for c in existing_components.split(",") if c.strip()]
            if existing_components
            else []
        )
        self.comfyui_url: str = os.getenv("SHS_COMFYUI_URL") or self.existing.get(
            "SHS_COMFYUI_URL", ""
        )
        # Audio worker GPU: "" = CPU, "all" = all GPUs, "0"/"1"/... = one device
        self.audio_gpu_device: str = self.existing.get("CONSOLE_AUDIO_GPU_DEVICE", "")

        # Worker scale - restore from existing .env
        self.worker_scale: dict[str, str] = {}
        for var, comp in SCALE_VARS_REVERSE.items():
            val = self.existing.get(var, "")
            if val:
                self.worker_scale[comp] = val

        # API/UI replica counts and nginx port
        self.api_replicas: int = int(self.existing.get("CONSOLE_API_REPLICAS", "1"))
        self.ui_replicas: int = int(self.existing.get("CONSOLE_UI_REPLICAS", "1"))
        self.nginx_port: int = int(self.existing.get("SHS_NGINX_PORT", "80"))

        # Network - env vars override .env for quick testing
        self.remote_api_url: str = os.getenv("SHS_API_BASE_URL") or self.existing.get(
            "SHS_API_BASE_URL", "http://localhost:8000"
        )
        self.public_domain: str = os.getenv("SHS_PUBLIC_BASE_URL") or self.existing.get(
            "SHS_PUBLIC_BASE_URL", ""
        )
        # Optional split-hostname API URL. Empty == single-hostname mode.
        self.public_api_domain: str = os.getenv(
            "CONSOLE_PUBLIC_API_BASE_URL"
        ) or self.existing.get("CONSOLE_PUBLIC_API_BASE_URL", "")
        # IP allowlist scope: "none" | "ui" | "both"
        self.ip_restrict_mode: str = self.existing.get("CONSOLE_IP_RESTRICT_MODE", "none")

        # Cloudflare tunnel
        self.cloudflare_tunnel_token: str = self.existing.get(
            "CLOUDFLARE_TUNNEL_TOKEN", ""
        )
        self.cloudflare_tunnel_id: str = self.existing.get("CLOUDFLARE_TUNNEL_ID", "")
        # True when the operator runs cloudflared themselves (External only) —
        # console stores the domain but adds no cloudflared profile.
        self.cloudflare_external: bool = False
        self.cloudflare_mode: str = ""  # "docker", "native", or ""
        if self.cloudflare_tunnel_token:
            existing_profiles = self.existing.get("COMPOSE_PROFILES", "")
            self.cloudflare_mode = (
                "docker" if "cloudflared" in existing_profiles else "native"
            )
        elif self.public_domain and self.public_domain.startswith("https://"):
            self.cloudflare_mode = "native"

        # Secrets (preserve existing, generate new if missing)
        self.jwt_secret: str = self.existing.get("SHS_JWT_SECRET_KEY", "")
        self.worker_secret: str = self.existing.get("SHS_WORKER_SHARED_SECRET", "")
        self.encryption_key: str = self.existing.get(
            "SHS_CREDENTIAL_ENCRYPTION_KEY", ""
        )
        self.postgres_password: str = self.existing.get("POSTGRES_PASSWORD", "")

        # Entitlement (Plus)
        self.entitlement_token: str = self.existing.get("SHS_ENTITLEMENT_TOKEN", "")

        # Studio image version (registry mode)
        self.studio_version: str = self.existing.get("SHS_STUDIO_VERSION", "")

        # Admin
        self.admin_email: str = self.existing.get("SHS_ADMIN_EMAIL", "") or os.getenv(
            "SHS_ADMIN_EMAIL", "admin@example.com"
        )
        self.admin_password: str = ""


# ---------------------------------------------------------------------------
# Wizard sections - each can be re-run independently
# ---------------------------------------------------------------------------


def _section_components_and_scaling(state: SetupState) -> None:
    """Select components + set worker instance counts inline."""
    if state.components:
        pre = {i for i, c in enumerate(ALL_COMPONENTS) if c in state.components}
    else:
        pre = {i for i, c in enumerate(ALL_COMPONENTS) if c in CORE_DEFAULTS}

    picks = _interactive_multi(
        "What runs on this machine?",
        ALL_COMPONENTS,
        selected=pre,
        required=True,
        nav=False,
    )
    selected = [ALL_COMPONENTS[i] for i in picks]

    for core in ("PostgreSQL", "API", "UI"):
        if core not in selected:
            warn(f"{core} is not selected — Studio may not function correctly.")

    state.components = selected

    selected_workers = [c for c in selected if "worker" in c.lower()]
    if selected_workers:
        print()
        _prompt_worker_counts(state, selected_workers)


def _prompt_worker_counts(state: SetupState, workers: list[str]) -> None:
    """Prompt for instance counts for each worker component."""
    for comp in workers:
        current = state.worker_scale.get(comp, "1")
        state.worker_scale[comp] = _prompt(f"{comp} instances", current)
    _prompt_worker_extras(state, workers)


def _prompt_worker_extras(state: SetupState, workers: list[str]) -> None:
    """Per-worker follow-ups: ComfyUI server URL, audio GPU device."""
    if "ComfyUI image worker" in workers:
        state.comfyui_url = _prompt(
            "ComfyUI server URL (the worker proxies to it)",
            state.comfyui_url or "http://host.docker.internal:8188",
        )
    if "Audio worker" in workers:
        if sys.platform == "darwin":
            # Docker on macOS has no nvidia driver; a reservation breaks compose up
            if state.audio_gpu_device:
                info("Audio worker GPU: cleared (no NVIDIA GPU support on macOS, using CPU)")
            state.audio_gpu_device = ""
        else:
            state.audio_gpu_device = _prompt(
                "Audio worker GPU ('all', a CUDA device id, or blank = CPU)",
                state.audio_gpu_device,
            ).strip()


def _section_worker_scaling(state: SetupState) -> None:
    """Configure worker instance counts only (used standalone from Advanced menu)."""
    worker_components = [c for c in state.components if "worker" in c.lower()]
    print()
    _prompt_worker_counts(state, worker_components)


def _section_api_ui_scaling(state: SetupState) -> None:
    """Configure API/UI replica counts and the nginx public port."""
    print()
    raw_port = _prompt("Studio will listen on port", str(state.nginx_port))
    try:
        state.nginx_port = max(1, int(raw_port))
    except ValueError:
        state.nginx_port = 80

    raw_api = _prompt("Number of API server instances", str(state.api_replicas))
    raw_ui = _prompt("Number of UI server instances", str(state.ui_replicas))
    try:
        state.api_replicas = max(1, int(raw_api))
    except ValueError:
        state.api_replicas = 1
    try:
        state.ui_replicas = max(1, int(raw_ui))
    except ValueError:
        state.ui_replicas = 1

    ok(
        f"nginx on port {state.nginx_port} → {state.api_replicas} API, {state.ui_replicas} UI"
    )


def _section_network(state: SetupState) -> None:
    """Configure public domain. Tunnel setup is handled by the Cloudflare section."""
    print()

    if state.public_domain and state.public_domain.startswith("https://"):
        ok(f"Public domain: {state.public_domain}")
        if not _interactive_yn("Change it?", default=False):
            return

    needs_external = _interactive_yn(
        "Will Studio need a public URL? (for OAuth, webhooks, Cloudflare tunnel)",
        default=bool(
            state.public_domain and state.public_domain.startswith("https://")
        ),
    )

    if not needs_external:
        nginx_base = f"http://localhost:{state.nginx_port}"
        state.public_domain = nginx_base
        state.public_api_domain = ""
        state.ip_restrict_mode = "none"
        state.cloudflare_tunnel_token = ""
        state.cloudflare_mode = ""
        return

    root = _ask_root_domain(state)
    ui_sub = _ask_ui_subdomain(state, root)
    state.public_domain = f"https://{ui_sub}.{root}"
    _ask_api_hostname(state, root, ui_sub)


def _split_host_into_root_and_sub(hostname: str) -> tuple[str, str]:
    """Parse "<sub>.<root>" into ("sub", "root.tld[.tld]").

    Returns ("", host) if there's only one label (operator put their UI at the
    apex, which the wizard discourages but existing installs may have done).
    """
    parts = hostname.split(".")
    if len(parts) < 3:
        # example.com -> ("", "example.com") — apex
        return "", hostname
    return parts[0], ".".join(parts[1:])


def _api_hostname_options(root: str, ui_sub: str) -> list[str]:
    """Build the menu of suggested API hostnames given the UI hostname's parts.

    Always offers `api.<root>` (the canonical answer) first. If the UI subdomain
    looks like "<prefix>-<suffix>" with a recognisable prefix, also offers
    `api-<suffix>.<root>`. Always offers `api.<sub>.<root>` when that's
    distinct. Order matters — first entry is the default.
    """
    out: list[str] = [f"api.{root}"]

    # "<prefix>-<suffix>" pattern: app-mac, ui-prod, studio-pc, ...
    known_prefixes = ("app", "ui", "www", "studio", "frontend")
    if "-" in ui_sub:
        prefix, _, suffix = ui_sub.partition("-")
        if prefix in known_prefixes and suffix:
            cand = f"api-{suffix}.{root}"
            if cand not in out:
                out.append(cand)

    # Deep-subdomain shape: api.<sub>.<root>. Only adds a new option when the
    # UI subdomain is non-empty and the candidate isn't already in the list.
    if ui_sub:
        cand = f"api.{ui_sub}.{root}"
        if cand not in out:
            out.append(cand)

    return out


def _ask_root_domain(state: SetupState) -> str:
    """Ask for the apex domain (e.g. example.com) used by Studio.

    Pre-fills from any existing SHS_PUBLIC_BASE_URL on re-run. The wizard
    operates on root + subdomain rather than full URLs so the API menu can
    suggest sensible companions.
    """
    default_root = ""
    if state.public_domain.startswith("https://"):
        existing_host = state.public_domain[len("https://") :].split("/", 1)[0]
        _, default_root = _split_host_into_root_and_sub(existing_host)

    root = _prompt("Root domain (e.g. example.com)", default_root).strip().lower()
    while not root or "." not in root or "/" in root or " " in root:
        warn("Enter a bare domain like example.com — no scheme, no path.")
        root = _prompt("Root domain", default_root).strip().lower()
    return root


def _ask_ui_subdomain(state: SetupState, root: str) -> str:
    """Ask for the UI subdomain. Required (apex/root URLs are not supported)."""
    default_sub = ""
    if state.public_domain.startswith("https://"):
        existing_host = state.public_domain[len("https://") :].split("/", 1)[0]
        sub, existing_root = _split_host_into_root_and_sub(existing_host)
        if existing_root == root:
            default_sub = sub or "app"
        else:
            default_sub = "app"
    else:
        default_sub = "app"

    print(f"  {_dim('Used by the browser. The full UI URL becomes https://<sub>.' + root + '.')}")
    sub = _prompt("UI subdomain", default_sub).strip().lower().rstrip(".")
    while not sub or "/" in sub or " " in sub or "." in sub.strip("."):
        # Allow internal dots (deep subdomain) but reject leading/trailing dots,
        # whitespace, and slashes. Empty string means apex — not supported.
        if not sub:
            warn("Subdomain is required. Apex/root URLs are not supported by the wizard.")
        else:
            warn("Enter just the subdomain — no scheme, no path.")
        sub = _prompt("UI subdomain", default_sub).strip().lower().rstrip(".")
    return sub


def _ask_api_hostname(state: SetupState, root: str, ui_sub: str, required: bool = False) -> None:
    """Optionally configure a separate public hostname for the API via a menu.

    Empty selection means "use the same hostname as the UI" — single-mode.
    When required=True the yes/no prompt is skipped (split is mandatory).
    """
    print()
    print(f"  {_dim('More: docs/topology.md (Why a separate API hostname?)')}")

    if not required and not _interactive_yn(
        "Use a separate hostname for the API?",
        default=bool(state.public_api_domain.startswith("https://")),
    ):
        state.public_api_domain = ""
        # "ui" mode requires split hostnames — reset to "none" if we just left split.
        if state.ip_restrict_mode == "ui":
            state.ip_restrict_mode = "none"
        return

    options = _api_hostname_options(root, ui_sub)

    # Pre-select the matching option if the operator had one already.
    default_idx = 0
    existing_host = ""
    if state.public_api_domain.startswith("https://"):
        existing_host = state.public_api_domain[len("https://") :].split("/", 1)[0]
        if existing_host in options:
            default_idx = options.index(existing_host)
        else:
            # Unknown existing hostname — surface it as a "Custom" entry pre-selected.
            options = options + [f"Custom ({existing_host})"]
            default_idx = len(options) - 1

    if not any(o.startswith("Custom") for o in options):
        options = options + ["Custom (type my own)"]

    labels = [f"https://{o}" if not o.startswith("Custom") else o for o in options]
    idx = _interactive_single("Public API hostname", labels, default=default_idx)
    chosen = options[idx]

    if chosen.startswith("Custom"):
        # Either preserve the previously-typed hostname or ask for a fresh one.
        default_custom = existing_host
        host = _prompt("API hostname (e.g. api.example.com)", default_custom).strip().lower().rstrip("/")
        while not host or "/" in host or " " in host or "." not in host:
            warn("Enter a bare hostname like api.example.com — no scheme, no path.")
            host = _prompt("API hostname", default_custom).strip().lower().rstrip("/")
        state.public_api_domain = f"https://{host}"
    else:
        state.public_api_domain = f"https://{chosen}"


def _ask_ip_restrict_mode(state: SetupState) -> None:
    """Ask whether Cloudflare Access should IP-restrict the UI, both, or neither.

    The "UI only" option is hidden when there's no separate API hostname, since
    a single Access app gates everything on that hostname — there's nothing to
    leave public.
    """
    print()
    has_split = bool(
        state.public_api_domain and state.public_api_domain.startswith("https://")
    )

    print(f"  {_dim('More: docs/topology.md (Choosing IP restrictions)')}")

    if has_split:
        labels = [
            f"No        {_dim('both UI and API public')}",
            f"UI only   {_dim('UI gated, API public — recommended for webhooks/OAuth')}",
            f"Both      {_dim('UI and API both gated by IP allowlist')}",
        ]
        modes = ["none", "ui", "both"]
        default = modes.index(state.ip_restrict_mode) if state.ip_restrict_mode in modes else 1
    else:
        labels = [
            f"No        {_dim('public — anyone with the URL can reach Studio')}",
            f"Yes       {_dim('IP allowlist gates the public hostname')}",
        ]
        # In single-hostname mode "ui" and "both" both mean "the one app".
        # Normalise to "both" so the persisted value is unambiguous.
        modes = ["none", "both"]
        default = 0 if state.ip_restrict_mode == "none" else 1

    idx = _interactive_single("Restrict access by IP allowlist?", labels, default=default)
    state.ip_restrict_mode = modes[idx]


def _prompt_secret(
    label: str,
    current: str,
    min_length: int = 32,
    generator=_generate_secret,
) -> tuple[str, str]:
    """Prompt for a single secret with auto-generate default.

    Returns:
        (value, method) where method is "generated", "manual", or "kept"
    """
    too_short_hint = (
        "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        if generator is _generate_fernet_key
        else "openssl rand -hex 32"
    )

    if current:
        hint = f"{_dim('[set]')} (enter=no-change, new=auto-generate)"
        value = _prompt(f"{label} {hint}", "")
        if not value:
            return current, "kept"
        if value == "new":
            return generator(), "generated"
    else:
        hint = f"(enter=auto-generate) {min_length}+ chars"
        value = _prompt(f"{label} {hint}", "")
        if not value:
            return generator(), "generated"

    while len(value) < min_length:
        warn(
            f"Too short ({len(value)} chars, need {min_length}+). Try: {too_short_hint}"
        )
        value = _prompt(f"{label}", "")
        if not value:
            return generator(), "generated"

    return value, "manual"


def _upstream_block(service: str, port: int, replicas: int, numbered: bool = False) -> str:
    """Return nginx upstream server lines for a service.

    numbered=True forces service-N naming even for replicas=1 (required when
    docker-compose.override.yml is active, which parks the bare service names).
    """
    if replicas <= 1 and not numbered:
        return f"    server {service}:{port};\n"
    return "".join(f"    server {service}-{i + 1}:{port};\n" for i in range(max(replicas, 1)))


def _hostname_from_https(url: str) -> str:
    """Strip https:// and any path. Returns "" for non-https URLs."""
    if not url.startswith("https://"):
        return ""
    return url[len("https://") :].split("/", 1)[0]


def _render_split_nginx_conf(api_upstream: str, ui_upstream: str, ui_host: str, api_host: str) -> str:
    """Build a two-server-block nginx config for split-hostname mode.

    UI server keeps the (api|ws|uploads) regex location so browser sessions hit
    the API same-origin (and inherit any IP allowlist on the UI hostname). The
    dedicated API server block exposes the API on its own hostname for webhooks
    and OAuth callbacks. Both server blocks proxy to the same studio_api
    upstream — there are deliberately two paths, gated by different Access apps.
    """
    return f"""\
upstream studio_api {{
    ip_hash;
{api_upstream.rstrip()}
}}

upstream studio_ui {{
{ui_upstream.rstrip()}
}}

# WebSocket connection upgrade map
map $http_upgrade $connection_upgrade {{
    default upgrade;
    ""      close;
}}

# UI hostname: serves the UI and proxies same-origin /api, /ws, /uploads
# requests from the browser to the API. Whatever Access policy is on this
# hostname applies to all of these paths.
server {{
    listen 80;
    server_name {ui_host};

    # Compression — all responses are proxied from upstreams, so gzip_proxied
    # must be set or nginx skips them by default.
    gzip              on;
    gzip_vary         on;
    gzip_proxied      any;
    gzip_comp_level   5;
    gzip_min_length   256;
    gzip_types
        application/json
        application/javascript
        text/javascript
        text/css
        text/plain
        application/xml
        image/svg+xml
        application/rss+xml
        font/ttf
        font/otf
        application/font-woff;

    location /nginx-health {{
        access_log off;
        return 200 "ok\\n";
        add_header Content-Type text/plain;
    }}

    location = /health {{
        proxy_pass http://studio_api;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    location ~ ^/(api|ws|uploads)(/|$) {{
        proxy_pass http://studio_api;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }}

    # Hashed Next.js assets — content-hashed filenames, safe to cache forever.
    location /_next/static/ {{
        proxy_pass http://studio_ui;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }}

    location / {{
        proxy_pass http://studio_ui;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}

# API hostname: dedicated public entry for webhooks, OAuth callbacks, and
# anything else that must be reachable independent of the UI Access policy.
# All paths route to the API — there is no UI fallback here.
server {{
    listen 80;
    server_name {api_host};

    location /nginx-health {{
        access_log off;
        return 200 "ok\\n";
        add_header Content-Type text/plain;
    }}

    location / {{
        proxy_pass http://studio_api;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }}
}}
"""


def _gpu_override_block(device: str) -> str:
    """worker-audio override granting a CUDA device reservation."""
    if device == "all":
        selector = "              count: all\n"
    else:
        selector = f'              device_ids: ["{device}"]\n'
    return (
        "  worker-audio:\n"
        "    deploy:\n"
        "      resources:\n"
        "        reservations:\n"
        "          devices:\n"
        "            - driver: nvidia\n"
        + selector
        + "              capabilities: [gpu]\n"
    )


def _write_override_and_nginx(state: "SetupState") -> None:
    """Write nginx/studio.conf (always) and docker-compose.override.yml (multi-replica
    and/or audio GPU reservation).

    Single instance: base compose services api/ui are used directly.
    Multi-replica: override adds numbered api-N/ui-N services and parks the base ones.
    nginx is always in the base compose - no override needed for it.
    """
    env_file = state.env_file
    api_replicas = state.api_replicas
    ui_replicas = state.ui_replicas
    nginx_port = state.nginx_port

    override_path = env_file.parent / "docker-compose.override.yml"
    nginx_dir = env_file.parent / "nginx"
    nginx_conf_path = nginx_dir / "studio.conf"
    nginx_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # nginx.conf — always written
    # Single instance uses service names api/ui; multi uses api-N/ui-N.
    # ------------------------------------------------------------------
    # When the override is active, bare service names (api/ui) are profile-parked
    # and only numbered replicas (api-1, ui-1, …) exist. Use numbered names in
    # the nginx conf whenever we're going to write the override.
    use_override = api_replicas > 1 or ui_replicas > 1
    api_upstream = _upstream_block("api", 8000, api_replicas, numbered=use_override)
    ui_upstream = _upstream_block("ui", 3000, ui_replicas, numbered=use_override)

    ui_host = _hostname_from_https(state.public_domain)
    api_host = _hostname_from_https(state.public_api_domain)
    is_split = bool(ui_host and api_host and ui_host != api_host)

    if is_split:
        conf = _render_split_nginx_conf(api_upstream, ui_upstream, ui_host, api_host)
    else:
        # Single-hostname (or local-only) — use the bundled template, which has
        # one server block with no server_name (accepts any Host). Operators on
        # an HTTPS public domain reach it via cloudflared, which only forwards
        # configured hostnames anyway.
        template_path = nginx_dir / "studio.conf.template"
        template = template_path.read_text() if template_path.exists() else _NGINX_CONF_TEMPLATE
        conf = template.replace("{{API_UPSTREAM}}", api_upstream.rstrip("\n")).replace(
            "{{UI_UPSTREAM}}", ui_upstream.rstrip("\n")
        )
    nginx_conf_path.write_text(conf)

    # ------------------------------------------------------------------
    # docker-compose.override.yml - multi-replica and/or audio GPU
    # Multi-replica parks the base api/ui services and adds numbered replicas.
    # ------------------------------------------------------------------
    gpu_block = (
        _gpu_override_block(state.audio_gpu_device)
        if state.audio_gpu_device and "Audio worker" in state.components
        else ""
    )
    if not use_override:
        if gpu_block:
            override_path.write_text(
                "# Auto-generated by studio-console - do not edit by hand.\n"
                "# Re-run studio-console to regenerate.\n"
                "services:\n" + gpu_block
            )
            ok(
                f"nginx/studio.conf + docker-compose.override.yml written "
                f"(audio GPU: {state.audio_gpu_device})"
            )
            return
        if override_path.exists():
            override_path.unlink()
        ok(f"nginx/studio.conf written (1 API, 1 UI → port {nginx_port})")
        return

    workspace_bind = "${SHS_STORAGE_ROOT}:/workspace"
    version_var = "${SHS_STUDIO_VERSION:?SHS_STUDIO_VERSION is required - set in .env}"

    api_services: list[str] = []
    for i in range(api_replicas):
        api_services.append(
            f"  api-{i + 1}:\n"
            f"    container_name: studio-api-{i + 1}\n"
            f"    image: ghcr.io/selfhosthub/studio-api:{version_var}\n"
            # Replicas are distinct services, NOT scaled instances of `api`, so
            # they do not inherit the base service's env_file. Without this the
            # API can't read SHS_DATABASE_URL and crashloops on bootstrap.
            f"    env_file:\n"
            f"      - ${{SHS_WORKSPACE_DIR}}/.env\n"
            f"    environment:\n"
            f"      - SHS_WORKSPACE_ROOT=/workspace\n"
            f"    volumes:\n"
            f"      - {workspace_bind}\n"
            f"    depends_on:\n"
            f"      postgres:\n"
            f"        condition: service_healthy\n"
            f"    extra_hosts:\n"
            f"      - host.docker.internal:host-gateway\n"
            f"    healthcheck:\n"
            f'      test: ["CMD", "curl", "-f", "http://127.0.0.1:8000/health"]\n'
            f"      interval: 30s\n"
            f"      timeout: 10s\n"
            f"      retries: 3\n"
            f"      start_period: 10s\n"
            f"    networks:\n"
            f"      - prod-network\n"
            f"    restart: unless-stopped\n"
            f"    logging:\n"
            f"      driver: json-file\n"
            f"      options:\n"
            f'        max-size: "50m"\n'
            f'        max-file: "3"\n'
        )

    ui_services: list[str] = []
    for i in range(ui_replicas):
        ui_services.append(
            f"  ui-{i + 1}:\n"
            f"    container_name: studio-ui-{i + 1}\n"
            f"    image: ghcr.io/selfhosthub/studio-ui:{version_var}\n"
            f"    environment:\n"
            f"      - NEXT_PUBLIC_API_URL=${{SHS_PUBLIC_API_URL:-${{SHS_API_BASE_URL}}}}\n"
            f"      - NEXT_PUBLIC_WS_URL=${{SHS_WS_URL:?SHS_WS_URL is required - set in .env}}\n"
            f"      - NEXT_PUBLIC_API_ENV=production\n"
            # SSR target; internal nginx.
            f"      - SHS_API_BASE_URL=http://nginx:${{SHS_NGINX_PORT}}\n"
            f"      - SHS_WS_URL=${{SHS_WS_URL}}\n"
            f"    depends_on:\n"
            + "".join(f"      - api-{j + 1}\n" for j in range(api_replicas))
            + f"    networks:\n"
            f"      - prod-network\n"
            f"    restart: unless-stopped\n"
            f"    logging:\n"
            f"      driver: json-file\n"
            f"      options:\n"
            f'        max-size: "50m"\n'
            f'        max-file: "3"\n'
        )

    override = (
        "# Auto-generated by studio-console — do not edit by hand.\n"
        "# Re-run studio-console to regenerate.\n"
        "services:\n"
        "  # Park base api/ui — numbered replicas below take over.\n"
        "  api:\n"
        "    profiles: [nginx-lb-disabled]\n"
        "  ui:\n"
        "    profiles: [nginx-lb-disabled]\n"
        "\n" + "".join(api_services) + "".join(ui_services) + gpu_block
    )
    override_path.write_text(override)
    gpu_note = f", audio GPU: {state.audio_gpu_device}" if gpu_block else ""
    ok(
        f"nginx/studio.conf + docker-compose.override.yml written "
        f"({api_replicas} API, {ui_replicas} UI → port {nginx_port}{gpu_note})"
    )


# Inline nginx template — kept in sync with deploy/nginx/studio.conf.template
_NGINX_CONF_TEMPLATE = """\
upstream studio_api {
    ip_hash;
{{API_UPSTREAM}}
}

upstream studio_ui {
{{UI_UPSTREAM}}
}

server {
    listen 80;

    # Compression — all responses are proxied from upstreams, so gzip_proxied
    # must be set or nginx skips them by default.
    gzip              on;
    gzip_vary         on;
    gzip_proxied      any;
    gzip_comp_level   5;
    gzip_min_length   256;
    gzip_types
        application/json
        application/javascript
        text/javascript
        text/css
        text/plain
        application/xml
        image/svg+xml
        application/rss+xml
        font/ttf
        font/otf
        application/font-woff;

    # Health endpoint for the nginx container itself
    location /nginx-health {
        access_log off;
        return 200 "ok\\n";
        add_header Content-Type text/plain;
    }

    # API health check (top-level route, not under /api)
    location = /health {
        proxy_pass http://studio_api;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API + WebSocket + static uploads (org media served by API)
    location ~ ^/(api|ws|uploads)(/|$) {
        proxy_pass http://studio_api;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket upgrade
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Hashed Next.js assets — content-hashed filenames, safe to cache forever.
    location /_next/static/ {
        proxy_pass http://studio_ui;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }

    # UI (everything else)
    location / {
        proxy_pass http://studio_ui;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# WebSocket connection upgrade map (referenced above)
map $http_upgrade $connection_upgrade {
    default upgrade;
    \"\"      close;
}
"""


def _section_secrets(state: SetupState) -> None:
    """Generate or input secrets."""
    print()
    has_secrets = all(
        [
            state.jwt_secret,
            state.worker_secret,
            state.encryption_key,
            state.postgres_password,
        ]
    )
    # SHS_CREDENTIAL_ENCRYPTION_KEY needs a Fernet key, not plain hex.
    secrets_config = [
        ("SHS_JWT_SECRET_KEY", "jwt_secret", 32, _generate_secret),
        ("SHS_WORKER_SHARED_SECRET", "worker_secret", 32, _generate_secret),
        ("SHS_CREDENTIAL_ENCRYPTION_KEY", "encryption_key", 32, _generate_fernet_key),
        ("POSTGRES_PASSWORD", "postgres_password", 16, _generate_secret),
    ]

    if has_secrets:
        if _interactive_yn("All secrets already set. Keep them?"):
            ok("Secrets unchanged")
            return
        # Fall through to per-secret prompts (enter=keep, "new"=regenerate)

    if not has_secrets and _interactive_yn("Generate all secrets automatically?"):
        generated = []
        for label, attr, _, gen in secrets_config:
            if not getattr(state, attr):
                setattr(state, attr, gen())
                generated.append(label)
        ok(f"Auto-generated: {', '.join(generated)}")
        return

    # Per-secret prompts with auto-generate option for each
    _DANGEROUS_SECRETS = {
        "POSTGRES_PASSWORD": (
            "Changing this in .env does NOT change the database password.\n"
            "    The API will fail to connect. Use 'studio-console rotate-secrets' instead."
        ),
        "SHS_CREDENTIAL_ENCRYPTION_KEY": (
            "Changing this makes all stored provider credentials unrecoverable.\n"
            "    Use 'studio-console rotate-secrets' to re-encrypt safely."
        ),
    }
    results: list[tuple[str, str]] = []
    for label, attr, min_len, gen in secrets_config:
        current = getattr(state, attr)
        value, method = _prompt_secret(label, current, min_length=min_len, generator=gen)
        # Block dangerous secret changes with a warning
        if method != "kept" and current and label in _DANGEROUS_SECRETS:
            warn(_DANGEROUS_SECRETS[label])
            if not _interactive_yn(f"Change {label} anyway?", default=False):
                value, method = current, "kept"
        setattr(state, attr, value)
        results.append((label, method))

    # Summary
    generated = [label for label, method in results if method == "generated"]
    manual = [label for label, method in results if method == "manual"]
    kept = [label for label, method in results if method == "kept"]
    if generated:
        ok(f"Auto-generated: {', '.join(generated)}")
    if manual:
        print(f"  {_dim('Manual:')} {', '.join(manual)}")
    if kept:
        print(f"  {_dim('Kept:')} {', '.join(kept)}")
    return


def _section_entitlement(state: SetupState) -> None:
    """Optionally configure the Plus entitlement token."""
    print()
    print(
        f"  {_yellow(_bold('Unlock the Plus catalog with an Entitlement Token.'))}"
    )
    print(f"  {_yellow(_bold('Plus access comes with the SelfHost Innovators membership.'))}")
    print(f"\n  {_cyan('→')} {_cyan(_bold('https://www.skool.com/selfhostinnovators'))}\n")

    if state.entitlement_token:
        if not _interactive_yn(
            "Entitlement Token already configured. Replace it?", default=False
        ):
            return

    if _interactive_yn("Do you have an Entitlement Token?", default=False):
        import getpass

        try:
            token = getpass.getpass(f"▸ Entitlement Token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise KeyboardInterrupt
        if token:
            # Sanity-check length — SelfHostHub tokens are long; a short paste
            # is almost always a truncated/mis-copied value. Runtime validates
            # for real, but catching it here saves a confusing failed activation.
            if len(token) < 20:
                warn(
                    f"That token looks short ({len(token)} chars) — it may be "
                    "truncated. Double-check you pasted the full token."
                )
                if not _interactive_yn("Use it anyway?", default=False):
                    return
            state.entitlement_token = token
            ok("Entitlement Token saved")
        else:
            warn("No token entered - skipping")
    else:
        print()
        print(
            f"  {_yellow('You can configure your token later from Settings → Secrets.')}"
        )


def _section_admin(state: SetupState) -> None:
    """Admin email and password. Skipped on re-run if already bootstrapped."""
    print()
    bootstrapped = state.env_file.parent / ".bootstrapped"
    if bootstrapped.exists():
        return

    env_email = os.getenv("SHS_ADMIN_EMAIL", "")
    env_password = os.getenv("SHS_ADMIN_PASSWORD", "")

    email = ""
    while not email or "@" not in email:
        email = _prompt("Super admin email", env_email or state.admin_email)
        if not email or "@" not in email:
            warn("Enter a valid email address")
    state.admin_email = email

    if env_password:
        valid, msg = validate_password(env_password)
        if valid:
            state.admin_password = env_password
            ok("Super admin password set from SHS_ADMIN_PASSWORD env var")
            return
        else:
            warn(f"SHS_ADMIN_PASSWORD env var invalid: {msg}")

    state.admin_password = _prompt_password("Super admin password")


def _pick_studio_version(state: SetupState) -> None:
    """Prompt for the SHS_STUDIO_VERSION to pull from the registry."""
    from .commands import _fetch_available_versions

    info("Fetching available versions...")
    versions = _fetch_available_versions()
    current = state.studio_version

    if not versions:
        warn("Could not fetch versions from the registry")
        target = _prompt(
            "Studio version to install (e.g. 1.0.0)", current or "1.0.0"
        ).strip()
    else:
        labels = []
        for v in versions:
            marker = f"  {_dim('(current)')}" if v == current else (
                f"  {_dim('(latest)')}" if v == versions[0] else ""
            )
            labels.append(f"{v}{marker}")
        labels.append(f"Enter manually  {_dim('(any tag)')}")

        default = versions.index(current) if current in versions else 0
        idx = _interactive_single(
            "Studio version", labels, default=default
        )
        if idx == len(versions):
            target = _prompt("Studio version (e.g. 1.0.0)", current or "").strip()
        else:
            target = versions[idx]

    if not target:
        warn("No version selected — keeping current value")
        return

    state.studio_version = target
    set_env_value(state.env_file, "SHS_STUDIO_VERSION", target)
    ok(f"SHS_STUDIO_VERSION={target}")


def _section_repo_root(state: SetupState) -> None:
    """Ask whether to pull images or build from source; collect repo path if needed."""
    import glob
    import readline

    def _path_completer(text: str, state_idx: int) -> str | None:
        expanded = os.path.expanduser(text)
        matches = []
        for m in glob.glob(expanded + "*"):
            matches.append(m + "/" if os.path.isdir(m) else m)
        try:
            return matches[state_idx]
        except IndexError:
            return None

    env_data = read_env(state.env_file)
    current = env_data.get("CONSOLE_REPO_ROOT", "").strip()

    print()
    options = [
        f"Pull docker images from registry  {_dim('(recommended)')}",
        f"Build from source  {_dim('(requires local studio repository)')}",
    ]
    default = 1 if current else 0
    idx = _interactive_single("How will you run Studio?", options, default=default)

    if idx == 0:
        if current:
            set_env_value(state.env_file, "CONSOLE_REPO_ROOT", "")
        ok("Images will be pulled from the registry")
        _pick_studio_version(state)
        return

    # Build from source — collect repo path
    print()
    readline.set_completer(_path_completer)
    readline.set_completer_delims(" \t\n;")
    readline.parse_and_bind("tab: complete")

    default_display = current if current else ""

    while True:
        raw = _prompt("Path to local studio directory", default_display).strip()

        if not raw:
            warn("No path entered — skipping (images will be pulled from registry)")
            break

        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            warn(f"Directory not found: {path}")
            default_display = raw
            continue
        if not (path / "api").is_dir():
            warn(f"{path} doesn't look like a studio checkout (no api/ dir)")
            default_display = raw
            continue

        from .env import set_env_value as _set

        _set(state.env_file, "CONSOLE_REPO_ROOT", str(path))
        ok(f"CONSOLE_REPO_ROOT={path}")
        break

    readline.set_completer(None)


def _tunnel_id_from_token(token: str) -> str:
    """Decode the tunnel id embedded in a cloudflared connector token.

    The token is base64-encoded JSON with keys a (account), t (tunnel id),
    s (secret). Returns "" if it can't be decoded.
    """
    import base64
    import json

    try:
        data = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
        return str(data.get("t", ""))
    except Exception:
        return ""


def _bring_your_own_tunnel(state: SetupState) -> None:
    """Collect an operator-supplied tunnel token. No API calls, no .env writes.

    Mutates SetupState only — the final write_env at save time persists the
    token, tunnel id, and cloudflared profile. Aborting/discarding the wizard
    therefore leaves nothing behind (unlike the idx==0 API flow, which must
    persist mid-run to mirror real Cloudflare resources it creates).
    """
    from .tui import error as _error

    print()
    print(f"  {_dim('Paste the tunnel token from: Cloudflare → Zero Trust → Networks')}")
    print(f"  {_dim('→ Tunnels → your tunnel → Configure (the long eyJ... string).')}")
    print()
    token = _prompt("Cloudflare tunnel token", "").strip()
    if not token:
        warn("No token entered — skipping Cloudflare setup.")
        return

    tunnel_id = _tunnel_id_from_token(token)
    if not tunnel_id:
        _error("That doesn't look like a tunnel token (expected base64 eyJ...).")
        return

    state.cloudflare_tunnel_token = token
    state.cloudflare_tunnel_id = tunnel_id
    state.cloudflare_external = False
    state.cloudflare_mode = "docker"
    ok(f"Tunnel token stored  (tunnel: {tunnel_id[:8]}...)")
    print(f"  {_dim('cloudflared will start with the stack. Configure ingress + DNS')}")
    print(f"  {_dim('for this tunnel in the Cloudflare dashboard if you have not already.')}")


def _section_cloudflare(state: SetupState) -> None:
    """Optional Cloudflare tunnel setup via PAT."""
    print()

    if not (state.public_domain and state.public_domain.startswith("https://")):
        print(
            f"  {_dim('Skipping — no public domain configured (set one in Public access)')}"
        )
        return

    env_data = read_env(state.env_file)
    already_configured = bool(env_data.get("CLOUDFLARE_TUNNEL_ID"))

    if already_configured:
        ok(
            f"Cloudflare tunnel already configured ({env_data.get('CLOUDFLARE_TUNNEL_ID', '')[:8]}...)"
        )
        if not _interactive_yn("Reconfigure?", default=False):
            return

    # Ask whether they have a tunnel or want console to create one
    idx = _interactive_single(
        "Cloudflare tunnel",
        [
            f"Create a tunnel  {_dim('console creates it via API + runs cloudflared via Docker')}",
            f"Use my tunnel    {_dim('I have a tunnel token — console runs cloudflared via Docker')}",
            f"External only    {_dim('I run cloudflared myself — console just stores the domain')}",
            f"No tunnel        {_dim('LAN / VPN / Tailscale — no Cloudflare needed')}",
        ],
        default=0,
    )

    if idx == 3:
        print(f"  {_dim('No tunnel configured.')}")
        return

    if idx == 2:
        # User manages cloudflared outside console — no Docker profile, no API
        # calls, no .env writes. State-only: the final write_env stores the
        # domain (already in env_data) and, because cloudflare_external clears
        # the tunnel token, omits the cloudflared profile.
        print(
            f"  {_dim('Console will not manage cloudflared. Public domain stored as-is.')}"
        )
        state.cloudflare_external = True
        state.cloudflare_tunnel_token = ""
        state.cloudflare_tunnel_id = ""
        state.cloudflare_mode = "native"
        return

    if idx == 1:
        # Bring-your-own tunnel token — no API calls. Store the connector token
        # + the tunnel id embedded in it, add the cloudflared profile, done.
        _bring_your_own_tunnel(state)
        return

    # idx 0 = create new tunnel via the PAT-driven API flow
    _ask_ip_restrict_mode(state)

    # Flush public domain + split-mode config to .env before cf_full_setup reads them
    from .env import set_env_value as _set

    _set(state.env_file, "SHS_PUBLIC_BASE_URL", state.public_domain)
    _set(state.env_file, "CONSOLE_PUBLIC_API_BASE_URL", state.public_api_domain)
    _set(state.env_file, "CONSOLE_IP_RESTRICT_MODE", state.ip_restrict_mode)

    from .cloudflare.cf_wizard import cf_full_setup

    # idx==0 is the API flow: it creates real Cloudflare resources (tunnel, DNS,
    # Access) and MUST persist their IDs mid-run — those writes are intentional
    # and survive a later discard so the IDs aren't orphaned (per topology.md, we
    # do NOT delete-on-discard). Respect the return value: on partial failure,
    # warn rather than silently proceeding as if setup succeeded.
    completed = cf_full_setup(state.env_file)

    # Sync state from what cf_full_setup wrote — so write_env includes cloudflared profile
    refreshed = read_env(state.env_file)
    state.cloudflare_tunnel_token = refreshed.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    state.cloudflare_tunnel_id = refreshed.get("CLOUDFLARE_TUNNEL_ID", "")
    if state.cloudflare_tunnel_token:
        state.cloudflare_mode = "docker"

    if not completed:
        warn(
            "Cloudflare setup did not complete — any resources created so far "
            "are kept (their IDs are in .env). Re-run 'Create a tunnel' to "
            "finish; it reuses the existing tunnel/DNS/Access instead of "
            "duplicating them."
        )


def _section_network_and_cloudflare(state: SetupState) -> None:
    """Configure public domain then Cloudflare tunnel as one flow."""
    _section_network(state)
    _section_cloudflare(state)


# ---------------------------------------------------------------------------
# Section registry
# ---------------------------------------------------------------------------

WIZARD_SECTIONS = [
    ("Components", _section_components_and_scaling),
    ("Scale API/UI", _section_api_ui_scaling),
    ("Secrets", _section_secrets),
    ("Entitlement token", _section_entitlement),
    ("Source repository", _section_repo_root),
    ("Public access + Cloudflare", _section_network_and_cloudflare),
]


# ---------------------------------------------------------------------------
# Section summaries (for settings menu display)
# ---------------------------------------------------------------------------


def _section_summary(state: SetupState, name: str) -> str:
    """Return a dim summary string for a wizard section."""
    if name == "Components":
        parts: list[str] = []
        if state.components:
            parts.append(", ".join(state.components))
        workers = {k: v for k, v in state.worker_scale.items() if k in state.components}
        if workers:
            parts.append("  " + "  ".join(f"{k} ×{v}" for k, v in workers.items()))
        return "".join(parts) if parts else "none"
    if name == "Secrets":
        return "configured" if state.jwt_secret else "not set"
    if name == "Entitlement token":
        return "configured" if state.entitlement_token else "not set"
    if name == "Source repository":
        env_data = read_env(state.env_file) if state.env_file.exists() else {}
        repo_root = env_data.get("CONSOLE_REPO_ROOT", "")
        if repo_root:
            return repo_root
        version = env_data.get("SHS_STUDIO_VERSION", "") or state.studio_version
        return f"registry (pull) — v{version}" if version else "registry (pull)"
    if name == "Public access + Cloudflare":
        if state.public_domain and state.public_domain.startswith("https://"):
            mode = {"docker": " (tunnel)", "native": " (native)"}.get(
                state.cloudflare_mode, ""
            )
            return state.public_domain + mode
        return "local only"
    if name == "Admin account":
        return f"{state.admin_email} / {'set' if state.admin_password else 'not set'}"
    return ""


# ---------------------------------------------------------------------------
# Settings menu (edit sections, save, or discard)
# ---------------------------------------------------------------------------


def _wizard_setup_menu(state: SetupState, first_run: bool = False) -> bool | None:
    """Interactive setup menu. Returns True=save, None=discard."""
    # Import here to avoid circular import - only needed for password reset
    from .commands import cmd_reset_password

    sections = WIZARD_SECTIONS
    print()  # visual separator from previous output

    def _build_options() -> list[str]:
        max_name = max(len(name) for name, _ in sections)
        opts: list[str] = []
        for name, _ in sections:
            summary = _section_summary(state, name)
            padding = " " * (max_name - len(name) + 4)
            opts.append(f"{name}{padding}{_dim(summary)}")
        return opts

    options = _build_options()
    n_sections = len(options)

    # Extra actions (not wizard sections)
    password_idx = n_sections
    if not first_run:
        options.append("Reset password")

    # Footer actions
    discard_idx = len(options)
    options.append("Discard changes")
    save_idx = len(options)
    options.append("Save")
    total = len(options)

    cursor = 0
    rendered_lines = 0

    def _handle_pick(idx: int) -> bool | None:
        """Handle a menu pick. Returns True=save, None=discard, False=handled."""
        if idx == save_idx:
            return True
        if idx == discard_idx:
            return None
        if idx == password_idx and not first_run:
            if _interactive_yn("Reset the super admin password?", default=False):
                cmd_reset_password(detect_context(), state.env_file)
            return False
        if idx < n_sections:
            try:
                _, func = sections[idx]
                func(state)
            except (NavBack, NavExit):
                pass
            # Refresh summaries
            refreshed = _build_options()
            for i in range(n_sections):
                options[i] = refreshed[i]
        return False

    while True:
        if rendered_lines:
            _clear_lines(rendered_lines)

        lines: list[str] = []
        lines.append(f"{_cyan('▸')} Setup")
        lines.append(f"  {_dim('↑↓=navigate  enter=select')}")
        for i, opt in enumerate(options):
            label = _ITEM_KEYS[i] if i < len(_ITEM_KEYS) else " "
            num = _dim(f"{label}.")
            if i == discard_idx:
                lines.append("")
            if i == cursor:
                lines.append(f"  → {num} {_green('●')} {_bold(opt)}")
            else:
                lines.append(f"    {num} {_dim('○')} {opt}")

        rendered_lines = len(lines)
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

        try:
            key = _read_key()
        except (KeyboardInterrupt, EOFError):
            print()
            raise NavBack

        if key == "up":
            cursor = (cursor - 1) % total
        elif key == "down":
            cursor = (cursor + 1) % total
        elif key == "enter":
            result = _handle_pick(cursor)
            if result is not False:
                return result
        elif key.upper() in _ITEM_KEYS:
            idx = _ITEM_KEYS.index(key.upper())
            if idx < total:
                result = _handle_pick(idx)
                if result is not False:
                    return result


# ---------------------------------------------------------------------------
# Studio directory bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_studio_dir(env_file: Path) -> None:
    """Copy bundled docker-compose.yml and nginx template into the studio dir.

    Safe to call on every wizard save — skips files that are already current
    (compares content, not mtime, so pip upgrades propagate the new compose).
    """
    import shutil

    studio_dir = env_file.parent
    pkg = _package_root()

    files = [
        (pkg / "docker-compose.yml", studio_dir / "docker-compose.yml"),
        (
            pkg / "nginx" / "studio.conf.template",
            studio_dir / "nginx" / "studio.conf.template",
        ),
    ]

    for src, dst in files:
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue
        shutil.copy2(src, dst)
        ok(f"{'Updated' if dst.exists() else 'Copied'}: {dst.name}")

    # Provision the four data subdirs alongside .env at the workspace root.
    # Compose mounts these (e.g. ${SHS_DB_DATA} into postgres), so they must
    # exist before the container starts.
    for sub in ("db", "storage", "models", "backups"):
        (studio_dir / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Wizard entry point
# ---------------------------------------------------------------------------


def wizard(context: str, env_file: Path) -> bool:
    """Interactive setup wizard with review and edit.

    Returns True if the wizard completed and wrote .env, False if aborted.
    """
    state = SetupState(env_file)
    is_rerun = env_file.exists() and bool(state.existing)

    if not is_rerun:
        # First install: walk through all sections sequentially
        heading("Studio Setup")
        i = 0
        while i < len(WIZARD_SECTIONS):
            _, func = WIZARD_SECTIONS[i]
            try:
                func(state)
            except NavBack:
                if i > 0:
                    i -= 1
                continue
            except NavExit:
                if _interactive_yn("Abort setup?", default=False, nav=False):
                    return False
                continue
            i += 1

    # Settings menu - edit sections, save, or discard
    result = _wizard_setup_menu(state, first_run=not is_rerun)
    if result is None:
        return False  # discard

    # -- Derive URLs --
    # nginx is always the front door — all local URLs go through it.
    public_url = state.public_domain
    has_public = public_url.startswith("https://")
    nginx_base = f"http://localhost:{state.nginx_port}"
    nginx_ws = f"ws://localhost:{state.nginx_port}"

    # Browsers send Origin: http://localhost (no port) for port 80, or http://localhost:N for others.
    localhost_origins = (
        "http://localhost"
        if state.nginx_port == 80
        else f"http://localhost:{state.nginx_port}"
    )

    api_public_url = state.public_api_domain if state.public_api_domain.startswith("https://") else ""

    if has_public:
        # Public tunnel via nginx — browser hits public domain for everything.
        # Split mode: API has its own public hostname, so point SHS_API_BASE_URL
        # at it. Single mode: UI fetches /api/* same-origin on its own hostname
        # (nginx routes /api/* to the API upstream under the UI server_name).
        api_url = api_public_url if api_public_url and api_public_url != public_url else public_url
        ws_url = public_url.replace("https://", "wss://")
        frontend_url = public_url
        # CORS: allow UI origin + (in split mode) the public API origin so 3rd
        # parties hitting the public API hostname don't get blocked.
        origins = [localhost_origins, public_url]
        if api_public_url and api_public_url != public_url:
            origins.append(api_public_url)
        cors_origins = ",".join(origins)
    else:
        # Local only — everything through nginx on localhost
        api_url = nginx_base
        ws_url = nginx_ws
        frontend_url = nginx_base
        cors_origins = localhost_origins

    # -- Build .env --
    db_url = f"postgresql+asyncpg://postgres:{state.postgres_password}@postgres:5432/selfhost_studio"
    env_data: dict[str, str] = {
        # Project
        "COMPOSE_PROJECT_NAME": "studio",
        # Secrets
        "SHS_JWT_SECRET_KEY": state.jwt_secret,
        "SHS_WORKER_SHARED_SECRET": state.worker_secret,
        "SHS_CREDENTIAL_ENCRYPTION_KEY": state.encryption_key,
        # URLs
        "SHS_API_BASE_URL": api_url,
        "SHS_PUBLIC_API_URL": api_url,
        "SHS_PUBLIC_BASE_URL": public_url,
        "CONSOLE_PUBLIC_API_BASE_URL": api_public_url,
        "CONSOLE_IP_RESTRICT_MODE": state.ip_restrict_mode,
        # Optional: comma-separated CIDRs to use as the IP allowlist. When set,
        # cf_wizard skips the interactive IP prompt. Read from os.environ at
        # wizard start so scripted installs can pass it without persisting.
        "CONSOLE_IP_ALLOWLIST": os.environ.get("CONSOLE_IP_ALLOWLIST", "")
            or state.existing.get("CONSOLE_IP_ALLOWLIST", ""),
        "SHS_WS_URL": ws_url,
        "SHS_FRONTEND_URL": frontend_url,
        "SHS_CORS_ORIGINS": cors_origins,
        # Database. The app URL opts the API into the restricted shs_app role
        # (RLS enforced); bootstrap provisions it on boot. Preserved across
        # wizard reruns so the password stays stable.
        "SHS_DATABASE_URL": db_url,
        "SHS_DATABASE_APP_URL": state.existing.get("SHS_DATABASE_APP_URL", "")
            or derive_app_db_url(db_url),
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": state.postgres_password,
        "POSTGRES_PORT": "5432",
        # Workspace - container side fixed at /workspace; host side splits
        # into private/ (config, db, backups) and shared/ (orgs, models, data).
        # SHS_WORKSPACE_DIR is the operator-facing knob; PRIVATE/SHARED_ROOT
        # are the live bind-mount sources docker compose reads.
        "SHS_WORKSPACE_ROOT": "/workspace",
        "SHS_WORKSPACE_DIR": _workspace_dir_default(),
        "SHS_DB_DATA": _workspace_dir_default() + "/db",
        "SHS_STORAGE_ROOT": _workspace_dir_default() + "/storage",
        "SHS_MODELS_ROOT": _workspace_dir_default() + "/models",
        "CONSOLE_BACKUP_ROOT": _workspace_dir_default() + "/backups",
        # Version
        "SHS_STUDIO_VERSION": state.studio_version
            or state.existing.get("SHS_STUDIO_VERSION", "1.0.0"),
        # Runtime
        "SHS_ENV": "production",
        "SHS_DEBUG": "false",
        "SHS_LOG_LEVEL": "WARNING",
        "SHS_LOG_FORMAT": "json",
        "SHS_ENABLE_ACCESS_LOGS": "false",
        "SHS_SUPPRESS_WORKER_POLLING_LOGS": "true",
        "SHS_STORAGE_BACKEND": "local",
        "SHS_MAINTENANCE_MODE": "false",
        # Workers
        # CONSOLE_STORAGE_MODE: "local" (default, workers share /workspace volume)
        # or "remote" (workers upload output files via API — for remote GPU workers
        # that cannot mount /workspace, e.g. RunPod/Vast.ai without shared storage).
        "CONSOLE_STORAGE_MODE": "local",
        "SHS_WHISPER_MODEL": "base",
        "SHS_COMFYUI_URL": state.comfyui_url,
        "CONSOLE_AUDIO_GPU_DEVICE": state.audio_gpu_device,
        # Catalog
        "SHS_COMMUNITY_SOURCE": "https://raw.githubusercontent.com/selfhosthub/studio-community/main",
        "SHS_PLUS_SOURCE": "https://raw.githubusercontent.com/selfhosthub/studio-plus/main",
        "SHS_CATALOG_CACHE_HOURS": "168",
        # Compose
        "CONSOLE_COMPONENTS": ",".join(state.components),
    }

    # Build COMPOSE_PROFILES from worker components + cloudflared when console
    # manages the connector. External-only (operator runs cloudflared) never
    # gets the profile, even if an old .env still holds a tunnel token.
    profiles = [
        COMPONENT_TO_PROFILE[c] for c in state.components if c in COMPONENT_TO_PROFILE
    ]
    if state.cloudflare_tunnel_token and not state.cloudflare_external:
        profiles.append("cloudflared")
    env_data["COMPOSE_PROFILES"] = ",".join(profiles)

    # Cloudflare tunnel token + id (console-managed connector only).
    if state.cloudflare_tunnel_token and not state.cloudflare_external:
        env_data["CLOUDFLARE_TUNNEL_TOKEN"] = state.cloudflare_tunnel_token
        if state.cloudflare_tunnel_id:
            env_data["CLOUDFLARE_TUNNEL_ID"] = state.cloudflare_tunnel_id
    elif state.cloudflare_external:
        # Switched to operator-managed: clear any stale connector creds that a
        # prior docker-tunnel run left on disk (write_env preserves old lines,
        # so we must blank them explicitly rather than just omitting them).
        env_data["CLOUDFLARE_TUNNEL_TOKEN"] = ""
        env_data["CLOUDFLARE_TUNNEL_ID"] = ""

    # Worker scale counts
    for comp, var in SCALE_VARS.items():
        if comp in state.components:
            env_data[var] = state.worker_scale.get(comp, "1")

    # API/UI replica counts and nginx port
    env_data["CONSOLE_API_REPLICAS"] = str(state.api_replicas)
    env_data["CONSOLE_UI_REPLICAS"] = str(state.ui_replicas)
    env_data["SHS_NGINX_PORT"] = str(state.nginx_port)

    if state.entitlement_token:
        env_data["SHS_ENTITLEMENT_TOKEN"] = state.entitlement_token

    write_env(env_file, env_data)

    # Copy bundled docker-compose.yml and nginx template into the studio dir
    _bootstrap_studio_dir(env_file)

    # Generate (or remove) docker-compose.override.yml and nginx/studio.conf
    _write_override_and_nginx(state)

    if context == "runpod":
        print()
        warn("RunPod: Ensure a network volume is attached at /workspace")
        warn("Console creates /workspace/private (config, db) and /workspace/shared (orgs, models)")
        warn("Worker pods should mount the same volume at /workspace — they only need shared/")
        warn("Models download on first run and are cached on the network volume")

    # -- Next steps --
    print()
    ok(f"Configuration saved to {env_file}")
    print()
    warn_header(
        f"  Back-up {env_file}\n    Losing system secrets will\n    could make your system unrecoverable."
    )
    print()
    print("  Next:")
    print(f"    {_bold('Services → Start all')}     build images and start Studio")
    print(f"    {_bold('Services → Health')}         check everything is running")
    print(f"    {_bold('Services → Links')}           open UI and API docs")
    print()
    print(
        "  If you add workers on another machine or GPU host:\n"
        f"    {_bold('studio-console worker-kit')}   prints the paste-ready setup"
    )
    print()

    return True


# ---------------------------------------------------------------------------
# Non-interactive init (driven entirely by env vars)
# ---------------------------------------------------------------------------


def wizard_non_interactive(context: str, env_file: Path) -> bool:
    """Write .env from environment variables, no prompts.

    Required for meaningful output: CONSOLE_COMPONENTS, secrets.
    Defaults: port 80, 1 API replica, 1 UI replica, no Cloudflare, registry mode.
    Cloudflare: set all five CF vars + SHS_PUBLIC_BASE_URL to activate.
    """
    g = os.environ.get
    state = SetupState(env_file)

    # Components — accept underscore aliases (e.g. General_worker) as well as canonical names
    _COMPONENT_ALIASES = {c.lower().replace(" ", "_"): c for c in ALL_COMPONENTS}
    raw_components = g("CONSOLE_COMPONENTS", "")
    if raw_components:
        resolved = []
        for c in raw_components.split(","):
            c = c.strip()
            resolved.append(_COMPONENT_ALIASES.get(c.lower(), c))
        state.components = [c for c in resolved if c]
    elif not state.components:
        state.components = list(CORE_DEFAULTS)

    # Worker scale counts
    for comp, var in SCALE_VARS.items():
        val = g(var, "")
        if val:
            state.worker_scale[comp] = val

    # API/UI replicas + nginx port
    try:
        state.nginx_port = int(g("SHS_NGINX_PORT", str(state.nginx_port)))
    except ValueError:
        pass
    try:
        state.api_replicas = int(g("CONSOLE_API_REPLICAS", str(state.api_replicas)))
    except ValueError:
        pass
    try:
        state.ui_replicas = int(g("CONSOLE_UI_REPLICAS", str(state.ui_replicas)))
    except ValueError:
        pass

    # Secrets — env var wins, then existing .env, then auto-generate
    state.jwt_secret = g("SHS_JWT_SECRET_KEY", "") or state.jwt_secret or _generate_secret()
    state.worker_secret = g("SHS_WORKER_SHARED_SECRET", "") or state.worker_secret or _generate_secret()
    state.encryption_key = g("SHS_CREDENTIAL_ENCRYPTION_KEY", "") or state.encryption_key or _generate_fernet_key()
    state.postgres_password = g("POSTGRES_PASSWORD", "") or state.postgres_password or _generate_secret()

    # Entitlement token
    state.entitlement_token = g("SHS_ENTITLEMENT_TOKEN", "") or state.entitlement_token

    # Audio worker GPU device
    state.audio_gpu_device = g("CONSOLE_AUDIO_GPU_DEVICE", state.audio_gpu_device).strip()

    # Source repo
    repo_root = g("CONSOLE_REPO_ROOT", "")
    if repo_root:
        set_env_value(env_file, "CONSOLE_REPO_ROOT", str(Path(repo_root).expanduser().resolve()))

    # Public domain — dynamic, required for Cloudflare
    public_domain = g("SHS_PUBLIC_BASE_URL", "")
    if public_domain:
        state.public_domain = public_domain.rstrip("/")

    # Optional split-mode API hostname + IP-restrict mode (read here so the
    # values get persisted to .env even if the CF bootstrap below is skipped).
    public_api_domain = g("CONSOLE_PUBLIC_API_BASE_URL", "")
    if public_api_domain:
        state.public_api_domain = public_api_domain.rstrip("/")
    state.ip_restrict_mode = g("CONSOLE_IP_RESTRICT_MODE", "") or state.ip_restrict_mode or "none"

    # Cloudflare — two modes:
    #   1. Full credentials provided (api token + account id + tunnel id + tunnel token):
    #      use them as-is, skip the API bootstrap.
    #   2. API token + account id only (tunnel id/token blank):
    #      bootstrap via cf_full_setup — creates tunnel, DNS, Access app(s), IP policy.
    cf_tunnel_token = g("CLOUDFLARE_TUNNEL_TOKEN", "")
    cf_tunnel_id = g("CLOUDFLARE_TUNNEL_ID", "")
    cf_account_id = g("CLOUDFLARE_ACCOUNT_ID", "")
    cf_api_token = g("CLOUDFLARE_API_TOKEN", "")
    has_full_creds = all([cf_tunnel_token, cf_tunnel_id, cf_account_id, cf_api_token,
                          state.public_domain.startswith("https://")])

    # If full creds are claimed, validate the tunnel actually exists in
    # Cloudflare. Stale IDs (deleted tunnel, ID from a different account, etc.)
    # would otherwise be silently trusted — the install "succeeds" but
    # cloudflared can't connect because the tunnel is gone. On any failure,
    # fall through to the bootstrap path so the wizard creates a fresh tunnel.
    if has_full_creds:
        try:
            from .cloudflare.cf_api import CloudflareAPI, CloudflareError

            _cf = CloudflareAPI(cf_api_token, cf_account_id)
            tunnels = _cf.list_tunnels()
            if not any(t.get("id") == cf_tunnel_id for t in tunnels):
                from .tui import warn as _warn
                _warn(
                    f"Tunnel {cf_tunnel_id[:8]}... not found in your Cloudflare "
                    "account — creating a new one"
                )
                has_full_creds = False
                cf_tunnel_id = ""
                cf_tunnel_token = ""
        except Exception as e:
            from .tui import warn as _warn
            _warn(f"Could not validate tunnel — falling back to bootstrap ({e})")
            has_full_creds = False
            cf_tunnel_id = ""
            cf_tunnel_token = ""

    needs_cf_bootstrap = (
        bool(cf_api_token and cf_account_id)
        and not has_full_creds
        and state.public_domain.startswith("https://")
    )
    has_cloudflare = has_full_creds  # may flip to True after bootstrap below

    if has_full_creds:
        state.cloudflare_tunnel_token = cf_tunnel_token
        state.cloudflare_mode = "docker"
        for key, val in [
            ("CLOUDFLARE_TUNNEL_TOKEN", cf_tunnel_token),
            ("CLOUDFLARE_TUNNEL_ID", cf_tunnel_id),
            ("CLOUDFLARE_ACCOUNT_ID", cf_account_id),
            ("CLOUDFLARE_API_TOKEN", cf_api_token),
        ]:
            set_env_value(env_file, key, val)
    elif needs_cf_bootstrap:
        # Persist the API token + account id + URLs first so cf_full_setup can
        # read them. The tunnel id/token will be written by cf_full_setup itself.
        set_env_value(env_file, "CLOUDFLARE_API_TOKEN", cf_api_token)
        set_env_value(env_file, "CLOUDFLARE_ACCOUNT_ID", cf_account_id)
        set_env_value(env_file, "SHS_PUBLIC_BASE_URL", state.public_domain)
        if state.public_api_domain:
            set_env_value(env_file, "CONSOLE_PUBLIC_API_BASE_URL", state.public_api_domain)
        set_env_value(env_file, "CONSOLE_IP_RESTRICT_MODE", state.ip_restrict_mode)

    # Admin credentials
    state.admin_email = g("SHS_ADMIN_EMAIL", "") or state.admin_email
    state.admin_password = g("SHS_ADMIN_PASSWORD", "")

    # -- Derive URLs (same logic as wizard()) --
    public_url = state.public_domain
    has_public = public_url.startswith("https://")
    nginx_base = f"http://localhost:{state.nginx_port}"
    nginx_ws = f"ws://localhost:{state.nginx_port}"
    localhost_origins = (
        "http://localhost" if state.nginx_port == 80 else f"http://localhost:{state.nginx_port}"
    )

    api_public_url = state.public_api_domain if state.public_api_domain.startswith("https://") else ""

    if has_public:
        # Split mode: API has its own public hostname (api.*), so SHS_API_BASE_URL
        # must point there — the UI domain (app.*) sits behind a Cloudflare Access
        # IP rule that blocks 3rd-party webhook callbacks. Single mode: same-origin.
        api_url = api_public_url if api_public_url and api_public_url != public_url else public_url
        ws_url = public_url.replace("https://", "wss://")
        frontend_url = public_url
        origins = [localhost_origins, public_url]
        if api_public_url and api_public_url != public_url:
            origins.append(api_public_url)
        cors_origins = ",".join(origins)
    else:
        api_url = nginx_base
        ws_url = nginx_ws
        frontend_url = nginx_base
        cors_origins = localhost_origins

    # -- Build .env --
    db_url = f"postgresql+asyncpg://postgres:{state.postgres_password}@postgres:5432/selfhost_studio"
    env_data: dict[str, str] = {
        "COMPOSE_PROJECT_NAME": "studio",
        "SHS_JWT_SECRET_KEY": state.jwt_secret,
        "SHS_WORKER_SHARED_SECRET": state.worker_secret,
        "SHS_CREDENTIAL_ENCRYPTION_KEY": state.encryption_key,
        "SHS_API_BASE_URL": api_url,
        "SHS_PUBLIC_API_URL": api_url,
        "SHS_PUBLIC_BASE_URL": public_url or nginx_base,
        "CONSOLE_PUBLIC_API_BASE_URL": state.public_api_domain if state.public_api_domain.startswith("https://") else "",
        "CONSOLE_IP_RESTRICT_MODE": state.ip_restrict_mode,
        "CONSOLE_IP_ALLOWLIST": g("CONSOLE_IP_ALLOWLIST", "") or state.existing.get("CONSOLE_IP_ALLOWLIST", ""),
        "SHS_WS_URL": ws_url,
        "SHS_FRONTEND_URL": frontend_url,
        "SHS_CORS_ORIGINS": cors_origins,
        "SHS_DATABASE_URL": db_url,
        "SHS_DATABASE_APP_URL": g("SHS_DATABASE_APP_URL", "") or derive_app_db_url(db_url),
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": state.postgres_password,
        "POSTGRES_PORT": "5432",
        "SHS_WORKSPACE_ROOT": "/workspace",
        "SHS_WORKSPACE_DIR": _workspace_dir_default(g),
        "SHS_DB_DATA": _workspace_dir_default(g) + "/db",
        "SHS_STORAGE_ROOT": _workspace_dir_default(g) + "/storage",
        "SHS_MODELS_ROOT": _workspace_dir_default(g) + "/models",
        "CONSOLE_BACKUP_ROOT": _workspace_dir_default(g) + "/backups",
        "SHS_STUDIO_VERSION": g("SHS_STUDIO_VERSION", "1.0.0"),
        "SHS_ENV": "production",
        "SHS_DEBUG": "false",
        "SHS_LOG_LEVEL": "WARNING",
        "SHS_LOG_FORMAT": "json",
        "SHS_ENABLE_ACCESS_LOGS": "false",
        "SHS_SUPPRESS_WORKER_POLLING_LOGS": "true",
        "SHS_STORAGE_BACKEND": "local",
        "SHS_MAINTENANCE_MODE": "false",
        "CONSOLE_STORAGE_MODE": "local",
        "SHS_WHISPER_MODEL": "base",
        "SHS_COMFYUI_URL": state.comfyui_url,
        "CONSOLE_AUDIO_GPU_DEVICE": state.audio_gpu_device,
        "SHS_COMMUNITY_SOURCE": "https://raw.githubusercontent.com/selfhosthub/studio-community/main",
        "SHS_PLUS_SOURCE": "https://raw.githubusercontent.com/selfhosthub/studio-plus/main",
        "SHS_CATALOG_CACHE_HOURS": "168",
        "CONSOLE_COMPONENTS": ",".join(state.components),
        "CONSOLE_API_REPLICAS": str(state.api_replicas),
        "CONSOLE_UI_REPLICAS": str(state.ui_replicas),
        "SHS_NGINX_PORT": str(state.nginx_port),
    }

    profiles = [COMPONENT_TO_PROFILE[c] for c in state.components if c in COMPONENT_TO_PROFILE]
    if has_cloudflare:
        profiles.append("cloudflared")
    env_data["COMPOSE_PROFILES"] = ",".join(profiles)

    if state.cloudflare_tunnel_token:
        env_data["CLOUDFLARE_TUNNEL_TOKEN"] = state.cloudflare_tunnel_token

    for comp, var in SCALE_VARS.items():
        if comp in state.components:
            env_data[var] = state.worker_scale.get(comp, "1")

    if state.entitlement_token:
        env_data["SHS_ENTITLEMENT_TOKEN"] = state.entitlement_token

    if state.admin_email:
        env_data["SHS_ADMIN_EMAIL"] = state.admin_email

    write_env(env_file, env_data)
    _bootstrap_studio_dir(env_file)
    _write_override_and_nginx(state)

    # Cloudflare bootstrap — runs only when API token + account id + UI URL are
    # provided but tunnel id/token aren't. Creates tunnel, DNS, Access apps,
    # IP policy. cf_full_setup writes its outputs back to .env directly.
    # If preflight aborts (existing CF resources detected), bail out — leaving
    # the operator to clean up before the rest of the install proceeds.
    if needs_cf_bootstrap:
        from .cloudflare.cf_wizard import cf_full_setup

        if not cf_full_setup(env_file, non_interactive=True):
            from .tui import error as _error
            _error("Cloudflare bootstrap failed — aborting install.")
            return False
        # Sync state from what cf_full_setup wrote so the cloudflared compose
        # profile gets added on the next write_env call.
        refreshed = read_env(env_file)
        new_tunnel_token = refreshed.get("CLOUDFLARE_TUNNEL_TOKEN", "")
        if new_tunnel_token:
            state.cloudflare_tunnel_token = new_tunnel_token
            state.cloudflare_mode = "docker"
            # Re-emit env_data so COMPOSE_PROFILES picks up cloudflared.
            profiles = [COMPONENT_TO_PROFILE[c] for c in state.components if c in COMPONENT_TO_PROFILE]
            profiles.append("cloudflared")
            set_env_value(env_file, "COMPOSE_PROFILES", ",".join(profiles))
            set_env_value(env_file, "CLOUDFLARE_TUNNEL_TOKEN", new_tunnel_token)
            has_cloudflare = True

    ok(f"Configuration saved to {env_file}")
    if has_cloudflare:
        ok(f"Cloudflare tunnel configured: {state.public_domain}")
    return True
