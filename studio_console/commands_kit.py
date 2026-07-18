# studio_console/commands_kit.py
"""Worker kit - print paste-ready setup for a worker on this or another machine."""

from __future__ import annotations

from pathlib import Path

from .constants import WORKER_CATALOG
from .env import read_env
from .tui import _bold, _dim, _interactive_single, _prompt, heading, ok, warn


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

    api_url = _prompt(
        "API URL the worker will use", _default_api_url(env_data)
    ).rstrip("/")

    # Per-type extras
    gpu = ""
    extra_env: list[tuple[str, str]] = []
    if entry["worker_type"] == "audio":
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
