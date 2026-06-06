# Changelog

All notable changes to this project will be documented in this file.

## [0.1.1.0] - 2026-06-06

### Added
- **HTML export TOC**: Long reports are now navigable. The issues report opens with a table of contents — issue ID, severity chip, and title — with anchor links to each issue section and a "↑ Contents" back-link on every article.

### Changed
- **Observed block promoted to primary**: The most important signal is now first. In both the review console and the HTML export, the Observed section is rendered first with a prominent green accent border and larger text. Evidence is de-emphasised to a muted supporting role. Expected and Evidence are stacked vertically (replacing the two-column Observed/Expected layout).

## [0.1.0.0] - 2026-06-05

### Added
- **issues_store**: Single source of truth for the review-console pipeline. Schema v2 `issues.json` with atomic save, full validation (severity, category, title, anchor evidence), renumber-by-severity, merge/split/edit with snapshot, and `validate_or_raise` hard gate.
- **extract_issues**: Four-pass transcript → grounded `issues.json` harness. Windowed transcript parsing, verbatim-quote grounding with `ts_seconds` derivation, grouping policy enforcement, and `EXTRACT_ISSUES.md` runbook. Run `python extract_issues.py assemble candidates.json <transcript.txt>` to produce a validated `issues.json`.
- **html_export**: Static self-contained session report. Embeds screenshots as base64, renders accepted/edited issues with evidence quotes and timestamps, incomplete-anchor banner, and deterministic output keyed on `session.generated_at`. Run `python html_export.py [issues.json] [-o report.html]`.
- **EXTRACT_ISSUES.md**: Runbook for the four-pass extraction workflow (window-extract → merge/group → adversarial audit → validate).

### Changed
- **build_review**: Rebuilt review console onto schema v2. Gate 3 (Accept / Reject+reason / Edit) and Gate 5 (screenshot pick per facet) are now driven entirely by `issues.json`. Adds lightbox with frame navigation, saved-crop preview, `frames_extracted()` guard on accept-advance, single-key focus guard (`editing||isTyping()`), and reject-reason picker. HTTP handlers now enforce Content-Length limits (64 KB for actions, 50 MB for image uploads).
- **extract_frames**: Reworked onto schema v2. Reads anchors from `issues.json`, seeks to `ts_seconds`, writes `candidate_frames` back atomically. Skips rejected and merged-out issues. Idempotent re-run support with `--force` flag.

### Fixed
- `renumber()` now gracefully handles missing or invalid severity values (sorts to end) instead of raising `KeyError` with a misleading error message.
- `_handle_action` and `_handle_save_image` HTTP handlers now reject oversized payloads with 413 before reading, preventing OOM on malformed requests.
- `export_issues()` in `html_export.main()` is now called once and assigned to a variable instead of being evaluated twice.
- Port constant `8765` in `build_review.main()` is now a named `DEFAULT_PORT` constant.
