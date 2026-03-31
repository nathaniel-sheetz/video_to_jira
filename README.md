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

### Step 2 — Author the issues markdown

Create a markdown file with one `# VID-NNN` section per issue. See `Description_template.md` for the required field structure.

Key fields that drive the toolchain:

| Field | Used by |
|---|---|
| `Timestamps` | `extract_frames.py` — seeks to these positions in the video |
| `Severity` | `generate_pptx.py` — colors the severity badge on the slide |
| `Observed behavior` / `Expected behavior` | `generate_pptx.py` — fills slide text shapes |

Set `issues_path` in `config.json` to point at this file.

---

### Step 3 — Extract candidate frames

**Prerequisites:** `ffmpeg` on your `PATH` (or set `ffmpeg_path` in `config.json`).

```
python extract_frames.py
```

Reads each issue's `Timestamps`, seeks to the start of each range, and captures frames at the offsets defined in `frame_offsets_seconds` (default: 0 s, +2 s, +5 s, +10 s, +20 s, +30 s). Output lands in `screenshots/<video-stem>/<VID-NNN>/ts-HH-MM-SS/`.

Already-extracted frames are skipped on re-runs.

---

### Step 4 — Review and select screenshots

```
python build_review.py
```

Starts a local HTTP server (default port 8765) and opens the review UI in your browser. For each issue:

1. Click a thumbnail to open the lightbox.
2. Drag to set an optional crop region.
3. Optionally click **Draw →** to annotate with a pen.
4. Click **Continue** to save and advance, or **Skip** to mark the issue with no screenshot.

Selections are written to the file specified by `selections_path` in `config.json`. You can re-open the UI at any time to revise earlier picks.

To borrow a screenshot from a different issue, use the **"Browse frames from another issue"** dropdown — the saved selection will be recorded under the current (target) issue.

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
  "issues_path":            "projects/<name>/session_issues.md",
  "output_dir":             "projects/<name>/screenshots",
  "frame_offsets_seconds":  [0, 2, 5, 10, 20, 30],
  "template_path":          "generic_template.pptx",
  "output_pptx":            "projects/<name>/output/Report.pptx",
  "selections_path":        "projects/<name>/selections.json",
  "server_port":            8765,
  "ffmpeg_path":            "ffmpeg"
}
```

`ffmpeg_path` defaults to `"ffmpeg"` (assumes it is on `PATH`). Override with an absolute path if needed.

---

## File layout

```
repo root/
├── config.json                  # active project config (gitignored; copy from projects/<name>/)
├── Description_template.md      # issue markdown format reference (tracked)
├── generic_template.pptx        # slide template with named shapes (tracked)
├── clean_vtt.py                 # Step 1: strip VTT formatting (tracked)
├── extract_frames.py            # Step 3: extract JPEGs via ffmpeg (tracked)
├── build_review.py              # Step 4: browser-based review UI (tracked)
├── generate_pptx.py             # Step 5: assemble the deck (tracked)
└── projects/                    # all project data — entirely gitignored
    └── <project-name>/
        ├── config.json          # project's saved config (copy to root to activate)
        ├── <issues>.md          # issue descriptions
        ├── selections.json      # saved screenshot picks
        ├── input/               # source video, optional .vtt
        ├── screenshots/         # extracted frames (created by extract_frames.py)
        └── output/              # generated .pptx files
```
