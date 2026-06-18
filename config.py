"""config.py — shared config + path resolution for the pipeline.

One place for the things every stage needs to agree on: where `config.json`
lives, the default frame offsets, and how a CLI turns its arguments into a
config + an `issues.json` path. Previously each script hardcoded
`CONFIG_FILE = "config.json"` at the repo root and carried its own copy of
`_resolve_issues_path` (build_review, extract_frames, html_export,
extract_issues) — so "active project" was global mutable state set by copying a
project's config over the root one.

`--project NAME` is sugar for `projects/NAME/config.json`; `--config PATH` names
a config file directly. With neither, we fall back to the repo-root
`config.json` so existing manual runs keep working.
"""

from __future__ import annotations

import glob
import json
import os

CONFIG_FILE = "config.json"

# Frame seek offsets (seconds) applied at each anchor's ts_seconds. Shared so
# frame extraction and issue extraction always agree.
DEFAULT_OFFSETS = [0, 2, 5, 10, 20, 30]

# Video containers we'll auto-detect in a project's input/ dir when scaffolding.
VIDEO_EXTS = ("*.mp4", "*.mov", "*.mkv", "*.webm")


def resolve_config_path(project=None, config=None):
    """Pick the config file: explicit --config, then --project sugar, then root."""
    if config:
        return config
    if project:
        return os.path.join("projects", project, "config.json")
    return CONFIG_FILE


def load_config(*, project=None, config=None):
    """Return (config_dict, path). Missing file -> ({}, path)."""
    path = resolve_config_path(project, config)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), path
    return {}, path


def resolve_issues_path(explicit=None, cfg=None):
    """Resolve the issues.json path: an explicit `*.json` arg, then the config's
    `issues_path`, then a bare `issues.json` in the cwd."""
    if explicit and explicit.endswith(".json"):
        return explicit
    if cfg and cfg.get("issues_path", "").endswith(".json"):
        return cfg["issues_path"]
    return "issues.json"


# ---------------------------------------------------------------------------
# Scaffolding a fresh project's config.json
# ---------------------------------------------------------------------------
# A new project folder under projects/ typically has only input/<video> (+ a
# transcript). The pipeline stages all need a config.json, so the first run used
# to require hand-copying another project's config. `scaffold_config` writes one
# from the same template every project uses, auto-detecting the input video.

def _posix(path):
    """Config values use forward slashes regardless of host OS, matching the
    convention in the committed project configs."""
    return path.replace(os.sep, "/")


def find_input_video(project):
    """Return the single video under projects/<project>/input/, or raise.

    Zero matches or more than one is a hard error — the caller should pass an
    explicit video path to disambiguate."""
    input_dir = os.path.join("projects", project, "input")
    vids = sorted(
        p for ext in VIDEO_EXTS for p in glob.glob(os.path.join(input_dir, ext))
    )
    if not vids:
        raise FileNotFoundError(
            f"No video ({'/'.join(VIDEO_EXTS)}) found in {input_dir}")
    if len(vids) > 1:
        listing = "\n  ".join(vids)
        raise ValueError(
            f"Multiple videos in {input_dir}; pass an explicit video:\n  {listing}")
    return _posix(vids[0])


def default_config(project, video_path):
    """The config dict written for a fresh project (matches committed configs)."""
    base = os.path.join("projects", project)
    return {
        "video_path": _posix(video_path),
        "issues_path": _posix(os.path.join(base, "issues.json")),
        "frames_root": _posix(os.path.join(base, "frames")),
        "output_pptx": _posix(os.path.join(base, "output", "report.pptx")),
        "template_path": "generic_template.pptx",
        "frame_offsets_seconds": list(DEFAULT_OFFSETS),
        "server_port": 8765,
        "ffmpeg_path": "ffmpeg",
    }


def scaffold_config(project, *, video=None, force=False):
    """Write projects/<project>/config.json from the template. Returns (path, cfg).

    Refuses to overwrite an existing config unless `force` is set. The video is
    the explicit `video` arg, else the single file auto-detected in input/."""
    path = resolve_config_path(project=project)
    if os.path.exists(path) and not force:
        raise FileExistsError(f"{path} already exists; pass force=True to overwrite")
    video_path = video or find_input_video(project)
    cfg = default_config(project, video_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path, cfg
