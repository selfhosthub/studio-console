# tests/test_ingress_mismatch.py
"""ingress_shape_mismatch refuses tunnel reuse across shapes.

Pure-function tests (no Docker/Cloudflare). Run with `make test`.

A cf-reuse launch skips the wizard, so the tunnel keeps whatever ingress the
last wizard run wrote. A wrong pairing 502s only on the public hostname, so
the launch must refuse up front. Re-syncing ingress on launch is rejected: a
boot on one machine would silently steal the tunnel from another.
"""

from __future__ import annotations

from pathlib import Path

from studio_console.cloudflare.cf_wizard import (
    INGRESS_ORIGIN_KEY,
    _push_tunnel_ingress,
    expected_ingress_origin,
    ingress_shape_mismatch,
)
from studio_console.env import read_env


def _env(origin: str = "", token: str = "tok", port: str = "") -> dict:
    env: dict[str, str] = {}
    if origin:
        env[INGRESS_ORIGIN_KEY] = origin
    if token:
        env["CLOUDFLARE_TUNNEL_TOKEN"] = token
    if port:
        env["SHS_NGINX_PORT"] = port
    return env


def test_expected_origin_per_shape() -> None:
    assert expected_ingress_origin("split") == "http://nginx:80"
    assert expected_ingress_origin("core") == "http://localhost:80"
    assert expected_ingress_origin("full", "8080") == "http://localhost:8080"


def test_match_passes_split() -> None:
    assert ingress_shape_mismatch("split", _env("http://nginx:80")) is None


def test_match_passes_full() -> None:
    assert ingress_shape_mismatch("full", _env("http://localhost:80")) is None


def test_mismatch_refuses_full_on_split_tunnel() -> None:
    msg = ingress_shape_mismatch("full", _env("http://nginx:80"))
    assert msg is not None
    assert "http://nginx:80" in msg
    assert "split" in msg
    assert "full" in msg
    assert "http://localhost:80" in msg
    assert "Rerun the Cloudflare wizard" in msg


def test_mismatch_refuses_split_on_full_tunnel() -> None:
    msg = ingress_shape_mismatch("split", _env("http://localhost:80"))
    assert msg is not None
    assert "core/full" in msg
    assert "split" in msg


def test_no_evidence_passes() -> None:
    assert ingress_shape_mismatch("full", _env(origin="")) is None


def test_no_token_passes() -> None:
    assert ingress_shape_mismatch("full", _env("http://nginx:80", token="")) is None


def test_core_full_share_origin() -> None:
    # Core and full both front with in-container nginx, so either may reuse.
    assert ingress_shape_mismatch("core", _env("http://localhost:80")) is None
    assert ingress_shape_mismatch("full", _env("http://localhost:80")) is None


def test_custom_port_match_passes() -> None:
    assert ingress_shape_mismatch("split", _env("http://nginx:8080", port="8080")) is None


def test_port_drift_refuses() -> None:
    msg = ingress_shape_mismatch("split", _env("http://nginx:8080", port="80"))
    assert msg is not None
    assert "http://nginx:8080" in msg


class _FakeCF:
    def put_tunnel_config(self, tunnel_id: str, ingress: list[dict]) -> None:
        pass


class _FailingCF:
    def put_tunnel_config(self, tunnel_id: str, ingress: list[dict]) -> None:
        from studio_console.cloudflare.cf_api import CloudflareError

        raise CloudflareError("boom")


def _write_env(tmp_path: Path, shape: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(
        f"SHS_DEPLOYMENT_SHAPE={shape}\n"
        "SHS_NGINX_PORT=80\n"
        "SHS_PUBLIC_BASE_URL=https://app.example.com\n"
    )
    return env


def test_push_records_origin(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "full")
    _push_tunnel_ingress(_FakeCF(), env, "tid")
    assert read_env(env)[INGRESS_ORIGIN_KEY] == "http://localhost:80"


def test_failed_push_records_nothing(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "split")
    _push_tunnel_ingress(_FailingCF(), env, "tid")
    assert INGRESS_ORIGIN_KEY not in read_env(env)
