---
name: process-session
description: >-
  Process a usability-test session for a folder under projects/: clean the VTT,
  extract issues from the transcript (autonomous 4-pass), extract candidate
  frames, and launch the review console. Stops at the human review gate — does
  NOT build the HTML report. Use when asked to process / run / start a session,
  or to "process projects/<name>/input". Phase B is the build-html skill.
---

# process-session — Phase A: input/ folder → live review console

Run the pipeline for one project folder under `projects/`, from raw transcript to
a running review server, then **stop** for the human to triage. All commands run
from the repo root. Every script takes `--project <name>` (sugar for
`projects/<name>/config.json`) — never copy a config to the repo root.

## Argument

`<project>` = a folder name under `projects/` (e.g. `2-news-20260610`). If the user
didn't name one, list the `projects/*` subfolders that contain an `input/` dir and
ask which to process. Set `<project>` and proceed.

## Steps

### 0. Guard: don't clobber a reviewed session
If `projects/<project>/issues.json` already exists, read it. If any issue has a
status other than `proposed` (i.e. `accepted` / `edited` / `rejected` / `merged_out`),
it has been reviewed — re-extracting mints fresh `iss_*` ids and orphans picked
frames. **Stop and confirm with the user** before overwriting. If it's all
`proposed` or the file is absent, continue.

### 0.5 Bootstrap the project config
Every stage needs `projects/<project>/config.json`. A fresh project folder
(only an `input/` dir) has none. If it's missing, scaffold it — this auto-detects
the single video in `input/` and writes the standard config:
```
python init_project.py --project <project>
```
Confirm the printed `video_path` matches the recording. If `input/` has zero or
multiple videos, the command errors — pass `--video <path>` to disambiguate. If
`config.json` already exists, skip this step (re-run with `--force` only to reset it).

### 1. Locate + clean the transcript
Look in `projects/<project>/input/`. If a `.vtt` exists, clean it:
```
python clean_vtt.py "projects/<project>/input/<name>.vtt"
```
This writes `projects/<project>/input/<name>.txt`. If there's only a `.txt`, use it.
Set `<txt>` = that transcript path.

### 2. Read the windows (extraction Pass 1 input)
```
python extract_issues.py windows "<txt>" --text
```
Read the printed windows. They overlap by design; a defect that straddles a
boundary appears whole in one window.

### 3. Extraction passes 1–3 (you, autonomous)
Follow `EXTRACT_ISSUES.md` (the authoritative runbook). Run all three — do not
shortcut to a single read:
- **Pass 1 — window-extract (recall).** For each window, emit a candidate for any
  line where the tester says something is wrong / broken / confusing / inconsistent /
  should-be-different, plus silent failures and called-out edge cases. Bias to recall;
  mark unsure ones `"confidence": "Low"`.
- **Pass 2 — merge / group.** Dedupe the same defect seen across windows into one
  issue. Only set `group.is_group: true` to bundle *distinct* small defects when **all**
  hold: same screen/component, same fix domain, each trivial, and none is Functional /
  Data integrity / Permissions. Multiple evidence anchors on one defect is NOT a group.
- **Pass 3 — adversarial audit (fresh read).** Re-read the full transcript + your draft
  (titles, severities, quotes) as an independent second reading. Report ONLY misses
  (add as new candidates, `origin: "audit_added"`), mis-severities, and false positives.
  Fold accepted recoveries in.

Write the result to `projects/<project>/input/extracted_candidates.json` (existing
convention). It's a JSON array of issue candidates (or `{"session": {...}, "issues": [...]}`).

**Candidate contract** (see `extract_issues.py` module docstring):
- `title` **required** and scannable — a title-less issue fails validation.
- One verbatim `quote` per anchor — a literal substring of the transcript
  (whitespace/case tolerant, wording is not). **Never paraphrase**, or assembly fails.
  Copy the quote straight from the window text. `line_hint` (the `[Lnnn]`) optionally
  disambiguates a repeated phrase.
- **Do NOT set `ts_seconds`** — the harness derives it from the transcript.
- `origin`: `"window_extract"` (Pass 1) or `"audit_added"` (Pass 3).

Per-issue fields: `title`, `severity` (S0–S4), `categories` (1–2 from the enum below),
`confidence` (High/Med/Low), `affected_area`, `affected_roles`, `observed` (bullets,
each may cite a short quote), `expected` (bullets), `notes` (optional; no "why"/"how to fix"),
`anchors` (`caption`, `quote`, optional `line_hint` / `transcript_ref`).

#### Severity rubric (S0–S4)
- **S0 – Blocker:** prevents task completion, crash, data loss/corruption, security/permission breach.
- **S1 – Critical:** major function incorrect, workflow severely impaired, frequent failure, wrong calculation/logic.
- **S2 – Major:** painful workaround exists; confusing UX that likely causes errors; important inconsistency.
- **S3 – Minor:** cosmetic UI, minor copy, spacing, low-impact inconsistency.
- **S4 – Trivial:** polish, micro-optimizations.

If the tester explicitly states severity ("this is minor", "big deal"), honor it.

#### Category enum (assign 1–2)
`Functional` · `Data integrity` · `Permissions` · `UI` · `Labeling` · `Error handling` · `Performance`

#### Trigger phrases (treat as an issue)
"bug", "issue", "problem", "broken", "doesn't work", "weird", "wrong"; "I expected…",
"it should…", "why is it…", "this is confusing", "this is inconsistent", "that doesn't
make sense"; "spacing", "alignment", "copy", "label", "typo", "layout"; "performance",
"slow", "lag", "timeout", "spinner"; "permissions", "role", "can't see", "shouldn't have
access". Also flag silent failures (no error shown but behavior wrong) and risky edge
cases the tester calls out. If a fact is missing (e.g. exact screen), don't invent —
write Unknown and quote what's known.

### 4. Assemble + validate (Pass 4, code — the hard gate)
```
python extract_issues.py --project <project> assemble "projects/<project>/input/extracted_candidates.json" "<txt>"
```
(`--project`/`--config` work either before or after the `assemble` subcommand.)
This grounds every quote to an authoritative `ts_seconds`, renumbers, validates the
schema, atomically writes `projects/<project>/issues.json`, and stamps `issues_path`
into the project config. Any ungrounded quote, missing title, or bad enum aborts the
write — fix the candidate and re-run. Confirm the printed
`Wrote …/issues.json: N issues, M anchors (validated)` line.

### 5. Extract candidate frames
```
python extract_frames.py --project <project>
```
Needs `ffmpeg` on PATH or `ffmpeg_path` set in the project config. Writes JPEGs under
`projects/<project>/frames/<issue.id>/<anchor.id>/` and records `candidate_frames` per
anchor. If ffmpeg is missing, report that and stop.

### 6. Launch the review console (background)
Start the server in the **background** (it's long-running):
```
python build_review.py --project <project>
```
Report the URL `http://localhost:8765` (or the project's `server_port`) and a one-line
severity summary (count of issues by S-level).

### 7. STOP — hand off to the human
State plainly: Phase A is complete, the review console is running at the URL, and the
user should triage issues (Gate 3: accept/reject/edit) and pick a screenshot per facet
(Gate 5) in the browser. Tell them the exact follow-up to build the report:
**`/build-html <project>`** (or just say "continue"). **Do not build the HTML report.**

## Notes
- Re-running over a reviewed `issues.json` is the main footgun — see step 0.
- The rubric above is embedded here on purpose (the per-project `llm_prompt.md` is
  gitignored and not present in every project).
