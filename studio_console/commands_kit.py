# studio_console/commands_kit.py
"""Worker kit - print paste-ready setup for a worker on this or another machine."""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

from .constants import WORKER_CATALOG
from .env import read_env
from .tui import _bold, _dim, _interactive_single, _prompt, heading, info, ok, warn

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
        host = "127.0.0.1" if native else "host.docker.internal"
        return f"http://{host}:{port}"
    if placement == PLACEMENT_LAN:
        return f"http://{lan_ip or socket.gethostname()}:{port}"
    return _tunnel_url(env_data) or f"http://{lan_ip or socket.gethostname()}:{port}"


def _kit_env_lines(run_env: list[tuple[str, str]]) -> list[str]:
    """KEY=value lines for the kit env file; pure so tests can pin the contract."""
    return [f"{k}={v}" for k, v in run_env]


def _write_kit_env(path: Path, run_env: list[tuple[str, str]]) -> None:
    """Write the kit env file with 0600 perms from creation."""
    content = "\n".join(_kit_env_lines(run_env)) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def _docker_kit_lines(entry: dict, tag: str, gpu: str, env_file_ref: str) -> list[str]:
    """Paste-ready docker run command; pure so tests can pin the contract.
    --add-host is always emitted: without it a Linux docker host cannot resolve
    host.docker.internal and fails late (worker registers, cannot reach ComfyUI)."""
    lines = [
        f"docker run -d --name studio-{entry['profile']} \\",
        "  --restart unless-stopped \\",
    ]
    if gpu:
        gpu_flag = "--gpus all" if gpu == "all" else f"--gpus 'device={gpu}'"
        lines.append(f"  {gpu_flag} \\")
    lines += [
        "  --add-host host.docker.internal:host-gateway \\",
        "  -v studio-workspace:/workspace \\",
        f"  --env-file {env_file_ref} \\",
        f"  ghcr.io/selfhosthub/{entry['image']}:{tag}",
    ]
    return lines


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
    for note in _connectivity_notes(api_url, env_data):
        print(f"    {note}")
    print()
    info("doctor fails when torch cannot see the GPU; fix the install before running.")
    ok("Once started, the worker registers itself: Studio UI, Settings, Workers.")
    print(
        f"  {_dim('The API refuses a worker whose studio-workers version differs from its own.')}"
    )
    print()


def cmd_worker_kit(context: str, env_file: Path) -> None:
    """Interactive: worker type + placement, derive the API URL, write the env
    file, print the paste-ready commands."""
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
    labels = [
        w["component"] + (f"  {_dim(w['gpu'])}" if w["gpu"] else "")
        for w in WORKER_CATALOG
    ]
    entry = WORKER_CATALOG[_interactive_single("Which worker?", labels, default=2)]

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

    default_url = _default_api_url(env_data, placement, native, _lan_ip())
    if placement == PLACEMENT_REMOTE and not _tunnel_url(env_data):
        warn("No public HTTPS hostname is configured; a remote worker cannot reach")
        warn("a LAN URL. Set up split hostnames (Setup wizard, network section).")
    api_url = _prompt("API URL the worker will use", default_url).rstrip("/")

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
        comfy = _prompt(
            "ComfyUI server URL, as reachable FROM THE WORKER",
            env_data.get("SHS_COMFYUI_URL", "http://127.0.0.1:8188"),
        )
        extra_env.append(("SHS_COMFYUI_URL", comfy))

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

    lines = _docker_kit_lines(entry, tag, gpu, env_file_ref)

    _print_secret_handoff(kit_env_path, env_file_ref, placement)
    print()
    print(f"  {_bold('Run on the worker machine:')}")
    print()
    for line in lines:
        print(f"    {line}")
    print()
    print(f"  {_bold('Connectivity:')}")
    for note in _connectivity_notes(api_url, env_data):
        print(f"    {note}")
    print()
    ok("Once started, the worker registers itself: Studio UI, Settings, Workers.")
    if placement == PLACEMENT_REMOTE:
        print(
            f"  {_dim('Rented GPU host guide: docs/vps-runpod.md in the studio-console repo.')}"
        )
    print()
