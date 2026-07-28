# tests/test_ingress_target.py
"""_ingress_target converges every shape on the nginx front door.

Pure-function tests (no Docker/Cloudflare). Run with `make test`.

Regression guard: core/full previously returned localhost:8000/:3000, bypassing
nginx, so a token-tunnel reused across shapes left stale ingress → 502. All
shapes must now target nginx (service name in split, localhost in core/full).
"""

from __future__ import annotations

from pathlib import Path

from studio_console.cloudflare.cf_wizard import (
    _api_base_url,
    _ingress_target,
    _push_tunnel_ingress,
)


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


def _write_env_raw(tmp_path: Path, body: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(body)
    return env


def test_api_base_url_console_var_wins(tmp_path: Path) -> None:
    env = _write_env_raw(
        tmp_path,
        "SHS_DEPLOYMENT_SHAPE=full\n"
        "CONSOLE_PUBLIC_API_BASE_URL=https://api.example.com\n"
        "SHS_PUBLIC_API_URL=https://other.example.com\n",
    )
    assert _api_base_url(env) == "https://api.example.com"


def test_api_base_url_core_full_falls_back_to_derived(tmp_path: Path) -> None:
    for shape in ("core", "full"):
        env = _write_env_raw(
            tmp_path,
            f"SHS_DEPLOYMENT_SHAPE={shape}\n"
            "SHS_PUBLIC_API_URL=https://api.example.com\n",
        )
        assert _api_base_url(env) == "https://api.example.com", shape


def test_api_base_url_split_ignores_derived(tmp_path: Path) -> None:
    # Split behavior unchanged: only CONSOLE_PUBLIC_API_BASE_URL counts.
    env = _write_env_raw(
        tmp_path,
        "SHS_DEPLOYMENT_SHAPE=split\n"
        "SHS_PUBLIC_API_URL=https://api.example.com\n",
    )
    assert _api_base_url(env) == ""


def test_api_base_url_rejects_non_https(tmp_path: Path) -> None:
    env = _write_env_raw(
        tmp_path,
        "SHS_DEPLOYMENT_SHAPE=full\n"
        "SHS_PUBLIC_API_URL=http://localhost:80\n",
    )
    assert _api_base_url(env) == ""


class _FakeCF:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    def put_tunnel_config(self, tunnel_id: str, ingress: list[dict]) -> None:
        self.calls.append((tunnel_id, ingress))


def test_full_ingress_includes_api_hostname(tmp_path: Path) -> None:
    env = _write_env_raw(
        tmp_path,
        "SHS_DEPLOYMENT_SHAPE=full\n"
        "SHS_NGINX_PORT=80\n"
        "SHS_PUBLIC_BASE_URL=https://app.example.com\n"
        "SHS_PUBLIC_API_URL=https://api.example.com\n",
    )
    cf = _FakeCF()
    _push_tunnel_ingress(cf, env, "tid")
    assert cf.calls[0][1] == [
        {"hostname": "app.example.com", "service": "http://localhost:80"},
        {"hostname": "api.example.com", "service": "http://localhost:80"},
    ]


def test_core_ingress_api_hostname_same_as_ui_not_duplicated(tmp_path: Path) -> None:
    env = _write_env_raw(
        tmp_path,
        "SHS_DEPLOYMENT_SHAPE=core\n"
        "SHS_NGINX_PORT=80\n"
        "SHS_PUBLIC_BASE_URL=https://app.example.com\n"
        "SHS_PUBLIC_API_URL=https://app.example.com\n",
    )
    cf = _FakeCF()
    _push_tunnel_ingress(cf, env, "tid")
    assert cf.calls[0][1] == [
        {"hostname": "app.example.com", "service": "http://localhost:80"},
    ]


def test_split_ingress_unchanged(tmp_path: Path) -> None:
    env = _write_env_raw(
        tmp_path,
        "SHS_DEPLOYMENT_SHAPE=split\n"
        "SHS_NGINX_PORT=80\n"
        "SHS_PUBLIC_BASE_URL=https://app.example.com\n"
        "CONSOLE_PUBLIC_API_BASE_URL=https://api.example.com\n",
    )
    cf = _FakeCF()
    _push_tunnel_ingress(cf, env, "tid")
    assert cf.calls[0][1] == [
        {"hostname": "app.example.com", "service": "http://nginx:80"},
        {"hostname": "api.example.com", "service": "http://nginx:80"},
    ]
