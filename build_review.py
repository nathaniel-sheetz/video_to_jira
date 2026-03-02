#!/usr/bin/env python3
"""
build_review.py  —  Step 5: Screenshot review UI.

Starts a local HTTP server and opens the review tool in your browser.
  - Navigate between issues via the top nav bar
  - Click a thumbnail to open it full-size in a lightbox
  - Hover over a thumbnail for a quick 480px preview (300 ms delay)
  - Use ← → buttons (or arrow keys) to cycle frames within an issue
  - Drag on the image to set an optional crop region
  - Click "Draw →" (appears after a valid crop) to annotate with a pen
  - Click Continue to save your selection and advance to the next issue
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
# Markdown parser  (shared with extract_frames.py)
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
            "id": header.group(1),
            "title": header.group(2).strip(),
            "severity": "",
            "timestamps": "",
        }

        for line in lines[1:15]:
            m = re.match(r"-\s+\*\*(.+?):\*\*\s*(.*)", line)
            if m:
                key = m.group(1).strip().lower()
                val = m.group(2).strip()
                if key == "severity":
                    issue["severity"] = val
                elif key == "timestamps":
                    issue["timestamps"] = val

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
.chip.active{outline:2px solid #60a5fa;outline-offset:2px}

/* ── Main ── */
#main{margin-top:62px;padding:28px 28px 60px;max-width:1300px;margin-left:auto;margin-right:auto}
#issue-header{margin-bottom:22px}
#issue-id{font-size:12px;font-weight:700;color:#60a5fa;text-transform:uppercase;letter-spacing:.08em}
#issue-title{font-size:20px;font-weight:600;margin-top:4px;line-height:1.3}
#issue-severity{font-size:12px;color:#f87171;margin-top:5px}
#progress-line{font-size:12px;color:#666;margin-top:3px}

/* ── Thumbnails ── */
#thumbnails{display:flex;flex-wrap:wrap;gap:14px;margin-top:18px}
.thumb-item{cursor:pointer;border:2px solid #2a2a2a;border-radius:8px;overflow:hidden;transition:border-color .15s;position:relative;flex-shrink:0}
.thumb-item:hover{border-color:#60a5fa}
.thumb-item img{display:block;width:220px;height:138px;object-fit:cover;background:#222}
.thumb-label{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.72);font-size:10px;padding:4px 7px;color:#bbb}

#no-screenshots{color:#555;padding:20px 0;font-size:14px}

#skip-btn{margin-top:22px;padding:8px 20px;background:#262626;color:#888;border:1px solid #3a3a3a;border-radius:6px;cursor:pointer;font-size:13px;transition:background .15s}
#skip-btn:hover{background:#333;color:#ccc}

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

#source-bar{display:none;margin-top:12px;padding:8px 12px;background:#1a1a2e;border:1px solid #3b3b8a;border-radius:6px;font-size:12px;color:#818cf8;align-items:center;gap:8px}
#source-bar strong{color:#a5b4fc}
#source-reset-btn{margin-left:auto;background:none;border:1px solid #3b3b8a;color:#818cf8;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px}
#other-issue-row{margin-top:12px;display:flex;align-items:center;gap:10px}
#other-issue-row span{font-size:12px;color:#666}
#other-issue-select{background:#1f1f1f;color:#ccc;border:1px solid #3a3a3a;border-radius:5px;padding:5px 8px;font-size:12px;cursor:pointer;max-width:340px}
</style>
</head>
<body>

<nav id="nav"></nav>

<div id="main">
  <div id="issue-header">
    <div id="issue-id"></div>
    <div id="issue-title"></div>
    <div id="issue-severity"></div>
    <div id="progress-line"></div>
  </div>
  <div id="thumbnails"></div>
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
      <option value="">— select —</option>
    </select>
  </div>

  <button id="skip-btn">Skip — no screenshot for this issue</button>
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

// Lightbox state
let lbPath    = null;
let lbImage   = new Image();
let lbMode    = 'crop';   // 'crop' | 'draw'

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
  return selections[vid] === null ? 'skipped' : 'selected';
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

// ── Issue panel ───────────────────────────────────────────────────────────

function renderThumbnails(issueIdx) {
  const thumbsEl = document.getElementById('thumbnails');
  const noSSEl   = document.getElementById('no-screenshots');
  thumbsEl.innerHTML = '';

  const shots = (ISSUES[issueIdx].screenshots) || [];
  if (shots.length === 0) {
    noSSEl.style.display = 'block';
  } else {
    noSSEl.style.display = 'none';
    shots.forEach((path, shotIdx) => {
      const label = labelFromPath(path);

      const item = document.createElement('div');
      item.className = 'thumb-item';
      item.innerHTML =
        '<img src="/images?path=' + encodeURIComponent(path) + '" alt="' + label + '" loading="lazy">' +
        '<div class="thumb-label">' + label + '</div>';

      item.onclick = () => openLightbox(shotIdx);

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

      thumbsEl.appendChild(item);
    });
  }
}

function renderIssue() {
  const issue   = ISSUES[currentIdx];
  const pending = pendingCount();
  const done    = ISSUES.length - pending;

  document.getElementById('issue-id').textContent       = issue.id;
  document.getElementById('issue-title').textContent    = issue.title;
  document.getElementById('issue-severity').textContent = issue.severity;
  document.getElementById('progress-line').textContent  =
    `${done} of ${ISSUES.length} reviewed`;

  sourceIssueIdx = currentIdx;
  renderThumbnails(currentIdx);

  const completionEl = document.getElementById('completion');
  if (pending === 0) {
    completionEl.classList.add('visible');
  } else {
    completionEl.classList.remove('visible');
  }
}

function navigateTo(idx) {
  currentIdx = idx;
  sourceIssueIdx = idx;
  document.getElementById('other-issue-select').value = '';
  document.getElementById('source-bar').style.display = 'none';
  renderNav();
  renderIssue();
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
  document.getElementById('lb-counter').textContent = `${currentShotIdx + 1} / ${shots.length}`;

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

  // Crop-mode-only elements
  document.getElementById('clear-btn').style.display = inDraw ? 'none' : '';
  // draw-btn managed by updateDrawBtn (hidden in draw mode via that path)

  // Draw-mode-only elements
  document.getElementById('recrop-btn').style.display    = inDraw ? '' : 'none';
  document.getElementById('clearmarks-btn').style.display = inDraw ? '' : 'none';

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
  // Capture crop in natural-image coords BEFORE touching the canvas — getCrop()
  // uses canvas dimensions as scale factors, so resizing first would corrupt it.
  const crop = getCrop();
  if (!crop) return;
  drawCrop = crop;
  lbMode   = 'draw';

  // Resize canvas to match the crop's own aspect ratio (undistorted fill)
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

  // Restore canvas to full-image dimensions
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
  const crop = drawCrop;   // use locked natural-image coords, not getCrop()
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
  const crop = drawCrop;   // locked natural-image coords captured at enterDrawMode()
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

// ── Actions ───────────────────────────────────────────────────────────────

async function saveAndAdvance(vid, payload) {
  await fetch('/save', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  });
  selections[vid] = payload.skip ? null : { path: payload.path, crop: payload.crop };
  closeLightbox();
  const next = nextPendingIdx();
  navigateTo(next === -1 ? currentIdx : next);
}

document.getElementById('continue-btn').onclick = async () => {
  const vid = ISSUES[currentIdx].id;
  if (lbMode === 'draw' && penStrokes.length > 0) {
    const b64 = await exportAnnotatedImage();
    const res = await fetch('/save-image', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ vid, image_b64: b64 }),
    });
    const { path: annotatedPath } = await res.json();
    await saveAndAdvance(vid, { vid, path: annotatedPath, crop: null });
  } else {
    // In crop mode getCrop() is valid; in draw mode use the locked drawCrop
    await saveAndAdvance(vid, { vid, path: lbPath, crop: drawCrop || getCrop() });
  }
};

document.getElementById('draw-btn').onclick = enterDrawMode;

document.getElementById('recrop-btn').onclick = enterCropMode;

document.getElementById('clearmarks-btn').onclick = () => {
  penStrokes    = [];
  currentStroke = null;
  drawDrawMode();
};

document.getElementById('skip-btn').onclick = () => {
  const vid = ISSUES[currentIdx].id;
  saveAndAdvance(vid, { vid, skip: true });
};

document.getElementById('clear-btn').onclick = () => {
  cropStart = cropEnd = null;
  updateDrawBtn();
  drawCropMode();
};

document.getElementById('cancel-btn').onclick = closeLightbox;

document.getElementById('lb-prev').onclick = () => navigateLightbox(-1);
document.getElementById('lb-next').onclick = () => navigateLightbox(1);

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeLightbox(); return; }
  const lb = document.getElementById('lightbox');
  if (!lb.classList.contains('open')) return;
  if (e.key === 'ArrowLeft')  navigateLightbox(-1);
  if (e.key === 'ArrowRight') navigateLightbox(1);
});

// ── Source-issue selector ─────────────────────────────────────────────────

function populateOtherIssueSelect() {
  const sel = document.getElementById('other-issue-select');
  sel.innerHTML = '<option value="">— select —</option>';
  ISSUES.forEach((issue, idx) => {
    const opt = document.createElement('option');
    opt.value = idx;
    opt.textContent = issue.id + ' — ' + issue.title;
    sel.appendChild(opt);
  });
}

function resetSourceIssue() {
  sourceIssueIdx = currentIdx;
  document.getElementById('other-issue-select').value = '';
  document.getElementById('source-bar').style.display = 'none';
  renderThumbnails(currentIdx);
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
    renderThumbnails(sourceIssueIdx);
  }
};

document.getElementById('source-reset-btn').onclick = resetSourceIssue;

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
        if body.get("skip"):
            sels[vid] = None
        else:
            sels[vid] = {"path": body["path"], "crop": body.get("crop")}
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
