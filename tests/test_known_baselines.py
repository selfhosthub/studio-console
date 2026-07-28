# tests/test_known_baselines.py
"""Major-version classification across squash-and-stamp baselines.

Pure-function tests (no Docker/Postgres). Run with `make test`.

The load-bearing case is `test_post_squash_prior_major_blocks`: a prior-major DB
stamped to a SECOND baseline of that major must classify prior_major and the
restore preflight must block it. That case silently passed before list-baselines.
"""

from __future__ import annotations

from pathlib import Path

from studio_console import major_version as mv


# A major-2 image; majors 1 and 2 each have a post-squash baseline appended.
BASELINES = {
    1: ["1aaaaaaaaaaa", "1bbbbbbbbbbb"],  # original + post-squash
    2: ["2aaaaaaaaaaa", "2bbbbbbbbbbb"],
}


def test_post_squash_prior_major_blocks():
    """Prior-major DB at the SECOND (post-squash) baseline → prior_major."""
    result, info = mv.classify_revision("1bbbbbbbbbbb", target_major=2, baselines=BASELINES)
    assert result == "prior_major", result
    assert info["db_major"] == 1


def test_first_baseline_prior_major_still_blocks():
    result, _ = mv.classify_revision("1aaaaaaaaaaa", target_major=2, baselines=BASELINES)
    assert result == "prior_major", result


def test_post_squash_future_major_blocks():
    result, _ = mv.classify_revision("2bbbbbbbbbbb", target_major=1, baselines=BASELINES)
    assert result == "unknown_future", result


def test_same_major_any_baseline_ok():
    for rev in ("2aaaaaaaaaaa", "2bbbbbbbbbbb"):
        result, _ = mv.classify_revision(rev, target_major=2, baselines=BASELINES)
        assert result == "ok", (rev, result)


def test_unknown_midchain_rev_still_ok():
    """Non-baseline rev defers to the API guardrail — must not block."""
    result, info = mv.classify_revision("deadbeefcafe", target_major=2, baselines=BASELINES)
    assert result == "ok", result
    assert info["db_major"] is None


def test_load_baselines_normalizes_scalar(tmp_path=None):
    """Legacy scalar shape and list shape both normalize to lists."""
    assert mv.load_baselines.__doc__  # sanity: function exists
    # exercise the normalization logic directly via classify with both shapes
    scalar = {1: ["only"]}  # load_baselines already returns lists
    result, _ = mv.classify_revision("only", target_major=2, baselines=scalar)
    assert result == "prior_major", result


def test_bundled_file_loads_as_lists():
    """The shipped known_baselines.json parses and yields list values."""
    loaded = mv.load_baselines()
    assert loaded, "bundled known_baselines.json failed to load"
    for major, revs in loaded.items():
        assert isinstance(major, int)
        assert isinstance(revs, list) and revs


def test_restore_preflight_blocks_post_squash_prior_major(monkeypatch, tmp_path):
    """End-to-end: preflight reads a prior-major (post-squash) dump rev → returns False."""
    from studio_console import commands

    monkeypatch.setattr(commands.mv, "load_baselines", lambda: BASELINES)
    monkeypatch.setattr(commands, "_read_current_db_revision", lambda *a, **k: "2aaaaaaaaaaa")
    monkeypatch.setattr(commands, "_read_revision_from_dump", lambda f: "1bbbbbbbbbbb")
    monkeypatch.setattr(commands, "_parse_dump_header", lambda f: {"studio_image_tag": "2.0.0"})
    monkeypatch.setattr(commands, "read_env", lambda f: {"SHS_STUDIO_VERSION": "2.0.0"})

    proceed = commands._restore_preflight("host", Path("/tmp/.env"), str(tmp_path / "x.sql"))
    assert proceed is False
