# TODOS

## Cleanup

## Completed

- **DEFAULT_OFFSETS duplication** — moved to `config.DEFAULT_OFFSETS`; `extract_frames.py` and `extract_issues.py` now import it.
- **_resolve_issues_path copy-paste** — replaced by `config.resolve_issues_path` (+ `resolve_config_path` / `load_config`), shared by `build_review.py`, `extract_frames.py`, `html_export.py`, `extract_issues.py`, and `generate_pptx.py`. The same change added `--project` / `--config` overrides so "active project" is no longer global mutable state.
