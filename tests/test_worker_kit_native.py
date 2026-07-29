# tests/test_worker_kit_native.py
"""Pure-function tests (no Docker) for the native worker kit. Pins the contract:
arch normalization, pip-extra mapping, the generated install/doctor/run lines,
and the api-image version probe."""

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


class TestNativeKitLines:
    def _lines(self, worker_type, extra_env=None):
        return kit._native_kit_lines(
            worker_type,
            "1.3.0",
            "https://api.example.com",
            "https://studio.example.com",
            "s3cret",
            "~/studio-workspace",
            extra_env or [],
        )

    def test_audio_pins_extra_and_version(self):
        lines = self._lines("audio")
        assert 'studio-worker/bin/pip install "studio-workers[audio]==1.3.0"' in lines
        assert "studio-worker/bin/studio-workers doctor --engine audio" in lines
        assert lines[-1] == "studio-worker/bin/studio-workers run --type audio"

    def test_general_uses_base_dist(self):
        lines = self._lines("general")
        assert 'studio-worker/bin/pip install "studio-workers==1.3.0"' in lines
        assert "studio-worker/bin/studio-workers doctor --engine general" in lines

    def test_comfyui_maps_to_comfyui_extra_and_keeps_env(self):
        lines = self._lines(
            "comfyui-image", extra_env=[("SHS_COMFYUI_URL", "http://gpu:8188")]
        )
        assert (
            'studio-worker/bin/pip install "studio-workers[comfyui]==1.3.0"' in lines
        )
        assert "SHS_COMFYUI_URL=http://gpu:8188 \\" in lines
        assert lines[-1] == "studio-worker/bin/studio-workers run --type comfyui-image"

    def test_run_env_is_complete_and_index_free(self):
        lines = self._lines("audio")
        joined = "\n".join(lines)
        for var in (
            "SHS_API_BASE_URL=https://api.example.com",
            "SHS_PUBLIC_BASE_URL=https://studio.example.com",
            "SHS_WORKER_SHARED_SECRET=s3cret",
            "SHS_WORKSPACE_ROOT=~/studio-workspace",
        ):
            assert var in joined
        assert "--index-url" not in joined


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
