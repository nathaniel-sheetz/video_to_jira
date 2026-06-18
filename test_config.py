"""
Tests for config.py — shared config + path resolution.

Run either way:
    python -m pytest test_config.py     # if pytest is installed
    python test_config.py                # standalone runner, no deps

Covers the precedence rules: --config beats --project beats the repo-root
default; load_config tolerates a missing file; and issues-path resolution
prefers an explicit *.json arg, then the config's issues_path, then a bare name.
"""

import json
import os
import tempfile

import config


def test_resolve_config_path_precedence():
    # --config wins outright.
    assert config.resolve_config_path(project="p", config="x.json") == "x.json"
    # --project is sugar for projects/<name>/config.json.
    assert config.resolve_config_path(project="2-news-20260610") == \
        os.path.join("projects", "2-news-20260610", "config.json")
    # Neither -> repo-root default.
    assert config.resolve_config_path() == config.CONFIG_FILE


def test_load_config_missing_returns_empty():
    cfg, path = config.load_config(config="does_not_exist_42.json")
    assert cfg == {}
    assert path == "does_not_exist_42.json"


def test_load_config_reads_file_and_reports_path():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"issues_path": "projects/x/issues.json"}, f)
        cfg, path = config.load_config(config=p)
        assert cfg["issues_path"] == "projects/x/issues.json"
        assert path == p


def test_load_config_project_resolves_under_projects():
    # A --project name maps to projects/<name>/config.json.
    _, path = config.load_config(project="some-session")
    assert path == os.path.join("projects", "some-session", "config.json")


def test_resolve_issues_path_precedence():
    cfg = {"issues_path": "projects/x/issues.json"}
    # Explicit *.json arg wins.
    assert config.resolve_issues_path("a/b.json", cfg) == "a/b.json"
    # Else the config's issues_path.
    assert config.resolve_issues_path(None, cfg) == "projects/x/issues.json"
    # Else the bare default.
    assert config.resolve_issues_path(None, {}) == "issues.json"
    # A non-.json explicit arg is ignored in favour of config.
    assert config.resolve_issues_path("not-json", cfg) == "projects/x/issues.json"


def test_resolve_issues_path_non_json_cfg_falls_back():
    assert config.resolve_issues_path(None, {"issues_path": "issues.csv"}) == "issues.json"


def test_default_offsets_is_shared_list():
    assert config.DEFAULT_OFFSETS == [0, 2, 5, 10, 20, 30]


# ---------------------------------------------------------------------------
# Scaffolding a fresh project's config.json
# ---------------------------------------------------------------------------

def _project_with_video(root, name, videos=("clip.mp4",)):
    """Create projects/<name>/input/ under `root` with the given video files."""
    input_dir = os.path.join(root, "projects", name, "input")
    os.makedirs(input_dir)
    for v in videos:
        open(os.path.join(input_dir, v), "w").close()
    return input_dir


def test_scaffold_config_writes_template_from_detected_video():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            _project_with_video(d, "p1")
            path, cfg = config.scaffold_config("p1")
            assert path == os.path.join("projects", "p1", "config.json")
            # Values use forward slashes regardless of host OS.
            assert cfg["video_path"] == "projects/p1/input/clip.mp4"
            assert cfg["issues_path"] == "projects/p1/issues.json"
            assert cfg["frames_root"] == "projects/p1/frames"
            assert cfg["frame_offsets_seconds"] == config.DEFAULT_OFFSETS
            assert cfg["server_port"] == 8765
            # And it's the file we actually wrote.
            on_disk, _ = config.load_config(project="p1")
            assert on_disk == cfg
        finally:
            os.chdir(cwd)


def test_scaffold_config_refuses_overwrite_without_force():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            _project_with_video(d, "p2")
            config.scaffold_config("p2")
            try:
                config.scaffold_config("p2")
                assert False, "expected FileExistsError"
            except FileExistsError:
                pass
            # force overwrites.
            _, cfg = config.scaffold_config("p2", force=True)
            assert cfg["video_path"] == "projects/p2/input/clip.mp4"
        finally:
            os.chdir(cwd)


def test_scaffold_config_errors_on_zero_or_multiple_videos():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            os.makedirs(os.path.join(d, "projects", "empty", "input"))
            try:
                config.scaffold_config("empty")
                assert False, "expected FileNotFoundError for no video"
            except FileNotFoundError:
                pass
            _project_with_video(d, "many", videos=("a.mp4", "b.mov"))
            try:
                config.scaffold_config("many")
                assert False, "expected ValueError for multiple videos"
            except ValueError:
                pass
            # An explicit --video disambiguates the multi-video case.
            _, cfg = config.scaffold_config(
                "many", video="projects/many/input/b.mov")
            assert cfg["video_path"] == "projects/many/input/b.mov"
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
