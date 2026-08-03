# tests/test_worker_kit_topology.py
"""Topology-first worker kit: derivation, probes, and the local launch argv.

Pure functions with injected probers, no Docker, no network, no TTY.
Run with `make test`.
"""

from __future__ import annotations

import shlex

from studio_console import commands_kit as kit
from studio_console.constants import WORKER_CATALOG

AUDIO = next(w for w in WORKER_CATALOG if w["worker_type"] == "audio")


class TestDockerRunArgv:
    def test_argv_matches_printed_kit_lines(self):
        """The console must exec exactly what the printed kit says."""
        lines = kit._docker_kit_lines(AUDIO, "1.2.10", "all", "./w.env")
        printed = shlex.split(" ".join(line.rstrip(" \\") for line in lines))
        argv = kit._docker_run_argv(
            AUDIO, f"ghcr.io/selfhosthub/{AUDIO['image']}:1.2.10", "all", "./w.env"
        )
        assert argv == printed

    def test_argv_matches_with_device_gpu_and_no_gpu(self):
        for gpu in ("1", ""):
            lines = kit._docker_kit_lines(AUDIO, "latest", gpu, "./w.env")
            printed = shlex.split(" ".join(line.rstrip(" \\") for line in lines))
            argv = kit._docker_run_argv(
                AUDIO, f"ghcr.io/selfhosthub/{AUDIO['image']}:latest", gpu, "./w.env"
            )
            assert argv == printed


class TestMeasuredNotes:
    def test_healthy(self):
        notes = kit._measured_notes("http://127.0.0.1:80", prober=lambda u: 200)
        assert notes == ["Measured from this machine: http://127.0.0.1:80/health answers 200."]

    def test_host_docker_internal_probed_as_loopback(self):
        seen = []

        def prober(url):
            seen.append(url)
            return 200

        kit._measured_notes("http://host.docker.internal:80", prober=prober)
        assert seen == ["http://127.0.0.1:80/health"]

    def test_no_answer_is_a_view_not_a_verdict(self):
        notes = kit._measured_notes("http://10.0.0.9:80", prober=lambda u: 0)
        assert "did not answer" in notes[0]
        assert "view from here" in notes[1]


class TestComfyuiCandidates:
    ENV = {"SHS_COMFYUI_URL": "http://gpu-box:8188"}

    def test_existing_url_is_first_candidate(self, monkeypatch):
        probed = []
        monkeypatch.setattr(kit, "_lan_ip", lambda: "192.168.1.5")
        monkeypatch.setattr(
            kit, "_interactive_single", lambda q, items, default=0: default
        )
        url = kit._pick_comfyui_url(self.ENV, native=False, prober=lambda u: probed.append(u) or 200)
        assert url == "http://gpu-box:8188"
        assert probed[0] == "http://gpu-box:8188/system_stats"

    def test_first_reachable_is_default(self, monkeypatch):
        monkeypatch.setattr(kit, "_lan_ip", lambda: None)
        chosen = {}

        def fake_single(q, items, default=0):
            chosen["default"] = default
            return default

        monkeypatch.setattr(kit, "_interactive_single", fake_single)
        # existing unreachable, host.docker.internal reachable
        def prober(url):
            return 200 if "127.0.0.1" in url else 0

        url = kit._pick_comfyui_url(self.ENV, native=False, prober=prober)
        assert chosen["default"] == 1
        assert url == "http://host.docker.internal:8188"


class TestLanPick:
    def test_candidates_probed_and_picked(self, monkeypatch):
        monkeypatch.setattr(kit, "_iface_addrs", lambda: ["192.168.1.10"])
        monkeypatch.setattr(kit.socket, "gethostname", lambda: "studio-box")
        monkeypatch.setattr(
            kit, "_interactive_single", lambda q, items, default=0: 0
        )
        ip = kit._pick_lan_ip({"SHS_NGINX_PORT": "80"}, prober=lambda u: 200)
        assert ip == "192.168.1.10"


class TestWorkerApiTarget:
    ENV = {"SHS_NGINX_PORT": "80", "SHS_API_HOSTNAME": "api.example.com"}

    def test_local_native_uses_published_api_port(self):
        url, network, hosts, notes = kit._worker_api_target(self.ENV, "local", True)
        assert (url, network, hosts, notes) == ("http://127.0.0.1:8000", None, [], [])

    def test_local_docker_joins_compose_network(self, monkeypatch):
        monkeypatch.setattr(kit, "_compose_network", lambda env: "studio_prod-network")
        url, network, hosts, notes = kit._worker_api_target(self.ENV, "local", False)
        assert url == "http://api:8000"
        assert network == "studio_prod-network"
        assert hosts == [] and notes == []

    def test_local_docker_falls_back_to_api_hostname(self, monkeypatch):
        monkeypatch.setattr(kit, "_compose_network", lambda env: None)
        url, network, hosts, notes = kit._worker_api_target(self.ENV, "local", False)
        assert url == "http://api.example.com:80"
        assert hosts == [("api.example.com", "host-gateway")]
        assert network is None and notes == []

    def test_local_docker_without_hostname_warns_front_door(self, monkeypatch):
        monkeypatch.setattr(kit, "_compose_network", lambda env: None)
        url, network, hosts, notes = kit._worker_api_target(
            {"SHS_NGINX_PORT": "80"}, "local", False
        )
        assert url == "http://host.docker.internal:80"
        assert any("404s worker job claims" in n for n in notes)

    def test_lan_docker_maps_hostname_to_picked_ip(self):
        url, network, hosts, notes = kit._worker_api_target(
            self.ENV, "lan", False, lan_ip="192.168.1.10"
        )
        assert url == "http://api.example.com:80"
        assert hosts == [("api.example.com", "192.168.1.10")]

    def test_lan_native_gets_etc_hosts_instruction(self):
        url, network, hosts, notes = kit._worker_api_target(
            self.ENV, "lan", True, lan_ip="192.168.1.10"
        )
        assert url == "http://api.example.com:80"
        assert hosts == []
        assert notes == ["On the worker machine, add to /etc/hosts: 192.168.1.10 api.example.com"]

    def test_remote_uses_tunnel(self):
        env = dict(self.ENV, CONSOLE_PUBLIC_API_BASE_URL="https://api.example.com")
        url, network, hosts, notes = kit._worker_api_target(env, "remote", False)
        assert url == "https://api.example.com"


class TestDockerTransportFlags:
    def test_network_and_add_host_in_both_forms(self):
        hosts = [("api.example.com", "192.168.1.10")]
        lines = kit._docker_kit_lines(AUDIO, "1.2.10", "", "./w.env", add_hosts=hosts)
        printed = shlex.split(" ".join(line.rstrip(" \\") for line in lines))
        argv = kit._docker_run_argv(
            AUDIO, f"ghcr.io/selfhosthub/{AUDIO['image']}:1.2.10", "", "./w.env",
            add_hosts=hosts,
        )
        assert argv == printed
        assert "api.example.com:192.168.1.10" in argv
        net_argv = kit._docker_run_argv(
            AUDIO, "img:1", "", "./w.env", network="studio_prod-network"
        )
        assert net_argv[net_argv.index("--network") + 1] == "studio_prod-network"
