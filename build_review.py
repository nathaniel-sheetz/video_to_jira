#!/usr/bin/env python3
"""
build_review.py  —  Step 5: Screenshot review UI.

Starts a local HTTP server and opens the review tool in your browser.
  - Navigate between issues via the top nav bar
  - Click a thumbnail in the strip to select it as the pending screenshot
  - Click the large main image to open it in the lightbox for crop/draw
  - Edit title, severity, observed, expected, notes, and metadata inline
  - Click Validate & Next to save all edits and advance to the next issue
  - Click Skip to mark an issue with no screenshot
  - All progress is written to selections.json automatically

Usage:
    python build_review.py
"""

import base64
import functools
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

CONFIG_FILE = "config.json"

# ---------------------------------------------------------------------------
# Markdown parser  (extended: parses all text fields needed by the UI)
# ---------------------------------------------------------------------------

def parse_issues(md_path):
    with open(md_path, encoding="utf-8") as f:
        content = f.read()

    sections = re.split(r"(?m)(?=^# VID-)", content)
    issues = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        lines = section.split("\n")
        header = re.match(r"# (VID-\d+)\s*[-\u2013]\s*(.+)", lines[0])
        if not header:
            continue

        issue = {
            "id":             header.group(1),
            "title":          header.group(2).strip(),
            "severity":       "",
            "timestamps":     "",
            "affected_roles": "",
            "affected_area":  "",
            "observed":       "",
            "expected":       "",
            "notes":          "",
        }

        for line in lines[1:20]:
            m = re.match(r"-\s+\*\*(.+?):\*\*\s*(.*)", line)
            if m:
                key = m.group(1).strip().lower()
                val = m.group(2).strip()
                mapping = {
                    "severity":       "severity",
                    "timestamps":     "timestamps",
                    "affected roles": "affected_roles",
                    "affected area":  "affected_area",
                }
                if key in mapping:
                    issue[mapping[key]] = val

        for field, heading in [
            ("observed", "Observed behavior"),
            ("expected", "Expected behavior"),
            ("notes",    "Notes"),
        ]:
            pat = rf"## {re.escape(heading)}\n(.*?)(?=\n## |\Z)"
            m = re.search(pat, section, re.DOTALL | re.IGNORECASE)
            if m:
                issue[field] = m.group(1).strip()

        issues.append(issue)

    return issues


# ---------------------------------------------------------------------------
# Screenshot discovery
# ---------------------------------------------------------------------------

def get_screenshots(output_dir, vid):
    """Return sorted list of relative paths for a VID's candidate frames."""
    vid_dir = Path(output_dir) / vid
    if not vid_dir.exists():
        return []
    paths = []
    for ts_dir in sorted(vid_dir.iterdir()):
        if ts_dir.is_dir():
            for frame in sorted(ts_dir.glob("*.jpg")):
                paths.append(frame.as_posix())
    return paths


# ---------------------------------------------------------------------------
# selections.json helpers
# ---------------------------------------------------------------------------

def load_selections(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_selections(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Embedded HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Screenshot Review</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#181818;color:#e0e0e0;min-height:100vh}

/* ── Nav ── */
#nav{position:fixed;top:0;left:0;right:0;background:#111;padding:10px 14px;display:flex;flex-wrap:wrap;gap:6px;z-index:100;border-bottom:1px solid #2a2a2a}
.chip{padding:4px 11px;border-radius:12px;font-size:11px;font-weight:700;cursor:pointer;border:2px solid transparent;transition:all .15s;letter-spacing:.03em}
.chip.pending{background:#2a2a2a;color:#888}
.chip.selected{background:#14391e;color:#4ade80;border-color:#22c55e}
.chip.skipped{background:#3b1f00;color:#fb923c;border-color:#f97316}
.chip.merged{background:#2a1a3e;color:#c084fc;border-color:#a855f7}
.chip.active{outline:2px solid #60a5fa;outline-offset:2px}

/* ── Main ── */
#main{margin-top:62px;padding:28px 28px 60px;max-width:1300px;margin-left:auto;margin-right:auto}

/* ── Issue header ── */
#issue-header{margin-bottom:18px}
#issue-id{font-size:12px;font-weight:700;color:#60a5fa;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
#edit-title{font-size:18px;font-weight:600;background:#1f1f1f;color:#e0e0e0;border:1px solid #3a3a3a;border-radius:6px;padding:6px 10px;width:100%;font-family:inherit}
#edit-title:focus{outline:none;border-color:#60a5fa}
#header-row2{display:flex;align-items:center;gap:12px;margin-top:8px}
#edit-severity{background:#1f1f1f;color:#f87171;border:1px solid #3a3a3a;border-radius:6px;padding:5px 8px;font-size:13px;cursor:pointer;font-family:inherit}
#edit-severity:focus{outline:none;border-color:#60a5fa}
#progress-line{font-size:12px;color:#666}

/* ── Screenshot section ── */
#screenshot-section{display:flex;gap:16px;margin-top:18px;align-items:flex-start}
#main-shot{flex:1;min-width:0;background:#111;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;cursor:pointer;max-height:480px;display:flex;align-items:center;justify-content:center}
#main-shot img{max-width:100%;max-height:480px;object-fit:contain;display:block}
#main-shot-empty{color:#555;font-size:13px;padding:40px 20px;text-align:center}
#thumb-strip{width:190px;flex-shrink:0;max-height:480px;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.strip-item{border:2px solid #2a2a2a;border-radius:6px;overflow:hidden;cursor:pointer;flex-shrink:0;position:relative}
.strip-item:hover{border-color:#60a5fa}
.strip-item.active{border-color:#22c55e}
.strip-item img{display:block;width:100%;height:auto}
.strip-label{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.72);font-size:9px;padding:3px 5px;color:#bbb}
#no-screenshots{color:#555;padding:20px 0;font-size:14px}

/* ── Source bar ── */
#source-bar{display:none;margin-top:12px;padding:8px 12px;background:#1a1a2e;border:1px solid #3b3b8a;border-radius:6px;font-size:12px;color:#818cf8;align-items:center;gap:8px}
#source-bar strong{color:#a5b4fc}
#source-reset-btn{margin-left:auto;background:none;border:1px solid #3b3b8a;color:#818cf8;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px}
#other-issue-row{margin-top:12px;display:flex;align-items:center;gap:10px}
#other-issue-row span{font-size:12px;color:#666}
#other-issue-select{background:#1f1f1f;color:#ccc;border:1px solid #3a3a3a;border-radius:5px;padding:5px 8px;font-size:12px;cursor:pointer;max-width:340px}

/* ── Fields section ── */
#fields-section{margin-top:22px;display:flex;flex-direction:column;gap:16px}
.field-group{display:flex;flex-direction:column;gap:6px}
.field-group label{font-size:11px;font-weight:700;color:#60a5fa;text-transform:uppercase;letter-spacing:.06em}
.field-group textarea,.field-group input[type=text]{background:#1f1f1f;color:#e0e0e0;border:1px solid #3a3a3a;border-radius:6px;padding:8px 10px;font-size:13px;font-family:inherit;resize:vertical;line-height:1.5}
.field-group textarea:focus,.field-group input:focus{outline:none;border-color:#60a5fa}

/* ── Action row ── */
#action-row{display:flex;gap:12px;margin-top:24px;padding-bottom:40px}
#validate-btn{padding:10px 28px;background:#1d4ed8;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
#validate-btn:hover{background:#2563eb}
#skip-btn{padding:8px 20px;background:#262626;color:#888;border:1px solid #3a3a3a;border-radius:6px;cursor:pointer;font-size:13px;transition:background .15s}
#skip-btn:hover{background:#333;color:#ccc}
#split-btn{padding:8px 16px;background:#0e3a3a;color:#2dd4bf;border:1px solid #0d9488;border-radius:6px;cursor:pointer;font-size:13px;transition:background .15s}
#split-btn:hover,#split-btn.active{background:#134e4a;border-color:#14b8a6;color:#5eead4}
#merge-btn{padding:8px 16px;background:#262626;color:#888;border:1px solid #3a3a3a;border-radius:6px;cursor:pointer;font-size:13px;transition:background .15s}
#merge-btn:hover{background:#333;color:#ccc}
#merge-row{margin-top:10px;align-items:center;gap:10px;flex-wrap:wrap;font-size:12px;color:#666}
#merge-target-select{background:#1f1f1f;color:#ccc;border:1px solid #3a3a3a;border-radius:5px;padding:5px 8px;font-size:12px;cursor:pointer;max-width:340px}
#merge-confirm-btn{padding:6px 14px;background:#7c1d1d;color:#fca5a5;border:1px solid #991b1b;border-radius:5px;cursor:pointer;font-size:12px}
#merge-confirm-btn:hover{background:#991b1b}
#merge-cancel-btn{padding:6px 14px;background:#262626;color:#888;border:1px solid #3a3a3a;border-radius:5px;cursor:pointer;font-size:12px}
#slide-b-section{margin-top:22px;border-top:1px solid #2a2a2a;padding-top:18px}
#slide-b-header{font-size:12px;font-weight:700;color:#2dd4bf;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
#slide-b-shot-row{display:flex;gap:16px;align-items:flex-start}
#slide-b-main-shot{flex:1;min-width:0;background:#111;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden;cursor:pointer;max-height:320px;display:flex;align-items:center;justify-content:center}
#slide-b-main-shot img{max-width:100%;max-height:320px;object-fit:contain;display:block}
#slide-b-main-shot-empty{color:#555;font-size:12px;padding:30px 16px;text-align:center}
#slide-b-strip{width:150px;flex-shrink:0;max-height:320px;overflow-y:auto;display:flex;flex-direction:column;gap:6px}
.strip-item.slide-b-active{border-color:#2dd4bf !important}
#slide-b-clear-btn{margin-top:10px;padding:6px 14px;background:#262626;color:#888;border:1px solid #3a3a3a;border-radius:5px;cursor:pointer;font-size:12px}
#merged-banner{margin-top:18px;padding:12px 16px;background:#1a0a2e;border:1px solid #7c3aed;border-radius:8px;font-size:13px;color:#c084fc}
#unmerge-btn{margin-top:8px;padding:6px 14px;background:#262626;color:#888;border:1px solid #3a3a3a;border-radius:5px;cursor:pointer;font-size:12px}

/* ── Completion ── */
#completion{display:none;padding:12px 18px;background:#0d2818;border:1px solid #166534;border-radius:8px;margin-top:22px;font-size:14px}
#completion.visible{display:block}
#completion code{background:#1a3a2a;padding:2px 6px;border-radius:4px;font-size:13px}

/* ── Hover preview ── */
#hover-preview{position:fixed;z-index:150;width:480px;pointer-events:none;border:1px solid #333;border-radius:8px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.8);background:#111;display:none}
#hover-preview img{display:block;width:100%;height:auto}

/* ── Lightbox ── */
#lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:200;flex-direction:column;align-items:center;justify-content:center;padding:16px}
#lightbox.open{display:flex}
#lb-top{display:flex;align-items:center;gap:16px;margin-bottom:12px;width:100%;max-width:92vw;justify-content:space-between}
#lb-nav{display:flex;align-items:center;gap:8px;flex-shrink:0}
#lb-prev,#lb-next{background:none;border:1px solid #444;color:#bbb;border-radius:4px;width:30px;height:30px;cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;transition:background .15s;line-height:1}
#lb-prev:hover,#lb-next:hover{background:#2a2a2a;color:#fff}
#lb-counter{font-size:12px;color:#666;min-width:36px;text-align:center}
#lb-label{font-size:13px;color:#999;flex:1;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lb-hint{font-size:12px;color:#555;flex-shrink:0}
#crop-canvas{cursor:crosshair;border:1px solid #333;max-width:90vw;max-height:70vh;display:block;transition:border-color .2s}
#lb-actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;justify-content:center}
.lb-btn{padding:8px 22px;border-radius:6px;border:none;cursor:pointer;font-size:14px;font-weight:500;transition:background .15s}
#continue-btn{background:#1d4ed8;color:#fff}
#continue-btn:hover{background:#2563eb}
#draw-btn{background:#7c3aed;color:#fff}
#draw-btn:hover{background:#8b5cf6}
#recrop-btn{background:#2a2a2a;color:#bbb;border:1px solid #3a3a3a}
#recrop-btn:hover{background:#333;color:#ccc}
#clearmarks-btn{background:#2a2a2a;color:#bbb;border:1px solid #3a3a3a}
#clearmarks-btn:hover{background:#333;color:#ccc}
#clear-btn{background:#2a2a2a;color:#999;border:1px solid #3a3a3a}
#clear-btn:hover{background:#333}
#cancel-btn{background:#2a2a2a;color:#999;border:1px solid #3a3a3a}
#cancel-btn:hover{background:#333}
</style>
</head>
<body>

<nav id="nav"></nav>

<div id="main">
  <div id="issue-header">
    <div id="issue-id"></div>
    <input type="text" id="edit-title" placeholder="Issue title">
    <div id="header-row2">
      <select id="edit-severity">
        <option value="S0 &ndash; Blocker">S0 &ndash; Blocker</option>
        <option value="S1 &ndash; Critical">S1 &ndash; Critical</option>
        <option value="S2 &ndash; Major">S2 &ndash; Major</option>
        <option value="S3 &ndash; Minor">S3 &ndash; Minor</option>
      </select>
      <div id="progress-line"></div>
    </div>
  </div>

  <div id="screenshot-section">
    <div id="main-shot">
      <img id="main-shot-img" src="" alt="">
      <div id="main-shot-empty">No screenshot selected &mdash; choose from the strip &rarr;</div>
    </div>
    <div id="thumb-strip"></div>
  </div>

  <div id="no-screenshots" style="display:none">No screenshots extracted for this issue. Run extract_frames.py first.</div>

  <!-- Banner shown when viewing a borrowed issue's frames -->
  <div id="source-bar">
    Showing frames from <strong id="source-vid-label"></strong>
    &mdash; will be saved as <span id="target-vid-label"></span>'s screenshot
    <button id="source-reset-btn">&times; Use own frames</button>
  </div>

  <!-- Selector row -->
  <div id="other-issue-row">
    <span>Browse frames from another issue:</span>
    <select id="other-issue-select">
      <option value="">&mdash; select &mdash;</option>
    </select>
  </div>

  <div id="fields-section">
    <div class="field-group">
      <label for="edit-observed">Observed behavior</label>
      <textarea id="edit-observed" rows="4" placeholder="Observed behavior..."></textarea>
    </div>
    <div class="field-group">
      <label for="edit-expected">Expected behavior</label>
      <textarea id="edit-expected" rows="3" placeholder="Expected behavior..."></textarea>
    </div>
    <div class="field-group">
      <label for="edit-notes">Notes</label>
      <textarea id="edit-notes" rows="3" placeholder="Notes..."></textarea>
    </div>
    <div class="field-group">
      <label for="edit-metadata">Metadata</label>
      <input type="text" id="edit-metadata" placeholder="Roles: ... | Area: ... | ...">
    </div>
  </div>

  <div id="action-row">
    <button id="validate-btn">Validate &amp; Next &#8594;</button>
    <button id="skip-btn">Skip &mdash; no screenshot</button>
    <button id="split-btn">Split &#8645; Add slide B</button>
    <button id="merge-btn">Merge into&hellip;</button>
  </div>

  <div id="merge-row" style="display:none">
    Merge this issue into:
    <select id="merge-target-select"><option value="">— select target —</option></select>
    <button id="merge-confirm-btn">Confirm merge</button>
    <button id="merge-cancel-btn">Cancel</button>
  </div>

  <div id="slide-b-section" style="display:none">
    <div id="slide-b-header">Slide B &mdash; second screenshot</div>
    <div id="slide-b-shot-row">
      <div id="slide-b-main-shot">
        <img id="slide-b-main-img" src="" style="display:none" alt="">
        <div id="slide-b-main-shot-empty">Select a frame from the strip</div>
      </div>
      <div id="slide-b-strip"></div>
    </div>
    <div id="slide-b-no-screenshots" style="display:none;color:#555;padding:12px 0;font-size:13px">No screenshots extracted for this issue.</div>
    <button id="slide-b-clear-btn">&#10005; Remove slide B</button>
  </div>

  <div id="merged-banner" style="display:none">
    This issue is merged into <strong id="merged-into-vid"></strong> &mdash; it will not generate its own slide.
    <br><button id="unmerge-btn">Unmerge (restore as standalone slide)</button>
  </div>

  <div id="completion">
    <span style="color:#4ade80;font-weight:700">&#10003; All issues reviewed</span>
    &nbsp;&mdash;&nbsp;
    <span style="color:#888">Click any thumbnail to re-edit &bull; Run <code>python generate_pptx.py</code> to build the deck</span>
  </div>
</div>

<!-- Hover preview -->
<div id="hover-preview"><img id="hover-img" src="" alt=""></div>

<div id="lightbox">
  <div id="lb-top">
    <div id="lb-nav">
      <button id="lb-prev">&#8592;</button>
      <span id="lb-counter">1 / 1</span>
      <button id="lb-next">&#8594;</button>
    </div>
    <span id="lb-label"></span>
    <span id="lb-hint">Drag on the image to crop &bull; optional</span>
  </div>
  <canvas id="crop-canvas"></canvas>
  <div id="lb-actions">
    <button class="lb-btn" id="draw-btn">Draw &#8594;</button>
    <button class="lb-btn" id="recrop-btn">&#8592; Re-crop</button>
    <button class="lb-btn" id="clearmarks-btn">Clear marks</button>
    <button class="lb-btn" id="continue-btn">Continue &#8594;</button>
    <button class="lb-btn" id="clear-btn">Clear crop</button>
    <button class="lb-btn" id="cancel-btn">&#10005; Cancel</button>
  </div>
</div>

<script>
const ISSUES = __ISSUES_JSON__;
let selections = __STATE_JSON__;

let currentIdx     = 0;
let currentShotIdx = 0;
let sourceIssueIdx = 0;   // which issue's frames are displayed (may differ from currentIdx)

// Pending screenshot (selected on this page, not yet persisted)
let pendingPath  = null;
let pendingCrop  = null;
let pendingPathB = null;
let pendingCropB = null;

// Lightbox state
let lbPath    = null;
let lbImage   = new Image();
let lbMode    = 'crop';   // 'crop' | 'draw'
let lbContext = 'a';      // 'a' = main slide, 'b' = slide B

// Crop state
let cropStart = null, cropEnd = null, dragging = false;

// Draw state
let drawCrop      = null; // crop in natural-image coords, locked when entering draw mode
let penStrokes    = [];   // array of [{x,y}] arrays
let penDrawing    = false;
let currentStroke = null;

// Hover state
let hoverTimer = null;

const canvas = document.getElementById('crop-canvas');
const ctx    = canvas.getContext('2d');

// ── Helpers ──────────────────────────────────────────────────────────────

function getStatus(vid) {
  if (!(vid in selections)) return 'pending';
  const sel = selections[vid];
  if (sel && sel.merged_into) return 'merged';
  if (!sel || !sel.path) return 'skipped';
  return 'selected';
}

function pendingCount() {
  return ISSUES.filter(i => !(i.id in selections)).length;
}

function nextPendingIdx() {
  for (let i = currentIdx + 1; i < ISSUES.length; i++)
    if (!(ISSUES[i].id in selections)) return i;
  for (let i = 0; i < currentIdx; i++)
    if (!(ISSUES[i].id in selections)) return i;
  return -1;
}

function labelFromPath(path) {
  const parts    = path.split('/');
  const file     = parts[parts.length - 1];
  const tsFolder = parts[parts.length - 2];
  return tsFolder + ' / ' + file.replace('.jpg', '');
}

// ── Nav ──────────────────────────────────────────────────────────────────

function renderNav() {
  const nav = document.getElementById('nav');
  nav.innerHTML = '';
  ISSUES.forEach((issue, idx) => {
    const btn = document.createElement('button');
    btn.className = 'chip ' + getStatus(issue.id);
    if (idx === currentIdx) btn.classList.add('active');
    btn.textContent = issue.id;
    btn.title = issue.title;
    btn.onclick = () => navigateTo(idx);
    nav.appendChild(btn);
  });
}

// ── Main shot ────────────────────────────────────────────────────────────

function updateMainShot() {
  const img     = document.getElementById('main-shot-img');
  const emptyEl = document.getElementById('main-shot-empty');
  if (pendingPath) {
    img.src              = '/images?path=' + encodeURIComponent(pendingPath);
    img.style.display    = '';
    emptyEl.style.display = 'none';
  } else {
    img.src              = '';
    img.style.display    = 'none';
    emptyEl.style.display = '';
  }
}

// ── Thumb strip ──────────────────────────────────────────────────────────

function renderThumbStrip(issueIdx) {
  const stripEl = document.getElementById('thumb-strip');
  const noSSEl  = document.getElementById('no-screenshots');
  stripEl.innerHTML = '';

  const shots = (ISSUES[issueIdx] && ISSUES[issueIdx].screenshots) || [];
  if (shots.length === 0) {
    noSSEl.style.display  = 'block';
    stripEl.style.display = 'none';
    return;
  }

  noSSEl.style.display  = 'none';
  stripEl.style.display = '';

  shots.forEach((path, shotIdx) => {
    const label = labelFromPath(path);
    const item  = document.createElement('div');
    item.className = 'strip-item' + (path === pendingPath ? ' active' : '');
    item.innerHTML =
      '<img src="/images?path=' + encodeURIComponent(path) + '" alt="' + label + '" loading="lazy">' +
      '<div class="strip-label">' + label + '</div>';

    item.onclick = () => {
      pendingPath    = path;
      pendingCrop    = null;
      currentShotIdx = shotIdx;
      updateMainShot();
      stripEl.querySelectorAll('.strip-item').forEach(el => el.classList.remove('active'));
      item.classList.add('active');
    };

    // Hover preview
    item.addEventListener('mouseenter', e => {
      hoverTimer = setTimeout(() => showHoverPreview(path, e), 300);
    });
    item.addEventListener('mouseleave', () => {
      clearTimeout(hoverTimer);
      hideHoverPreview();
    });
    item.addEventListener('mousemove', e => {
      if (document.getElementById('hover-preview').style.display !== 'none') {
        positionHoverPreview(e);
      }
    });

    stripEl.appendChild(item);
  });
}

// ── Issue panel ───────────────────────────────────────────────────────────

function renderIssue() {
  const issue = ISSUES[currentIdx];
  const vid   = issue.id;
  const sel   = selections[vid];
  const done  = ISSUES.length - pendingCount();

  document.getElementById('issue-id').textContent       = issue.id;
  document.getElementById('progress-line').textContent  = done + ' of ' + ISSUES.length + ' reviewed';

  // Title
  document.getElementById('edit-title').value =
    (sel && sel.title != null) ? sel.title : (issue.title || '');

  // Severity — ensure the value exists as an option before selecting it
  const sevEl  = document.getElementById('edit-severity');
  const sevVal = (sel && sel.severity != null) ? sel.severity : (issue.severity || '');
  let found = false;
  for (let i = 0; i < sevEl.options.length; i++) {
    if (sevEl.options[i].value === sevVal) { found = true; break; }
  }
  if (!found && sevVal) {
    const opt       = document.createElement('option');
    opt.value       = sevVal;
    opt.textContent = sevVal;
    sevEl.insertBefore(opt, sevEl.firstChild);
  }
  sevEl.value = sevVal;

  // Text fields
  document.getElementById('edit-observed').value =
    (sel && sel.observed != null) ? sel.observed : (issue.observed || '');
  document.getElementById('edit-expected').value =
    (sel && sel.expected != null) ? sel.expected : (issue.expected || '');
  document.getElementById('edit-notes').value =
    (sel && sel.notes != null) ? sel.notes : (issue.notes || '');

  // Metadata: assemble default from parts, or use saved override
  const metaParts = [];
  if (issue.affected_roles) metaParts.push('Roles: ' + issue.affected_roles);
  if (issue.affected_area)  metaParts.push('Area: '  + issue.affected_area);
  if (issue.timestamps)     metaParts.push(issue.timestamps);
  document.getElementById('edit-metadata').value =
    (sel && sel.metadata != null) ? sel.metadata : metaParts.join(' | ');

  // Screenshot pending state
  if (sel && sel.path) {
    pendingPath = sel.path;
    pendingCrop = sel.crop || null;
    // Sync currentShotIdx to the selected path in the current issue's shots
    const shots = issue.screenshots || [];
    const idx   = shots.indexOf(pendingPath);
    currentShotIdx = idx >= 0 ? idx : 0;
  } else {
    pendingPath    = null;
    pendingCrop    = null;
    currentShotIdx = 0;
  }

  sourceIssueIdx = currentIdx;
  renderThumbStrip(currentIdx);
  updateMainShot();

  // Merged state
  const isMerged = sel && sel.merged_into;
  document.getElementById('merged-banner').style.display = isMerged ? '' : 'none';
  if (isMerged) document.getElementById('merged-into-vid').textContent = sel.merged_into;
  document.getElementById('validate-btn').disabled = !!isMerged;
  document.getElementById('skip-btn').disabled     = !!isMerged;
  document.getElementById('split-btn').disabled    = !!isMerged;
  document.getElementById('merge-btn').disabled    = !!isMerged;

  // Split state
  const hasSlideB = sel && typeof sel === 'object' && sel.slide_b;
  const slideBSection = document.getElementById('slide-b-section');
  if (hasSlideB) {
    slideBSection.style.display = '';
    document.getElementById('split-btn').textContent = 'Split \u21c5 Remove slide B';
    document.getElementById('split-btn').classList.add('active');
    pendingPathB = sel.slide_b.path || null;
    pendingCropB = sel.slide_b.crop || null;
    renderSlideBStrip(pendingPathB);
    updateSlideBMainShot();
  } else {
    slideBSection.style.display = 'none';
    document.getElementById('split-btn').textContent = 'Split \u21c5 Add slide B';
    document.getElementById('split-btn').classList.remove('active');
    pendingPathB = null;
    pendingCropB = null;
  }

  const completionEl = document.getElementById('completion');
  if (pendingCount() === 0) {
    completionEl.classList.add('visible');
  } else {
    completionEl.classList.remove('visible');
  }
}

function navigateTo(idx) {
  currentIdx     = idx;
  sourceIssueIdx = idx;
  lbContext = 'a';
  document.getElementById('other-issue-select').value = '';
  document.getElementById('source-bar').style.display = 'none';
  document.getElementById('merge-row').style.display  = 'none';
  renderNav();
  renderIssue();
}

// ── Slide B strip ─────────────────────────────────────────────────────────

function updateSlideBMainShot() {
  const img     = document.getElementById('slide-b-main-img');
  const emptyEl = document.getElementById('slide-b-main-shot-empty');
  if (pendingPathB) {
    img.src = '/images?path=' + encodeURIComponent(pendingPathB);
    img.style.display    = '';
    emptyEl.style.display = 'none';
  } else {
    img.src = '';
    img.style.display    = 'none';
    emptyEl.style.display = '';
  }
}

function renderSlideBStrip(selectedPath) {
  const stripEl = document.getElementById('slide-b-strip');
  const noSSEl  = document.getElementById('slide-b-no-screenshots');
  stripEl.innerHTML = '';
  const shots = (ISSUES[currentIdx] && ISSUES[currentIdx].screenshots) || [];
  if (shots.length === 0) { noSSEl.style.display = 'block'; return; }
  noSSEl.style.display = 'none';
  shots.forEach((path, shotIdx) => {
    const label = labelFromPath(path);
    const item  = document.createElement('div');
    item.className = 'strip-item' + (path === selectedPath ? ' slide-b-active' : '');
    item.innerHTML =
      '<img src="/images?path=' + encodeURIComponent(path) + '" loading="lazy" alt="">' +
      '<div class="strip-label">' + label + '</div>';
    item.onclick = () => { lbContext = 'b'; openLightbox(shotIdx); };
    stripEl.appendChild(item);
  });
}

// ── Hover preview ─────────────────────────────────────────────────────────

function showHoverPreview(path, e) {
  document.getElementById('hover-img').src = '/images?path=' + encodeURIComponent(path);
  positionHoverPreview(e);
  document.getElementById('hover-preview').style.display = 'block';
}

function positionHoverPreview(e) {
  const preview = document.getElementById('hover-preview');
  const pw = 480;
  let px = e.clientX + 16;
  if (px + pw > window.innerWidth - 10) px = e.clientX - pw - 16;
  let py = e.clientY - 100;
  py = Math.max(10, Math.min(py, window.innerHeight - 300));
  preview.style.left = px + 'px';
  preview.style.top  = py + 'px';
}

function hideHoverPreview() {
  const preview = document.getElementById('hover-preview');
  preview.style.display = 'none';
  document.getElementById('hover-img').src = '';
}

// ── Lightbox ──────────────────────────────────────────────────────────────

function resetLightboxState() {
  cropStart = cropEnd = null;
  dragging  = false;
  drawCrop      = null;
  penStrokes    = [];
  penDrawing    = false;
  currentStroke = null;
  lbMode = 'crop';
}

function openLightbox(shotIdx) {
  clearTimeout(hoverTimer);
  hideHoverPreview();

  const shots = ISSUES[sourceIssueIdx].screenshots;
  currentShotIdx = shotIdx;
  lbPath = shots[currentShotIdx];

  resetLightboxState();

  document.getElementById('lb-label').textContent   = labelFromPath(lbPath);
  document.getElementById('lb-counter').textContent = (currentShotIdx + 1) + ' / ' + shots.length;

  const showNav = shots.length > 1;
  document.getElementById('lb-prev').style.display = showNav ? '' : 'none';
  document.getElementById('lb-next').style.display = showNav ? '' : 'none';

  updateModeUI();
  document.getElementById('lightbox').classList.add('open');

  lbImage = new Image();
  lbImage.onload = () => {
    const mw = window.innerWidth  * 0.88;
    const mh = window.innerHeight * 0.70;
    const sc = Math.min(mw / lbImage.naturalWidth, mh / lbImage.naturalHeight, 1);
    canvas.width  = Math.round(lbImage.naturalWidth  * sc);
    canvas.height = Math.round(lbImage.naturalHeight * sc);
    drawCropMode();
  };
  lbImage.src = '/images?path=' + encodeURIComponent(lbPath);
}

function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
  lbPath = null;
  resetLightboxState();
}

function navigateLightbox(delta) {
  const shots = ISSUES[sourceIssueIdx].screenshots;
  if (shots.length < 2) return;
  openLightbox((currentShotIdx + delta + shots.length) % shots.length);
}

// ── Mode state machine ────────────────────────────────────────────────────

function updateModeUI() {
  const inDraw = lbMode === 'draw';

  document.getElementById('clear-btn').style.display       = inDraw ? 'none' : '';
  document.getElementById('recrop-btn').style.display      = inDraw ? '' : 'none';
  document.getElementById('clearmarks-btn').style.display  = inDraw ? '' : 'none';

  canvas.style.borderColor = inDraw ? '#7c3aed' : '#333';

  document.getElementById('lb-hint').textContent = inDraw
    ? 'Draw on the cropped area \u2022 pen tool'
    : 'Drag on the image to crop \u2022 optional';

  updateDrawBtn();
}

function updateDrawBtn() {
  document.getElementById('draw-btn').style.display =
    (lbMode === 'crop' && getCrop() !== null) ? '' : 'none';
}

function enterDrawMode() {
  const crop = getCrop();
  if (!crop) return;
  drawCrop = crop;
  lbMode   = 'draw';

  const sw = crop.right - crop.left;
  const sh = crop.bottom - crop.top;
  const mw = window.innerWidth  * 0.90;
  const mh = window.innerHeight * 0.70;
  const sc = Math.min(mw / sw, mh / sh, 1);
  canvas.width  = Math.round(sw * sc);
  canvas.height = Math.round(sh * sc);

  updateModeUI();
  drawDrawMode();
}

function enterCropMode() {
  lbMode        = 'crop';
  drawCrop      = null;
  penStrokes    = [];
  penDrawing    = false;
  currentStroke = null;

  const mw = window.innerWidth  * 0.88;
  const mh = window.innerHeight * 0.70;
  const sc = Math.min(mw / lbImage.naturalWidth, mh / lbImage.naturalHeight, 1);
  canvas.width  = Math.round(lbImage.naturalWidth  * sc);
  canvas.height = Math.round(lbImage.naturalHeight * sc);

  updateModeUI();
  drawCropMode();
}

// ── Canvas rendering ──────────────────────────────────────────────────────

function drawCropMode() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(lbImage, 0, 0, canvas.width, canvas.height);

  if (cropStart && cropEnd) {
    const x = Math.min(cropStart.x, cropEnd.x);
    const y = Math.min(cropStart.y, cropEnd.y);
    const w = Math.abs(cropEnd.x - cropStart.x);
    const h = Math.abs(cropEnd.y - cropStart.y);

    ctx.save();
    ctx.fillStyle = 'rgba(0,0,0,.55)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.clearRect(x, y, w, h);
    const imgScaleX = lbImage.naturalWidth  / canvas.width;
    const imgScaleY = lbImage.naturalHeight / canvas.height;
    ctx.drawImage(lbImage, x * imgScaleX, y * imgScaleY, w * imgScaleX, h * imgScaleY, x, y, w, h);

    ctx.strokeStyle = '#60a5fa';
    ctx.lineWidth   = 2;
    ctx.strokeRect(x, y, w, h);

    const hs = 7;
    ctx.fillStyle = '#60a5fa';
    [[x,y],[x+w,y],[x,y+h],[x+w,y+h]].forEach(([hx,hy]) =>
      ctx.fillRect(hx - hs/2, hy - hs/2, hs, hs));
    ctx.restore();
  }
}

function drawDrawMode() {
  const crop = drawCrop;
  if (!crop) return;

  const { left, top, right, bottom } = crop;
  const sw = right - left;
  const sh = bottom - top;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(lbImage, left, top, sw, sh, 0, 0, canvas.width, canvas.height);

  penStrokes.forEach(stroke => {
    if (stroke.length < 2) return;
    ctx.beginPath();
    ctx.strokeStyle = '#ef4444';
    ctx.lineWidth   = 3;
    ctx.lineCap     = 'round';
    ctx.lineJoin    = 'round';
    ctx.moveTo(stroke[0].x, stroke[0].y);
    for (let i = 1; i < stroke.length; i++) ctx.lineTo(stroke[i].x, stroke[i].y);
    ctx.stroke();
  });
}

// ── Canvas events ─────────────────────────────────────────────────────────

canvas.addEventListener('mousedown', e => {
  const r  = canvas.getBoundingClientRect();
  const pt = { x: e.clientX - r.left, y: e.clientY - r.top };
  if (lbMode === 'draw') {
    penDrawing    = true;
    currentStroke = [pt];
    penStrokes.push(currentStroke);
  } else {
    cropStart = pt;
    cropEnd   = { ...pt };
    dragging  = true;
  }
});

canvas.addEventListener('mousemove', e => {
  const r  = canvas.getBoundingClientRect();
  const pt = { x: e.clientX - r.left, y: e.clientY - r.top };
  if (lbMode === 'draw') {
    if (!penDrawing) return;
    currentStroke.push(pt);
    drawDrawMode();
  } else {
    if (!dragging) return;
    cropEnd = pt;
    drawCropMode();
  }
});

window.addEventListener('mouseup', () => {
  dragging      = false;
  penDrawing    = false;
  currentStroke = null;
  if (lbMode === 'crop') updateDrawBtn();
});

// ── Crop helper ───────────────────────────────────────────────────────────

function getCrop() {
  if (!cropStart || !cropEnd) return null;
  const sx     = lbImage.naturalWidth  / canvas.width;
  const sy     = lbImage.naturalHeight / canvas.height;
  const left   = Math.round(Math.min(cropStart.x, cropEnd.x) * sx);
  const top    = Math.round(Math.min(cropStart.y, cropEnd.y) * sy);
  const right  = Math.round(Math.max(cropStart.x, cropEnd.x) * sx);
  const bottom = Math.round(Math.max(cropStart.y, cropEnd.y) * sy);
  if (right - left < 10 || bottom - top < 10) return null;
  return { left, top, right, bottom };
}

// ── Composite export ──────────────────────────────────────────────────────

async function exportAnnotatedImage() {
  const crop = drawCrop;
  if (!crop) return null;
  const { left, top, right, bottom } = crop;
  const sw = right - left;
  const sh = bottom - top;

  const offscreen = new OffscreenCanvas(sw, sh);
  const offCtx    = offscreen.getContext('2d');
  offCtx.drawImage(lbImage, left, top, sw, sh, 0, 0, sw, sh);

  if (penStrokes.length > 0) {
    const scaleX = sw / canvas.width;
    const scaleY = sh / canvas.height;
    offCtx.strokeStyle = '#ef4444';
    offCtx.lineCap     = 'round';
    offCtx.lineJoin    = 'round';
    offCtx.lineWidth   = 3 * Math.max(scaleX, scaleY);
    penStrokes.forEach(stroke => {
      if (stroke.length < 2) return;
      offCtx.beginPath();
      offCtx.moveTo(stroke[0].x * scaleX, stroke[0].y * scaleY);
      for (let i = 1; i < stroke.length; i++)
        offCtx.lineTo(stroke[i].x * scaleX, stroke[i].y * scaleY);
      offCtx.stroke();
    });
  }

  const blob = await offscreen.convertToBlob({ type: 'image/jpeg', quality: 0.92 });
  return new Promise(resolve => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.readAsDataURL(blob);
  });
}

// ── Lightbox: Continue (stores in pending — does NOT save or advance) ──────

document.getElementById('continue-btn').onclick = async () => {
  if (lbMode === 'draw' && penStrokes.length > 0) {
    const vid = ISSUES[currentIdx].id;
    const b64 = await exportAnnotatedImage();
    const res = await fetch('/save-image', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ vid, image_b64: b64 }),
    });
    const { path: annotatedPath } = await res.json();
    if (lbContext === 'b') {
      pendingPathB = annotatedPath; pendingCropB = null;
    } else {
      pendingPath = annotatedPath; pendingCrop = null;
    }
  } else {
    if (lbContext === 'b') {
      pendingPathB = lbPath; pendingCropB = drawCrop || getCrop();
    } else {
      pendingPath = lbPath; pendingCrop = drawCrop || getCrop();
    }
  }
  if (lbContext === 'b') {
    updateSlideBMainShot();
    renderSlideBStrip(pendingPathB);
  } else {
    updateMainShot();
    const shots = (ISSUES[sourceIssueIdx] && ISSUES[sourceIssueIdx].screenshots) || [];
    document.querySelectorAll('#thumb-strip .strip-item').forEach((item, idx) => {
      item.classList.toggle('active', shots[idx] === pendingPath);
    });
  }
  closeLightbox();
  lbContext = 'a';
};

document.getElementById('draw-btn').onclick = enterDrawMode;

document.getElementById('recrop-btn').onclick = enterCropMode;

document.getElementById('clearmarks-btn').onclick = () => {
  penStrokes    = [];
  currentStroke = null;
  drawDrawMode();
};

document.getElementById('clear-btn').onclick = () => {
  cropStart = cropEnd = null;
  updateDrawBtn();
  drawCropMode();
};

document.getElementById('cancel-btn').onclick = closeLightbox;

document.getElementById('lb-prev').onclick = () => navigateLightbox(-1);
document.getElementById('lb-next').onclick = () => navigateLightbox(1);

// ── Main shot click → open lightbox ───────────────────────────────────────

document.getElementById('main-shot').onclick = () => {
  if (!pendingPath) return;
  const shots = (ISSUES[sourceIssueIdx] && ISSUES[sourceIssueIdx].screenshots) || [];
  const idx   = shots.indexOf(pendingPath);
  openLightbox(idx >= 0 ? idx : currentShotIdx);
};

// ── Validate & Next ───────────────────────────────────────────────────────

document.getElementById('validate-btn').onclick = async () => {
  const vid     = ISSUES[currentIdx].id;
  const payload = {
    vid,
    path:     pendingPath || null,
    crop:     pendingCrop || null,
    title:    document.getElementById('edit-title').value,
    severity: document.getElementById('edit-severity').value,
    observed: document.getElementById('edit-observed').value,
    expected: document.getElementById('edit-expected').value,
    notes:    document.getElementById('edit-notes').value,
    metadata: document.getElementById('edit-metadata').value,
  };
  const slideBSection = document.getElementById('slide-b-section');
  if (slideBSection.style.display !== 'none') {
    payload.slide_b = { path: pendingPathB || null, crop: pendingCropB || null };
  }
  await fetch('/save', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  });
  selections[vid] = {
    path:     payload.path,
    crop:     payload.crop,
    title:    payload.title,
    severity: payload.severity,
    observed: payload.observed,
    expected: payload.expected,
    notes:    payload.notes,
    metadata: payload.metadata,
  };
  if (payload.slide_b) selections[vid].slide_b = payload.slide_b;
  const next = nextPendingIdx();
  navigateTo(next === -1 ? currentIdx : next);
};

// ── Skip ──────────────────────────────────────────────────────────────────

document.getElementById('skip-btn').onclick = async () => {
  const vid     = ISSUES[currentIdx].id;
  const payload = {
    vid,
    path:     null,
    crop:     null,
    title:    document.getElementById('edit-title').value,
    severity: document.getElementById('edit-severity').value,
    observed: document.getElementById('edit-observed').value,
    expected: document.getElementById('edit-expected').value,
    notes:    document.getElementById('edit-notes').value,
    metadata: document.getElementById('edit-metadata').value,
  };
  // Preserve existing slide_b when skipping
  const existing = selections[vid];
  if (existing && typeof existing === 'object' && existing.slide_b) {
    payload.slide_b = existing.slide_b;
  }
  await fetch('/save', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  });
  selections[vid] = { ...payload };
  const next = nextPendingIdx();
  navigateTo(next === -1 ? currentIdx : next);
};

// ── Split ─────────────────────────────────────────────────────────────────

document.getElementById('split-btn').onclick = async () => {
  const vid = ISSUES[currentIdx].id;
  const slideBSection = document.getElementById('slide-b-section');
  const isActive = slideBSection.style.display !== 'none';
  if (isActive) {
    slideBSection.style.display = 'none';
    document.getElementById('split-btn').textContent = 'Split \u21c5 Add slide B';
    document.getElementById('split-btn').classList.remove('active');
    pendingPathB = null; pendingCropB = null;
    await fetch('/save', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ vid, remove_slide_b: true }),
    });
    const sel = selections[vid];
    if (sel && typeof sel === 'object') delete sel.slide_b;
  } else {
    slideBSection.style.display = '';
    document.getElementById('split-btn').textContent = 'Split \u21c5 Remove slide B';
    document.getElementById('split-btn').classList.add('active');
    pendingPathB = null; pendingCropB = null;
    renderSlideBStrip(null);
    updateSlideBMainShot();
  }
};

document.getElementById('slide-b-clear-btn').onclick = () => {
  document.getElementById('split-btn').click();
};

document.getElementById('slide-b-main-shot').onclick = () => {
  if (!pendingPathB) return;
  const shots = (ISSUES[currentIdx] && ISSUES[currentIdx].screenshots) || [];
  const idx   = shots.indexOf(pendingPathB);
  lbContext = 'b';
  openLightbox(idx >= 0 ? idx : currentShotIdx);
};

// ── Merge ─────────────────────────────────────────────────────────────────

function populateMergeTargetSelect() {
  const sel = document.getElementById('merge-target-select');
  sel.innerHTML = '<option value="">— select target —</option>';
  const currentVid = ISSUES[currentIdx].id;
  ISSUES.forEach(issue => {
    if (issue.id === currentVid) return;
    const s = selections[issue.id];
    if (s && s.merged_into) return;
    const opt = document.createElement('option');
    opt.value = issue.id;
    opt.textContent = issue.id + ' \u2014 ' + issue.title;
    sel.appendChild(opt);
  });
}

document.getElementById('merge-btn').onclick = () => {
  const mergeRow = document.getElementById('merge-row');
  const isVisible = mergeRow.style.display === 'flex';
  mergeRow.style.display = isVisible ? 'none' : 'flex';
  if (!isVisible) populateMergeTargetSelect();
};

document.getElementById('merge-confirm-btn').onclick = async () => {
  const targetVid = document.getElementById('merge-target-select').value;
  if (!targetVid) return;
  const vid = ISSUES[currentIdx].id;
  await fetch('/save', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ vid, merged_into: targetVid }),
  });
  selections[vid] = { merged_into: targetVid };
  document.getElementById('merge-row').style.display = 'none';
  renderNav();
  renderIssue();
  const next = nextPendingIdx();
  if (next !== -1) navigateTo(next);
};

document.getElementById('merge-cancel-btn').onclick = () => {
  document.getElementById('merge-row').style.display = 'none';
};

document.getElementById('unmerge-btn').onclick = async () => {
  const vid = ISSUES[currentIdx].id;
  await fetch('/save', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ vid, path: null, crop: null }),
  });
  selections[vid] = { path: null, crop: null };
  renderNav();
  renderIssue();
};

// ── Source-issue selector ─────────────────────────────────────────────────

function populateOtherIssueSelect() {
  const sel = document.getElementById('other-issue-select');
  sel.innerHTML = '<option value="">&mdash; select &mdash;</option>';
  ISSUES.forEach((issue, idx) => {
    const opt = document.createElement('option');
    opt.value = idx;
    opt.textContent = issue.id + ' \u2014 ' + issue.title;
    sel.appendChild(opt);
  });
}

function resetSourceIssue() {
  sourceIssueIdx = currentIdx;
  document.getElementById('other-issue-select').value = '';
  document.getElementById('source-bar').style.display = 'none';
  renderThumbStrip(currentIdx);
}

document.getElementById('other-issue-select').onchange = function() {
  const val = this.value;
  if (!val) {
    resetSourceIssue();
  } else {
    sourceIssueIdx = parseInt(val);
    document.getElementById('source-vid-label').textContent = ISSUES[sourceIssueIdx].id;
    document.getElementById('target-vid-label').textContent = ISSUES[currentIdx].id;
    document.getElementById('source-bar').style.display = 'flex';
    renderThumbStrip(sourceIssueIdx);
  }
};

document.getElementById('source-reset-btn').onclick = resetSourceIssue;

// ── Keyboard ──────────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeLightbox(); return; }
  const lb = document.getElementById('lightbox');
  if (!lb.classList.contains('open')) return;
  if (e.key === 'ArrowLeft')  navigateLightbox(-1);
  if (e.key === 'ArrowRight') navigateLightbox(1);
});

// ── Init ──────────────────────────────────────────────────────────────────

(function init() {
  let start = 0;
  for (let i = 0; i < ISSUES.length; i++) {
    if (!(ISSUES[i].id in selections)) { start = i; break; }
  }
  populateOtherIssueSelect();
  navigateTo(start);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ReviewHandler(BaseHTTPRequestHandler):

    def __init__(self, issues, config, *args, **kwargs):
        self.issues = issues
        self.config = config
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # suppress request logs
        pass

    # ── Routing ──

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_html()
        elif parsed.path == "/images":
            qs   = parse_qs(parsed.query)
            path = qs.get("path", [None])[0]
            if path:
                self._serve_image(unquote(path))
            else:
                self.send_error(400)
        elif parsed.path == "/state":
            self._send_json(load_selections(self.config["selections_path"]))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/save":
            self._handle_save()
        elif self.path == "/save-image":
            self._handle_save_image()
        else:
            self.send_error(404)

    # ── Handlers ──

    def _serve_html(self):
        sels = load_selections(self.config["selections_path"])

        issues_data = []
        for issue in self.issues:
            shots = get_screenshots(self.config["output_dir"], issue["id"])
            issues_data.append({**issue, "screenshots": shots})

        html = HTML.replace("__ISSUES_JSON__", json.dumps(issues_data, ensure_ascii=False))
        html = html.replace("__STATE_JSON__",  json.dumps(sels,        ensure_ascii=False))
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_image(self, rel_path):
        abs_path     = os.path.abspath(rel_path)
        project_root = os.path.abspath(".")
        if not abs_path.startswith(project_root):
            self.send_error(403)
            return
        if not os.path.isfile(abs_path):
            self.send_error(404)
            return
        with open(abs_path, "rb") as f:
            data = f.read()
        self._send_bytes(data, "image/jpeg")

    def _handle_save(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))
        sels   = load_selections(self.config["selections_path"])
        vid    = body["vid"]

        if "merged_into" in body:
            sels[vid] = {"merged_into": body["merged_into"]}
        elif body.get("remove_slide_b"):
            existing = sels.get(vid)
            if isinstance(existing, dict):
                existing.pop("slide_b", None)
                sels[vid] = existing
        else:
            TEXT_FIELDS = ("title", "severity", "observed", "expected", "notes", "metadata")
            entry = {"path": body.get("path"), "crop": body.get("crop")}
            for f in TEXT_FIELDS:
                if f in body:
                    entry[f] = body[f]
            if "slide_b" in body and isinstance(body["slide_b"], dict):
                entry["slide_b"] = body["slide_b"]
            elif "slide_b" not in body:
                # Preserve existing slide_b when not explicitly sent
                existing = sels.get(vid)
                if isinstance(existing, dict) and "slide_b" in existing:
                    entry["slide_b"] = existing["slide_b"]
            sels[vid] = entry

        save_selections(self.config["selections_path"], sels)
        self._send_json({"ok": True})

    def _handle_save_image(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length))
        vid     = body["vid"]
        img_b64 = body["image_b64"]

        vid_dir  = Path(self.config["output_dir"]) / vid
        vid_dir.mkdir(parents=True, exist_ok=True)
        out_path = vid_dir / "annotated.jpg"
        out_path.write_bytes(base64.b64decode(img_b64))

        self._send_json({"path": out_path.as_posix()})

    def _send_json(self, obj):
        self._send_bytes(json.dumps(obj).encode(), "application/json")

    def _send_bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(CONFIG_FILE):
        raise SystemExit(f"Config not found: {CONFIG_FILE}")

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    issues_path = config["issues_path"]
    if not os.path.exists(issues_path):
        raise SystemExit(f"Issues file not found: {issues_path}")

    config["output_dir"] = str(
        Path(config["output_dir"]) / Path(config["video_path"]).stem
    )

    issues = parse_issues(issues_path)
    print(f"Loaded {len(issues)} issues from {issues_path}")

    port    = config.get("server_port", 8765)
    handler = functools.partial(ReviewHandler, issues, config)
    server  = HTTPServer(("localhost", port), handler)

    url = f"http://localhost:{port}"
    print(f"Review server running at {url}")
    print("Press Ctrl+C to stop.\n")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
