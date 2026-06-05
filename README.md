# Video to PPTX

Turns a usability-session recording into a structured PowerPoint issue report. Given a video and a markdown file of timestamped issues, the toolchain extracts candidate frames, lets you pick and annotate the best screenshot for each issue, then assembles a slide deck from a `.pptx` template.

---

## Prerequisites (all steps)

- Python 3.11+
- Dependencies: `pip install python-pptx Pillow`
- `config.json` present at the repo root (copy from `projects/<name>/config.json`; see [Configuration](#configuration))

---

## Workflow

### Step 1 — Prepare the video and transcript

Obtain the session recording (`.mp4`) and, optionally, its WebVTT caption file.

**If you have a `.vtt` file**, strip cue IDs and speaker tags before using it as a reference:

```
python clean_vtt.py session.vtt
```

Produces `session.txt` alongside the source file. No prerequisites beyond Python.

---

### Step 2 — Extract issues from the transcript

Run the four-pass extraction workflow to produce `issues.json` from the session transcript. See [EXTRACT_ISSUES.md](EXTRACT_ISSUES.md) for the full runbook.

```
# Preview the overlapping windows you will read:
python extract_issues.py windows projects/<session>/input/<transcript>.txt --text

# After completing passes 1-3 and producing candidates.json, assemble and validate:
python extract_issues.py assemble candidates.json projects/<session>/input/<transcript>.txt
```

`assemble` reads `config.json` for `video_path`, `frame_offsets_seconds`, and `issues_path`, validates all candidates against schema v2, and writes `issues.json` atomically. Any ungrounded quote, missing title, or invalid enum aborts the write.

Set `issues_path` in `config.json` to point at the output `issues.json`.

---

### Step 3 — Extract candidate frames

**Prerequisites:** `ffmpeg` on your `PATH` (or set `ffmpeg_path` in `config.json`).

```
python extract_frames.py
```

Reads each issue's `ts_seconds` from `issues.json`, seeks to that position in the video, and captures frames at the offsets defined in `frame_offsets_seconds` (default: 0 s, +2 s, +5 s, +10 s, +20 s, +30 s). Skips rejected and merged-out issues. Already-extracted frames are skipped on re-runs; use `--force` to re-extract.

---

### Step 4 — Review and select screenshots

```
python build_review.py
```

Starts a local HTTP server (default port 8765) and opens the review UI in your browser. Driven by `issues.json`. For each issue:

1. **Gate 3** — Accept, reject (with reason), or edit the issue.
2. **Gate 5** — For each anchor facet, click a thumbnail to open the lightbox, navigate frames, drag to crop, and pick the best screenshot.

The review console writes all decisions back to `issues.json` atomically. You can re-open the UI at any time to revise earlier picks.

After review, export a self-contained HTML report:

```
python html_export.py [path/to/issues.json] [-o report.html]
```

Embeds all selected screenshots as base64 and renders accepted/edited issues with evidence quotes and timestamps. Issues with incomplete frame selections appear in a warning banner.

---

### Step 5 — Generate the PowerPoint

**Prerequisites:** A `.pptx` template with named shapes matching the schema below (set `template_path` in `config.json`).

```
python generate_pptx.py
```

Duplicates the template's first slide once per issue, fills all named shapes, inserts the selected screenshot (applying any crop), then removes the blank template slide. Output is written to `output_pptx`.

**Required named shapes in the template slide:**

| Shape name | Content |
|---|---|
| `issue_id` | VID-NNN |
| `severity_badge` | S0 / S1 / S2 / S3 (colored automatically) |
| `title` | Issue title |
| `observed` | Observed behavior bullets |
| `expected` | Expected behavior bullets |
| `notes` | Notes bullets (optional — shape may be absent) |
| `metadata` | Roles · Area · Timestamps concatenated |
| `screenshot` | Selected frame (cropped/annotated if applicable) |

---

## Adding a new project

1. Create `projects/<project-name>/` with three subfolders: `input/`, `screenshots/`, `output/`.
2. Drop the video (`.mp4`) and optional `.vtt` into `input/`.
3. Copy the config template below into `projects/<project-name>/config.json` and fill in the paths.
4. Copy that config to the repo root to activate it: `cp projects/<project-name>/config.json config.json`.

Everything under `projects/` is gitignored automatically. To switch between projects, copy the target project's `config.json` to the repo root.

---

## Configuration

`config.json` at the repo root controls all scripts. All paths are relative to the repo root.

```json
{
  "video_path":             "projects/<name>/input/session.mp4",
  "issues_path":            "projects/<name>/issues.json",
  "output_dir":             "projects/<name>/screenshots",
  "frame_offsets_seconds":  [0, 2, 5, 10, 20, 30],
  "template_path":          "generic_template.pptx",
  "output_pptx":            "projects/<name>/output/Report.pptx",
  "server_port":            8765,
  "ffmpeg_path":            "ffmpeg"
}
```

`issues_path` points at the schema v2 `issues.json` produced by `extract_issues.py assemble`. `ffmpeg_path` defaults to `"ffmpeg"` (assumes it is on `PATH`). Override with an absolute path if needed.

---

## File layout

```
repo root/
├── config.json                  # active project config (gitignored; copy from projects/<name>/)
├── CHANGELOG.md                 # version history
├── TODOS.md                     # deferred cleanup items
├── EXTRACT_ISSUES.md            # runbook for the four-pass extraction workflow
├── Description_template.md      # legacy issue markdown format reference (tracked)
├── generic_template.pptx        # slide template with named shapes (tracked)
├── issues_store.py              # schema v2 load/save/validate — used by all pipeline stages
├── clean_vtt.py                 # Step 1: strip VTT formatting (tracked)
├── extract_issues.py            # Step 2: transcript → issues.json extraction harness
├── extract_frames.py            # Step 3: extract JPEGs via ffmpeg (tracked)
├── build_review.py              # Step 4: browser-based review UI (tracked)
├── html_export.py               # Step 4b: static self-contained HTML report
├── generate_pptx.py             # Step 5: assemble the deck (tracked)
└── projects/                    # all project data — entirely gitignored
    └── <project-name>/
        ├── config.json          # project's saved config (copy to root to activate)
        ├── issues.json          # schema v2 issue store (written by extract_issues.py)
        ├── input/               # source video, optional .vtt, transcript
        ├── screenshots/         # extracted frames (created by extract_frames.py)
        └── output/              # generated .pptx and HTML report files
```
