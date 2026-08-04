# studio_console/commands_kit.py
"""Worker kit - launch a worker on this machine, or hand off setup for another."""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

from .constants import WORKER_CATALOG
from .env import read_env, run, run_quiet
from .tui import _bold, _dim, _interactive_single, _prompt, error, heading, info, ok, warn

# worker_type -> studio-workers pip extra (None = base install covers it)
PIP_EXTRAS = {
    "general": None,
    "transfer": None,
    "audio": "audio",
    "video": "video",
    "comfyui-image": "comfyui",
}

# Engines whose inference needs the GPU on the worker host itself
GPU_BOUND_TYPES = {"audio", "video"}

# Placement of the worker relative to the console host
PLACEMENT_LOCAL = "local"
PLACEMENT_LAN = "lan"
PLACEMENT_REMOTE = "remote"

VERSION_PROBE = (
    "from studio_workers.contracts.version import WORKERS_VERSION; print(WORKERS_VERSION)"
)


def _host_arch() -> str:
    """Normalized machine arch of the console host (arm64, x86_64, ...)."""
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and _host_arch() == "arm64"


def _lan_ip() -> str | None:
    """This host's LAN IP via a routing lookup (no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _iface_addrs() -> list[str]:
    """Global-scope IPv4 addresses of this host, LAN-routable first."""
    addrs: list[str] = []
    rc, out = run_quiet(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                addr = parts[parts.index("inet") + 1].split("/")[0]
                if addr not in addrs:
                    addrs.append(addr)
    lan = _lan_ip()
    if lan:
        if lan in addrs:
            addrs.remove(lan)
        addrs.insert(0, lan)
    return addrs


def _probe_status(url: str, timeout: int = 3) -> int:
    """HTTP status of *url* from this machine; 0 = no answer."""
    rc, out = run_quiet(
        ["curl", "-so", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url]
    )
    code = out.strip()
    if not code.isdigit():
        return 0
    return int(code) if int(code) != 0 else 0


def _workers_dist_version(tag: str, runner=subprocess.run) -> str | None:
    """Read WORKERS_VERSION out of the api image; None when the image is absent
    locally or predates the version contract (pre-1.2.8)."""
    image = f"ghcr.io/selfhosthub/studio-api:{tag}"
    present = runner(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if present.returncode != 0:
        return None
    probe = runner(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", VERSION_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
    )
    version = probe.stdout.strip()
    return version if probe.returncode == 0 and version else None


def _tunnel_url(env_data: dict) -> str:
    """Public HTTPS URL for the API (split hostname first), '' when none."""
    split_api = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").rstrip("/")
    if split_api.startswith("https://"):
        return split_api
    public = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/")
    if public.startswith("https://"):
        return public
    return ""


def _default_api_url(
    env_data: dict,
    placement: str,
    native: bool = False,
    lan_ip: str | None = None,
) -> str:
    """Default API URL by worker placement: direct path on this machine and on
    the LAN, tunnel hostname only for remote hosts (the tunnel caps uploads)."""
    port = env_data.get("SHS_NGINX_PORT", "80")
    if placement == PLACEMENT_LOCAL:
        # Native workers use the localhost-published API port: the front door
        # 404s /api/v1/internal/*, so nginx URLs cannot claim jobs.
        return "http://127.0.0.1:8000" if native else f"http://host.docker.internal:{port}"
    if placement == PLACEMENT_LAN:
        return f"http://{lan_ip or socket.gethostname()}:{port}"
    return _tunnel_url(env_data) or f"http://{lan_ip or socket.gethostname()}:{port}"


def _api_hostname(env_data: dict) -> str:
    """The API's own hostname (Host-header routed past the front-door 404)."""
    host = env_data.get("SHS_API_HOSTNAME", "").strip()
    if host:
        return host
    split_api = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").rstrip("/")
    if split_api.startswith("https://"):
        return split_api.split("://", 1)[1].split("/")[0]
    return ""


def _compose_network(env_data: dict) -> str | None:
    """The running stack's compose network, when it exists on this host."""
    project = env_data.get("COMPOSE_PROJECT_NAME", "studio")
    network = f"{project}_prod-network"
    if run_quiet(["docker", "network", "inspect", network])[0] == 0:
        return network
    return None


def _worker_api_target(
    env_data: dict,
    placement: str,
    native: bool,
    lan_ip: str | None = None,
) -> tuple[str, str | None, list[tuple[str, str]], list[str]]:
    """Derive (api_url, docker network, add-host entries, notes) by topology.

    Workers talk to /api/v1/internal/*, which the front door 404s by design;
    the working transports are the compose network (api:8000), the
    api-hostname server block, or the localhost-published API port.
    """
    port = env_data.get("SHS_NGINX_PORT", "80")
    hostname = _api_hostname(env_data)
    if placement == PLACEMENT_LOCAL:
        if native:
            return "http://127.0.0.1:8000", None, [], []
        network = _compose_network(env_data)
        if network:
            return "http://api:8000", network, [], []
        if hostname:
            return (
                f"http://{hostname}:{port}",
                None,
                [(hostname, "host-gateway")],
                [],
            )
        return (
            f"http://host.docker.internal:{port}",
            None,
            [("host.docker.internal", "host-gateway")],
            [
                "No api hostname is configured and the stack's network was not found:",
                "this URL hits the front door, which 404s worker job claims.",
                "Set up split hostnames (Setup wizard, network section).",
            ],
        )
    if placement == PLACEMENT_LAN:
        ip = lan_ip or _lan_ip() or socket.gethostname()
        if hostname:
            add_hosts = [] if native else [(hostname, ip)]
            notes = (
                [f"On the worker machine, add to /etc/hosts: {ip} {hostname}"]
                if native
                else []
            )
            return f"http://{hostname}:{port}", None, add_hosts, notes
        return (
            f"http://{ip}:{port}",
            None,
            [],
            [
                "No api hostname is configured: this URL hits the front door, which",
                "404s worker job claims. Set up split hostnames (Setup wizard).",
            ],
        )
    return _tunnel_url(env_data), None, [], []


def _kit_env_lines(run_env: list[tuple[str, str]]) -> list[str]:
    """KEY=value lines for the kit env file; pure so tests can pin the contract."""
    return [f"{k}={v}" for k, v in run_env]


def _write_kit_env(path: Path, run_env: list[tuple[str, str]]) -> None:
    """Write the kit env file with 0600 perms from creation."""
    content = "\n".join(_kit_env_lines(run_env)) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def _docker_kit_lines(
    entry: dict,
    tag: str,
    gpu: str,
    env_file_ref: str,
    network: str | None = None,
    add_hosts: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Paste-ready docker run command; pure so tests can pin the contract.
    --add-host host.docker.internal is always emitted: without it a Linux
    docker host cannot resolve it and fails late (worker registers, cannot
    reach ComfyUI). *add_hosts* carries the api-hostname mapping by topology."""
    lines = [
        f"docker run -d --name studio-{entry['profile']} \\",
        "  --restart unless-stopped \\",
    ]
    if network:
        lines.append(f"  --network {network} \\")
    if gpu:
        gpu_flag = "--gpus all" if gpu == "all" else f"--gpus 'device={gpu}'"
        lines.append(f"  {gpu_flag} \\")
    hosts = [("host.docker.internal", "host-gateway")]
    for host, ip in add_hosts or []:
        if (host, ip) not in hosts:
            hosts.append((host, ip))
    for host, ip in hosts:
        lines.append(f"  --add-host {host}:{ip} \\")
    lines += [
        "  -v studio-workspace:/workspace \\",
        f"  --env-file {env_file_ref} \\",
        f"  ghcr.io/selfhosthub/{entry['image']}:{tag}",
    ]
    return lines


def _docker_run_argv(
    entry: dict,
    image_ref: str,
    gpu: str,
    env_file_ref: str,
    network: str | None = None,
    add_hosts: list[tuple[str, str]] | None = None,
) -> list[str]:
    """argv form of the kit's docker run, for the console to exec itself.
    Must stay flag-for-flag identical to _docker_kit_lines (pinned by test)."""
    argv = [
        "docker", "run", "-d", "--name", f"studio-{entry['profile']}",
        "--restart", "unless-stopped",
    ]
    if network:
        argv += ["--network", network]
    if gpu:
        argv += ["--gpus", "all" if gpu == "all" else f"device={gpu}"]
    hosts = [("host.docker.internal", "host-gateway")]
    for host, ip in add_hosts or []:
        if (host, ip) not in hosts:
            hosts.append((host, ip))
    for host, ip in hosts:
        argv += ["--add-host", f"{host}:{ip}"]
    argv += [
        "-v", "studio-workspace:/workspace",
        "--env-file", env_file_ref,
        image_ref,
    ]
    return argv


def _pick_lan_ip(env_data: dict, prober=_probe_status) -> str:
    """Pick the address LAN workers reach this host on, each probed live."""
    port = env_data.get("SHS_NGINX_PORT", "80")
    candidates = _iface_addrs()
    host = socket.gethostname()
    if host and "localhost" not in host and host not in candidates:
        candidates.append(host)
    if not candidates:
        return _prompt("Address of this machine on your network", "").strip()
    labeled = [(a, prober(f"http://{a}:{port}/health")) for a in candidates]
    items = [
        a + "  " + _dim("API answers here" if st == 200 else "no API answer from this machine")
        for a, st in labeled
    ]
    items.append("Enter an address")
    idx = _interactive_single("Address workers on your network will use", items, default=0)
    if idx == len(items) - 1:
        return _prompt("Address of this machine on your network", labeled[0][0]).strip()
    return labeled[idx][0]


def _pick_comfyui_url(env_data: dict, native: bool, prober=_probe_status) -> str:
    """Pick-list of ComfyUI candidates probed on /system_stats; custom entry last.
    host.docker.internal is probed as 127.0.0.1 (only containers resolve it)."""
    candidates: list[str] = []
    existing = env_data.get("SHS_COMFYUI_URL", "").rstrip("/")
    if existing:
        candidates.append(existing)
    default_host = "127.0.0.1" if native else "host.docker.internal"
    for host in (default_host, _lan_ip()):
        if host:
            url = f"http://{host}:8188"
            if url not in candidates:
                candidates.append(url)
    labeled = []
    for url in candidates:
        probe_url = url.replace("host.docker.internal", "127.0.0.1")
        labeled.append((url, prober(f"{probe_url}/system_stats")))
    items = [
        url + "  " + _dim("ComfyUI answers" if st == 200 else "no answer from this machine")
        for url, st in labeled
    ]
    items.append("Enter a URL")
    default = next((i for i, (_, st) in enumerate(labeled) if st == 200), 0)
    idx = _interactive_single(
        "ComfyUI server, as reachable FROM THE WORKER", items, default=default
    )
    if idx == len(items) - 1:
        return _prompt(
            "ComfyUI server URL, as reachable FROM THE WORKER",
            labeled[0][0] if labeled else "http://127.0.0.1:8188",
        )
    return labeled[idx][0]


def _measured_notes(api_url: str, prober=_probe_status) -> list[str]:
    """Live reachability of the API URL from this machine, appended to the
    inferred Connectivity verdicts."""
    probe_url = (
        api_url.replace("host.docker.internal", "127.0.0.1")
        .replace("http://api:", "http://127.0.0.1:")
        .rstrip("/")
    )
    status = prober(f"{probe_url}/health")
    if status == 200:
        return [f"Measured from this machine: {probe_url}/health answers 200."]
    if status == 0:
        return [
            f"Measured from this machine: {probe_url}/health did not answer.",
            "(A worker elsewhere may still reach it; this is only the view from here.)",
        ]
    return [f"Measured from this machine: {probe_url}/health answers HTTP {status}."]


def _ensure_worker_image(image: str, tag: str) -> str | None:
    """Resolve the worker image locally (ghcr then bare), pulling when absent."""
    for prefix in ("ghcr.io/selfhosthub", "selfhosthub"):
        ref = f"{prefix}/{image}:{tag}"
        if run_quiet(["docker", "image", "inspect", ref])[0] == 0:
            return ref
    ref = f"ghcr.io/selfhosthub/{image}:{tag}"
    info(f"Pulling {ref} ...")
    try:
        if run(["docker", "pull", ref], check=False, timeout=600).returncode != 0:
            error(f"Pull failed: {ref}")
            return None
    except Exception:
        error(f"Pull failed: {ref}")
        return None
    return ref


def _wait_worker_registered(name: str, attempts: int = 30, interval: int = 2) -> bool:
    """Poll the container log for the API registration line (JWT received)."""
    for _ in range(attempts):
        rc, out = run_quiet(["docker", "logs", "--tail", "100", name])
        if rc == 0 and "Registered with API" in out:
            return True
        if not run_quiet(["docker", "ps", "-q", "--filter", f"name=^{name}$"])[1]:
            return False
        time.sleep(interval)
    return False


def _launch_local_worker(
    entry: dict,
    tag: str,
    gpu: str,
    kit_env_path: Path,
    network: str | None = None,
    add_hosts: list[tuple[str, str]] | None = None,
) -> bool:
    """Launch the worker container on this machine; the secret never prints."""
    name = f"studio-{entry['profile']}"
    if run_quiet(["docker", "version"])[0] != 0:
        error("Docker is not available.")
        return False
    if run_quiet(["docker", "ps", "-aq", "--filter", f"name=^{name}$"])[1]:
        warn(f"Container '{name}' already exists.")
        print(f"  {_dim('Start it:')} {_bold(f'docker start {name}')}")
        print(f"  {_dim('Replace it:')} {_bold(f'docker rm -f {name}')} then rerun the worker kit")
        return False
    image_ref = _ensure_worker_image(entry["image"], tag)
    if image_ref is None:
        return False
    argv = _docker_run_argv(
        entry, image_ref, gpu, str(kit_env_path), network=network, add_hosts=add_hosts
    )
    info(f"Launching {name} ({image_ref}) ...")
    if run(argv, check=False).returncode != 0:
        error("docker run failed.")
        return False
    ok(f"{name} started; the shared secret stayed in {kit_env_path} (never shown).")
    info("Waiting for the worker to register with the API ...")
    if _wait_worker_registered(name):
        ok("Worker registered: Studio UI, Settings, Workers.")
    else:
        warn("Worker has not registered yet (model downloads can delay startup).")
        print(f"  {_dim('Watch it:')} {_bold(f'docker logs -f {name}')}")
    return True


def _native_kit_lines(worker_type: str, dist_version: str, env_file_ref: str) -> list[str]:
    """Paste-ready native (pip) worker setup; pure so tests can pin the contract."""
    extra = PIP_EXTRAS.get(worker_type)
    spec = f"studio-workers[{extra}]=={dist_version}" if extra else f"studio-workers=={dist_version}"
    engine = extra or worker_type
    return [
        "python3 -m venv studio-worker",
        f'studio-worker/bin/pip install "{spec}"',
        f"studio-worker/bin/studio-workers doctor --engine {engine}",
        f"set -a; . {env_file_ref}; set +a",
        f"studio-worker/bin/studio-workers run --type {worker_type}",
    ]


def _connectivity_notes(api_url: str, env_data: dict) -> list[str]:
    """Reachability verdict for the chosen API URL, from the operator's .env."""
    split_api = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").rstrip("/")
    public = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/")
    ip_mode = env_data.get("CONSOLE_IP_RESTRICT_MODE", "none")
    access_app = env_data.get("CLOUDFLARE_ACCESS_APP_ID", "")

    cap_note = [
        "This URL goes through the Cloudflare tunnel, which caps uploads at 100 MB;",
        "video renders exceed it. Workers on this machine or your LAN should use",
        "the direct http URL instead.",
    ]
    if split_api and api_url == split_api:
        if ip_mode == "both":
            return [
                "Your API hostname has an IP allowlist (CONSOLE_IP_RESTRICT_MODE=both).",
                "Workers polling from elsewhere will be blocked. Either add the worker's",
                "IP (Cloudflare menu, Update IP rules) or switch the mode to 'ui'.",
            ] + cap_note
        return [
            "Split API hostname: reachable by workers. No Cloudflare changes needed."
        ] + cap_note
    if public.startswith("https://") and api_url == public and access_app:
        return [
            "This hostname sits behind Cloudflare Access, which blocks worker polling.",
            "Use split hostnames (Setup wizard, network section) so the API gets its",
            "own hostname without an Access login.",
        ]
    if public.startswith("https://") and api_url == public:
        return cap_note
    if api_url.startswith("http://api:"):
        return ["Stack-internal URL: the worker joins the stack's docker network."]
    if "host.docker.internal" in api_url or "127.0.0.1" in api_url:
        return ["Local-only URL: works for a worker on this same machine."]
    if api_url.startswith("http://"):
        return ["Plain HTTP: fine for a LAN, use HTTPS for workers on the internet."]
    return ["Reachable as long as the worker machine can hit this URL over HTTPS."]


def _print_secret_handoff(kit_env_path: Path, env_file_ref: str, placement: str) -> None:
    """Env-file handoff: where the secret lives and how it reaches the worker."""
    info(f"Worker env written to {kit_env_path} (chmod 600, holds the shared secret).")
    if placement != PLACEMENT_LOCAL:
        print()
        print(f"  {_bold('Copy the env file to the worker machine first:')}")
        print()
        print(f"    scp {kit_env_path} <user>@<worker-host>:{env_file_ref}")


def _print_native_kit(
    entry: dict,
    tag: str,
    kit_env_path: Path,
    env_file_ref: str,
    placement: str,
    api_url: str,
    env_data: dict,
) -> None:
    """Print the native (pip) kit: disclosure, install + doctor + run, notes."""
    dist_version = _workers_dist_version(tag)
    if dist_version is None:
        warn(
            f"Could not read the studio-workers version from the api image (tag {tag})."
        )
        warn("The Studio release notes name it; replace the placeholder below.")
        dist_version = "<studio-workers-version>"

    lines = _native_kit_lines(entry["worker_type"], dist_version, env_file_ref)

    print()
    print(f"  {_bold('This will download, from their own upstreams:')}")
    print("    studio-workers from PyPI (Studio's worker runtime), plus per-engine")
    print("    third-party packages (torch, Chatterbox TTS, whisper, ComfyUI client),")
    print("    each under its own license; model weights download on first run.")
    if entry["worker_type"] == "video":
        print()
        print(f"  {_bold('Prerequisite: ffmpeg with libass')}")
        print("    Subtitle burn needs an ffmpeg build with the libass 'ass' filter.")
        print("    Check on the worker machine: ffmpeg -filters | grep ass")
        print("    macOS (core Homebrew ffmpeg no longer includes libass):")
        print("      brew trust homebrew-ffmpeg/ffmpeg")
        print("      brew install homebrew-ffmpeg/ffmpeg/ffmpeg")
        print("    doctor below verifies this and fails if the filter is missing.")
    _print_secret_handoff(kit_env_path, env_file_ref, placement)
    print()
    print(f"  {_bold('Run on the worker machine:')}")
    print()
    for line in lines:
        print(f"    {line}")
    print()
    print(f"  {_bold('Connectivity:')}")
    for note in _connectivity_notes(api_url, env_data) + _measured_notes(api_url):
        print(f"    {note}")
    print()
    info("doctor fails when torch cannot see the GPU; fix the install before running.")
    ok("Once started, the worker registers itself: Studio UI, Settings, Workers.")
    print(
        f"  {_dim('The API refuses a worker whose studio-workers version differs from its own.')}"
    )
    print()


def cmd_worker_kit(context: str, env_file: Path) -> None:
    """Interactive, topology-first: placement decides everything derivable.
    This machine launches the worker itself; a command prints only when a
    human is genuinely the transport (native shell, LAN, remote host)."""
    if not env_file.exists():
        warn("No .env found. Run setup first.")
        return
    env_data = read_env(env_file)
    secret = env_data.get("SHS_WORKER_SHARED_SECRET", "")
    if not secret:
        warn("SHS_WORKER_SHARED_SECRET is not set. Run setup first.")
        return
    tag = env_data.get("SHS_STUDIO_VERSION", "latest")

    heading("Worker kit")
    placement = [PLACEMENT_LOCAL, PLACEMENT_LAN, PLACEMENT_REMOTE][
        _interactive_single(
            "Where will this worker run?",
            [
                "This machine",
                "Another machine on my network",
                "A remote host (cloud or rented GPU)",
            ],
            default=0,
        )
    ]

    labels = [
        w["component"] + (f"  {_dim(w['gpu'])}" if w["gpu"] else "")
        for w in WORKER_CATALOG
    ]
    entry = WORKER_CATALOG[_interactive_single("Which worker?", labels, default=2)]

    # Host-side arch decides the default kit: Docker on a Mac cannot reach the
    # GPU (and audio has no arm64 image), so Apple Silicon defaults GPU-bound
    # workers to the native pip install. Only applies to this-machine workers.
    native = False
    if placement == PLACEMENT_LOCAL and _is_apple_silicon():
        gpu_bound = entry["worker_type"] in GPU_BOUND_TYPES
        if gpu_bound:
            warn(
                "GPU workers do not accelerate in Docker on Apple Silicon: the Mac GPU"
            )
            warn(
                "(MPS) is not passed into Linux containers. Run natively, or offload"
            )
            warn("to a CUDA host (Docker kit).")
        native = (
            _interactive_single(
                "Which kit?",
                [
                    f"Native (pip install, uses the Mac GPU){'' if gpu_bound else '  ' + _dim('no GPU benefit for this worker')}",
                    "Docker (container, CPU-only on this Mac)",
                ],
                default=0 if gpu_bound else 1,
            )
            == 0
        )

    # API target: derived by topology (URL plus the transport that makes it
    # resolve: compose network, host-gateway, LAN mapping, tunnel DNS). A
    # human types a URL only when nothing is derivable.
    lan_ip = _pick_lan_ip(env_data) if placement == PLACEMENT_LAN else None
    api_url, network, add_hosts, target_notes = _worker_api_target(
        env_data, placement, native, lan_ip=lan_ip
    )
    if api_url:
        info(f"API URL (derived): {api_url}")
    else:
        warn("No public HTTPS hostname is configured; a remote worker cannot reach")
        warn("a LAN URL. Set up split hostnames (Setup wizard, network section).")
        api_url = _prompt(
            "API URL the worker will use",
            _default_api_url(env_data, placement, native, _lan_ip()),
        ).rstrip("/")
    for note in target_notes:
        warn(note)

    # Per-type extras
    gpu = ""
    extra_env: list[tuple[str, str]] = []
    if entry["worker_type"] == "audio":
        if not native:
            gpu = _prompt("GPU ('all', a CUDA device id, or blank = CPU)", "").strip()
            extra_env.append(("HF_HOME", "/workspace/models/huggingface"))
    elif entry["worker_type"] == "video":
        extra_env.append(
            ("SHS_WHISPER_MODEL", env_data.get("SHS_WHISPER_MODEL", "base"))
        )
    elif entry["worker_type"] == "comfyui-image":
        comfy = _pick_comfyui_url(env_data, native)
        extra_env.append(("SHS_COMFYUI_URL", comfy))

    # Ordered queue list passthrough: the wheel/image derives the default
    # from the worker type; an operator override in .env reaches the worker.
    if env_data.get("SHS_WORKER_QUEUES"):
        extra_env.append(("SHS_WORKER_QUEUES", env_data["SHS_WORKER_QUEUES"]))

    public_base = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/") or api_url

    kit_env_name = f"studio-worker-{entry['worker_type']}.env"
    kit_env_path = env_file.parent / kit_env_name
    if placement == PLACEMENT_LOCAL:
        try:
            env_file_ref = f"~/{kit_env_path.relative_to(Path.home())}"
        except ValueError:
            env_file_ref = str(kit_env_path)
    else:
        env_file_ref = f"./{kit_env_name}"

    if native:
        workspace = _prompt(
            "Workspace directory on the worker machine", "~/studio-workspace"
        ).rstrip("/")
    else:
        workspace = "/workspace"

    run_env = [
        ("SHS_API_BASE_URL", api_url),
        ("SHS_PUBLIC_BASE_URL", public_base),
        ("SHS_WORKER_SHARED_SECRET", secret),
        ("SHS_WORKER_TYPE", entry["worker_type"]),
        ("SHS_WORKSPACE_ROOT", workspace),
        *extra_env,
    ]
    _write_kit_env(kit_env_path, run_env)

    if native:
        _print_native_kit(
            entry, tag, kit_env_path, env_file_ref, placement, api_url, env_data
        )
        return

    if placement == PLACEMENT_LOCAL:
        # This machine: the console is the transport, so it launches the
        # worker itself; nothing to paste, secret never on screen.
        if _launch_local_worker(
            entry, tag, gpu, kit_env_path, network=network, add_hosts=add_hosts
        ):
            print()
            print(f"  {_bold('Connectivity:')}")
            for note in _connectivity_notes(api_url, env_data) + _measured_notes(api_url):
                print(f"    {note}")
            print()
        return

    lines = _docker_kit_lines(
        entry, tag, gpu, env_file_ref, network=network, add_hosts=add_hosts
    )

    _print_secret_handoff(kit_env_path, env_file_ref, placement)
    print()
    print(f"  {_bold('Run on the worker machine:')}")
    print()
    for line in lines:
        print(f"    {line}")
    print()
    print(f"  {_bold('Connectivity:')}")
    for note in _connectivity_notes(api_url, env_data) + _measured_notes(api_url):
        print(f"    {note}")
    print()
    ok("Once started, the worker registers itself: Studio UI, Settings, Workers.")
    if placement == PLACEMENT_REMOTE:
        print(
            f"  {_dim('Rented GPU host guide: docs/vps-runpod.md in the studio-console repo.')}"
        )
    print()
