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

import json
import os

CONFIG_FILE = "config.json"

# Frame seek offsets (seconds) applied at each anchor's ts_seconds. Shared so
# frame extraction and issue extraction always agree.
DEFAULT_OFFSETS = [0, 2, 5, 10, 20, 30]


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
