# tests/test_ingress_target.py
"""_ingress_target converges every shape on the nginx front door.

Pure-function tests (no Docker/Cloudflare). Run with `python -m pytest tests/`
or standalone via `python tests/test_ingress_target.py`.

Regression guard: core/full previously returned localhost:8000/:3000, bypassing
nginx, so a token-tunnel reused across shapes left stale ingress → 502. All
shapes must now target nginx (service name in split, localhost in core/full).
"""

from __future__ import annotations

from pathlib import Path

from studio_console.cloudflare.cf_wizard import _ingress_target


def _write_env(tmp_path: Path, shape: str, nginx_port: str = "80") -> Path:
    env = tmp_path / ".env"
    env.write_text(
        f"SHS_DEPLOYMENT_SHAPE={shape}\n"
        f"SHS_NGINX_PORT={nginx_port}\n"
        "CONSOLE_PUBLIC_API_BASE_URL=https://api.example.com\n"
    )
    return env


def test_split_targets_nginx_service(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "split")
    assert _ingress_target(env, "app.example.com") == "http://nginx:80"


def test_core_targets_localhost_nginx(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "core")
    # Even the api hostname routes through nginx, not :8000.
    assert _ingress_target(env, "api.example.com") == "http://localhost:80"
    assert _ingress_target(env, "app.example.com") == "http://localhost:80"


def test_full_targets_localhost_nginx(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "full")
    assert _ingress_target(env, "api.example.com") == "http://localhost:80"


def test_custom_nginx_port_honored(tmp_path: Path) -> None:
    assert _ingress_target(_write_env(tmp_path, "split", "8080"), "x") == "http://nginx:8080"
    assert _ingress_target(_write_env(tmp_path, "full", "8080"), "x") == "http://localhost:8080"


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    fn(Path(d))
                    print(f"ok   {name}")
                except AssertionError as e:
                    failures += 1
                    print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
