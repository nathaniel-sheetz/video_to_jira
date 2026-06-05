# TODOS

## Cleanup

- **DEFAULT_OFFSETS duplication**
  **Priority:** P2
  `DEFAULT_OFFSETS = [0, 2, 5, 10, 20, 30]` is defined identically in `extract_frames.py:39` and `extract_issues.py:75`. Move to a shared location (e.g., `issues_store` or a new `config.py`) so frame extraction and issue extraction always use the same offsets.

- **_resolve_issues_path copy-paste**
  **Priority:** P2
  The CLI helper that finds the `issues.json` path (check argv[1], then `config.json:issues_path`, then `'issues.json'`) is duplicated in `build_review.py`, `extract_frames.py`, and `html_export.py`. Extract to a shared utility or into `issues_store` so changes propagate everywhere.

## Completed
