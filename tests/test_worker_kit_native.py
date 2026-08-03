# tests/test_worker_kit_native.py
"""Pure-function tests (no Docker) for the worker kit. Pins the contract:
arch normalization, pip-extra mapping, placement-based API-URL defaults,
the env-file handoff, the generated command lines, and the version probe."""

from types import SimpleNamespace

from studio_console import commands_kit as kit
from studio_console.constants import WORKER_CATALOG


class TestHostArch:
    def test_normalizes_aarch64(self, monkeypatch):
        monkeypatch.setattr(kit.platform, "machine", lambda: "aarch64")
        assert kit._host_arch() == "arm64"

    def test_normalizes_amd64(self, monkeypatch):
        monkeypatch.setattr(kit.platform, "machine", lambda: "AMD64")
        assert kit._host_arch() == "x86_64"

    def test_passes_through_arm64(self, monkeypatch):
        monkeypatch.setattr(kit.platform, "machine", lambda: "arm64")
        assert kit._host_arch() == "arm64"

    def test_apple_silicon_requires_darwin_and_arm64(self, monkeypatch):
        monkeypatch.setattr(kit.platform, "machine", lambda: "arm64")
        monkeypatch.setattr(kit.sys, "platform", "darwin")
        assert kit._is_apple_silicon() is True
        monkeypatch.setattr(kit.sys, "platform", "linux")
        assert kit._is_apple_silicon() is False
        monkeypatch.setattr(kit.sys, "platform", "darwin")
        monkeypatch.setattr(kit.platform, "machine", lambda: "x86_64")
        assert kit._is_apple_silicon() is False


class TestPipExtras:
    def test_covers_every_catalog_worker_type(self):
        for entry in WORKER_CATALOG:
            assert entry["worker_type"] in kit.PIP_EXTRAS

    def test_gpu_bound_types_exist_in_catalog(self):
        types = {w["worker_type"] for w in WORKER_CATALOG}
        assert kit.GPU_BOUND_TYPES <= types


class TestDefaultApiUrl:
    ENV = {
        "SHS_NGINX_PORT": "8080",
        "CONSOLE_PUBLIC_API_BASE_URL": "https://api.example.com",
        "SHS_PUBLIC_BASE_URL": "https://studio.example.com",
    }

    def test_local_docker_is_direct(self):
        url = kit._default_api_url(self.ENV, kit.PLACEMENT_LOCAL)
        assert url == "http://host.docker.internal:8080"

    def test_local_native_is_published_api_port(self):
        """Native workers bypass nginx: the front door 404s /api/v1/internal/*."""
        url = kit._default_api_url(self.ENV, kit.PLACEMENT_LOCAL, native=True)
        assert url == "http://127.0.0.1:8000"

    def test_lan_is_direct_ip(self):
        url = kit._default_api_url(self.ENV, kit.PLACEMENT_LAN, lan_ip="192.168.1.5")
        assert url == "http://192.168.1.5:8080"

    def test_remote_prefers_split_hostname(self):
        url = kit._default_api_url(self.ENV, kit.PLACEMENT_REMOTE)
        assert url == "https://api.example.com"

    def test_remote_falls_back_to_public(self):
        env = dict(self.ENV, CONSOLE_PUBLIC_API_BASE_URL="")
        url = kit._default_api_url(env, kit.PLACEMENT_REMOTE)
        assert url == "https://studio.example.com"

    def test_tunnel_never_defaults_for_local_or_lan(self):
        for placement in (kit.PLACEMENT_LOCAL, kit.PLACEMENT_LAN):
            url = kit._default_api_url(self.ENV, placement, lan_ip="10.0.0.9")
            assert "example.com" not in url


class TestKitEnvLines:
    def test_key_value_lines(self):
        lines = kit._kit_env_lines(
            [("SHS_API_BASE_URL", "http://h:80"), ("SHS_WORKER_SHARED_SECRET", "s3cret")]
        )
        assert lines == [
            "SHS_API_BASE_URL=http://h:80",
            "SHS_WORKER_SHARED_SECRET=s3cret",
        ]

    def test_write_is_owner_only(self, tmp_path):
        path = tmp_path / "studio-worker-general.env"
        kit._write_kit_env(path, [("SHS_WORKER_SHARED_SECRET", "s3cret")])
        assert path.read_text() == "SHS_WORKER_SHARED_SECRET=s3cret\n"
        assert (path.stat().st_mode & 0o777) == 0o600


class TestDockerKitLines:
    ENTRY = {"profile": "worker-audio", "image": "studio-worker-audio"}

    def test_always_emits_add_host(self):
        lines = kit._docker_kit_lines(self.ENTRY, "1.3.0", "", "./w.env")
        assert "  --add-host host.docker.internal:host-gateway \\" in lines

    def test_references_env_file_and_never_secret(self):
        lines = kit._docker_kit_lines(self.ENTRY, "1.3.0", "", "./w.env")
        joined = "\n".join(lines)
        assert "  --env-file ./w.env \\" in lines
        assert "-e " not in joined
        assert lines[-1] == "  ghcr.io/selfhosthub/studio-worker-audio:1.3.0"

    def test_gpu_flag(self):
        lines = kit._docker_kit_lines(self.ENTRY, "1.3.0", "all", "./w.env")
        assert "  --gpus all \\" in lines
        lines = kit._docker_kit_lines(self.ENTRY, "1.3.0", "1", "./w.env")
        assert "  --gpus 'device=1' \\" in lines


class TestNativeKitLines:
    def _lines(self, worker_type):
        return kit._native_kit_lines(worker_type, "1.3.0", "./studio-worker.env")

    def test_audio_pins_extra_and_version(self):
        lines = self._lines("audio")
        assert 'studio-worker/bin/pip install "studio-workers[audio]==1.3.0"' in lines
        assert "studio-worker/bin/studio-workers doctor --engine audio" in lines
        assert lines[-1] == "studio-worker/bin/studio-workers run --type audio"

    def test_general_uses_base_dist(self):
        lines = self._lines("general")
        assert 'studio-worker/bin/pip install "studio-workers==1.3.0"' in lines
        assert "studio-worker/bin/studio-workers doctor --engine general" in lines

    def test_comfyui_maps_to_comfyui_extra(self):
        lines = self._lines("comfyui-image")
        assert (
            'studio-worker/bin/pip install "studio-workers[comfyui]==1.3.0"' in lines
        )
        assert lines[-1] == "studio-worker/bin/studio-workers run --type comfyui-image"

    def test_env_comes_from_file_not_argv(self):
        lines = self._lines("audio")
        assert "set -a; . ./studio-worker.env; set +a" in lines
        joined = "\n".join(lines)
        assert "SHS_WORKER_SHARED_SECRET" not in joined


class TestConnectivityNotes:
    ENV = {
        "CONSOLE_PUBLIC_API_BASE_URL": "https://api.example.com",
        "SHS_PUBLIC_BASE_URL": "https://studio.example.com",
    }

    def test_tunnel_url_warns_about_upload_cap(self):
        notes = kit._connectivity_notes("https://api.example.com", self.ENV)
        assert any("100 MB" in n for n in notes)

    def test_direct_url_has_no_cap_warning(self):
        notes = kit._connectivity_notes("http://host.docker.internal:80", self.ENV)
        assert not any("100 MB" in n for n in notes)
        notes = kit._connectivity_notes("http://192.168.1.5:80", self.ENV)
        assert not any("100 MB" in n for n in notes)


class TestWorkersDistVersion:
    @staticmethod
    def _runner(inspect_rc=0, probe_rc=0, probe_out="1.3.0\n"):
        def run(cmd, **kwargs):
            if cmd[:3] == ["docker", "image", "inspect"]:
                return SimpleNamespace(returncode=inspect_rc, stdout="", stderr="")
            return SimpleNamespace(returncode=probe_rc, stdout=probe_out, stderr="")

        return run

    def test_reads_version_from_image(self):
        assert kit._workers_dist_version("1.2.8", runner=self._runner()) == "1.3.0"

    def test_absent_image_returns_none(self):
        assert (
            kit._workers_dist_version("1.2.8", runner=self._runner(inspect_rc=1))
            is None
        )

    def test_probe_failure_returns_none(self):
        assert (
            kit._workers_dist_version("1.2.8", runner=self._runner(probe_rc=1)) is None
        )

    def test_empty_output_returns_none(self):
        assert (
            kit._workers_dist_version("1.2.8", runner=self._runner(probe_out=""))
            is None
        )
