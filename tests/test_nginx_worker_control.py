# tests/test_nginx_worker_control.py
"""Every front-door nginx conf source 404s the worker control prefix:
nginx/studio.conf.template, _NGINX_CONF_TEMPLATE, and the split renderer's
UI server block. The split api-hostname server block routes it."""

from __future__ import annotations

from pathlib import Path

from studio_console.wizard import _NGINX_CONF_TEMPLATE, _render_split_nginx_conf

BLOCK = "location ^~ /api/v1/internal/"
REPO_TEMPLATE = Path(__file__).parent.parent / "nginx" / "studio.conf.template"


def test_file_template_blocks_worker_control():
    assert BLOCK in REPO_TEMPLATE.read_text()


def test_inline_template_blocks_worker_control():
    assert BLOCK in _NGINX_CONF_TEMPLATE


def test_split_ui_host_blocks_worker_control():
    conf = _render_split_nginx_conf("    server api:8000;", "    server ui:3000;", "app.example.com", "api.example.com")
    ui_block = conf.split("server_name app.example.com;")[1].split("server {")[0]
    assert BLOCK in ui_block


def test_split_api_host_routes_worker_control():
    conf = _render_split_nginx_conf("    server api:8000;", "    server ui:3000;", "app.example.com", "api.example.com")
    api_block = conf.split("server_name api.example.com;")[1]
    assert BLOCK not in api_block
