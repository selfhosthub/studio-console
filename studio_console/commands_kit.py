# studio_console/commands_kit.py
"""Worker kit - print paste-ready setup for a worker on this or another machine."""

from __future__ import annotations

import platform
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

VERSION_PROBE = (
    "from studio_workers.contracts.version import WORKERS_VERSION; print(WORKERS_VERSION)"
)


def _host_arch() -> str:
    """Normalized machine arch of the console host (arm64, x86_64, ...)."""
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and _host_arch() == "arm64"


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


def _native_kit_lines(
    worker_type: str,
    dist_version: str,
    api_url: str,
    public_base: str,
    secret: str,
    workspace: str,
    extra_env: list[tuple[str, str]],
) -> list[str]:
    """Paste-ready native (pip) worker setup; pure so tests can pin the contract."""
    extra = PIP_EXTRAS.get(worker_type)
    spec = f"studio-workers[{extra}]=={dist_version}" if extra else f"studio-workers=={dist_version}"
    engine = extra or worker_type
    run_env = [
        ("SHS_API_BASE_URL", api_url),
        ("SHS_PUBLIC_BASE_URL", public_base),
        ("SHS_WORKER_SHARED_SECRET", secret),
        ("SHS_WORKSPACE_ROOT", workspace),
        *extra_env,
    ]
    lines = [
        "python3 -m venv studio-worker",
        f'studio-worker/bin/pip install "{spec}"',
        f"studio-worker/bin/studio-workers doctor --engine {engine}",
    ]
    lines += [f"{k}={v} \\" for k, v in run_env]
    lines.append(f"studio-worker/bin/studio-workers run --type {worker_type}")
    return lines


def _default_api_url(env_data: dict) -> str:
    """Best API URL for a worker: split API hostname > public domain > local port."""
    split_api = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").rstrip("/")
    if split_api.startswith("https://"):
        return split_api
    public = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/")
    if public.startswith("https://"):
        return public
    port = env_data.get("SHS_NGINX_PORT", "80")
    return f"http://host.docker.internal:{port}"


def _connectivity_notes(api_url: str, env_data: dict) -> list[str]:
    """Reachability verdict for the chosen API URL, from the operator's .env."""
    split_api = env_data.get("CONSOLE_PUBLIC_API_BASE_URL", "").rstrip("/")
    public = env_data.get("SHS_PUBLIC_BASE_URL", "").rstrip("/")
    ip_mode = env_data.get("CONSOLE_IP_RESTRICT_MODE", "none")
    access_app = env_data.get("CLOUDFLARE_ACCESS_APP_ID", "")

    if split_api and api_url == split_api:
        if ip_mode == "both":
            return [
                "Your API hostname has an IP allowlist (CONSOLE_IP_RESTRICT_MODE=both).",
                "Workers polling from elsewhere will be blocked. Either add the worker's",
                "IP (Cloudflare menu, Update IP rules) or switch the mode to 'ui'.",
            ]
        return ["Split API hostname: reachable by workers. No Cloudflare changes needed."]
    if public and api_url == public and access_app:
        return [
            "This hostname sits behind Cloudflare Access, which blocks worker polling.",
            "Use split hostnames (Setup wizard, network section) so the API gets its",
            "own hostname without an Access login.",
        ]
    if "host.docker.internal" in api_url:
        return ["Local-only URL: works for a worker container on this same machine."]
    if api_url.startswith("http://"):
        return ["Plain HTTP: fine for a LAN, use HTTPS for workers on the internet."]
    return ["Reachable as long as the worker machine can hit this URL over HTTPS."]


def _print_native_kit(
    entry: dict,
    tag: str,
    api_url: str,
    public_base: str,
    secret: str,
    extra_env: list[tuple[str, str]],
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

    workspace = _prompt(
        "Workspace directory on the worker machine", "~/studio-workspace"
    ).rstrip("/")

    lines = _native_kit_lines(
        entry["worker_type"],
        dist_version,
        api_url,
        public_base,
        secret,
        workspace,
        extra_env,
    )

    print()
    print(f"  {_bold('This will download, from their own upstreams:')}")
    print("    studio-workers from PyPI (Studio's worker runtime), plus per-engine")
    print("    third-party packages (torch, Chatterbox TTS, whisper, ComfyUI client),")
    print("    each under its own license; model weights download on first run.")
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
    warn("The command contains SHS_WORKER_SHARED_SECRET. Treat it as a secret.")
    info("doctor fails when torch cannot see the GPU; fix the install before running.")
    ok("Once started, the worker registers itself: Studio UI, Settings, Workers.")
    print(
        f"  {_dim('The API refuses a worker whose studio-workers version differs from its own.')}"
    )
    print()


def cmd_worker_kit(context: str, env_file: Path) -> None:
    """Interactive: pick a worker type, print docker run + RunPod values + notes."""
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

    # Host-side arch decides the default kit: Docker on a Mac cannot reach the
    # GPU (and audio has no arm64 image), so Apple Silicon defaults GPU-bound
    # workers to the native pip install. Elsewhere nothing changes.
    native = False
    if _is_apple_silicon():
        gpu_bound = entry["worker_type"] in GPU_BOUND_TYPES
        if gpu_bound:
            warn(
                "GPU workers do not accelerate in Docker on Apple Silicon: the Mac GPU"
            )
            warn(
                "(MPS) is not passed into Linux containers. Run natively, or offload"
            )
            warn("to a CUDA host / RunPod (Docker kit).")
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

    api_url = _prompt(
        "API URL the worker will use", _default_api_url(env_data)
    ).rstrip("/")

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

    if native:
        _print_native_kit(entry, tag, api_url, public_base, secret, extra_env, env_data)
        return

    lines = [
        f"docker run -d --name studio-{entry['profile']} \\",
        "  --restart unless-stopped \\",
    ]
    if gpu:
        gpu_flag = "--gpus all" if gpu == "all" else f"--gpus 'device={gpu}'"
        lines.append(f"  {gpu_flag} \\")
    if "host.docker.internal" in api_url:
        lines.append("  --add-host host.docker.internal:host-gateway \\")
    lines += [
        "  -v studio-workspace:/workspace \\",
        f"  -e SHS_API_BASE_URL={api_url} \\",
        f"  -e SHS_PUBLIC_BASE_URL={public_base} \\",
        f"  -e SHS_WORKER_SHARED_SECRET={secret} \\",
        f"  -e SHS_WORKER_TYPE={entry['worker_type']} \\",
        "  -e SHS_WORKSPACE_ROOT=/workspace \\",
    ]
    lines += [f"  -e {k}={v} \\" for k, v in extra_env]
    lines.append(f"  ghcr.io/selfhosthub/{entry['image']}:{tag}")

    print()
    print(f"  {_bold('Run on the worker machine:')}")
    print()
    for line in lines:
        print(f"    {line}")
    print()
    print(f"  {_bold('RunPod / Vast.ai template:')}")
    print(f"    Image:   ghcr.io/selfhosthub/{entry['image']}:{tag}")
    print("    Volume:  network volume mounted at /workspace (50+ GB, holds models)")
    print("    Ports:   none (workers are outbound-only pollers)")
    print("    Env:     the -e values above")
    print()
    print(f"  {_bold('Connectivity:')}")
    for note in _connectivity_notes(api_url, env_data):
        print(f"    {note}")
    print()
    warn("The command contains SHS_WORKER_SHARED_SECRET. Treat it as a secret.")
    ok("Once started, the worker registers itself: Studio UI, Settings, Workers.")
    print(f"  {_dim('Full guide: docs/vps-runpod.md in the studio-console repo.')}")
    print()
