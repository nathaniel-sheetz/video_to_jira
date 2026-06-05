#!/usr/bin/env python3
"""
build_review.py — the review console (schema v2).

A local web console for the two human gates over a session's issues.json:

  Gate 3  confirm the issue is real      → Accept / Reject(+reason) / Edit
  Gate 5  confirm a screenshot per facet → pick one candidate frame per anchor

One card per issue. The evidence quote + @timestamp that justifies each issue is
co-located on the card (the gate-3 proof), and every anchor gets its own row of
candidate frames (the gate-5 pick). Every decision mutates issues.json in place
through issues_store — atomic write, schema-validated — so nothing lives only in
a browser tab. Keyboard-first: read the quote, press Enter, next.

Data layer is issues_store; this module owns the gate state-transitions
(accept/reject/edit/pick/skip) and the HTTP surface that drives them.

Usage:
    python build_review.py [path/to/issues.json]
    # falls back to config.json's issues_path, then ./issues.json
"""

from __future__ import annotations

import base64
import functools
import io
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import issues_store as st

CONFIG_FILE = "config.json"
KEEP_STATUSES = ("accepted", "edited")          # gate-3 outcomes that reach export
RESOLVED_FRAME = ("selected", "skipped")        # gate-5 outcomes for an anchor


# ---------------------------------------------------------------------------
# Gate state-transitions (pure — operate on a loaded doc, caller persists)
# ---------------------------------------------------------------------------

def visible_issues(doc):
    """Nav shows everything except merged_out tombstones."""
    return [i for i in doc["issues"] if i.get("status") != "merged_out"]


def _anchor(issue, anchor_id):
    for a in issue.get("anchors", []):
        if a.get("id") == anchor_id:
            return a
    raise KeyError(f"anchor not found: {anchor_id!r} on {issue.get('id')!r}")


def accept_issue(doc, key):
    iss = st.find_issue(doc, key)
    if iss is None:
        raise KeyError(f"issue not found: {key!r}")
    if iss.get("status") != "edited":     # an edited issue is already a keep state
        iss["status"] = "accepted"
    iss.pop("reject_reason", None)
    return iss


def reject_issue(doc, key, reason=None):
    iss = st.find_issue(doc, key)
    if iss is None:
        raise KeyError(f"issue not found: {key!r}")
    if reason is not None and reason not in st.REJECT_REASONS:
        raise ValueError(f"bad reject reason: {reason!r}")
    iss["status"] = "rejected"
    if reason:
        iss["reject_reason"] = reason
    else:
        iss.pop("reject_reason", None)
    return iss


def edit_issue(doc, key, fields):
    iss = st.find_issue(doc, key)
    if iss is None:
        raise KeyError(f"issue not found: {key!r}")
    clean = {k: v for k, v in fields.items() if k in st.EDITABLE_FIELDS}
    return st.apply_edit(iss, clean)


def pick_frame(doc, key, anchor_id, *, offset=None, path=None, crop=None, caption=None):
    """
    Record the human's chosen frame for one anchor (gate 5). Identify the frame by
    `offset` (resolved against the anchor's candidate_frames) or by an explicit
    `path` (e.g. a cropped/annotated export). Sets frame_status=selected.
    """
    iss = st.find_issue(doc, key)
    if iss is None:
        raise KeyError(f"issue not found: {key!r}")
    anc = _anchor(iss, anchor_id)

    if path is None:
        if offset is None:
            raise ValueError("pick_frame needs offset or path")
        match = next((c for c in anc.get("candidate_frames", []) if c["offset"] == offset), None)
        if match is None:
            raise ValueError(f"no candidate at offset {offset} on {anchor_id}")
        path = match["path"]

    anc["selected_frame"] = {
        "path": path,
        "offset": offset,
        "crop": crop,
        "caption": caption if caption is not None else anc.get("caption", ""),
    }
    anc["frame_status"] = "selected"
    return anc


def skip_anchor(doc, key, anchor_id):
    iss = st.find_issue(doc, key)
    if iss is None:
        raise KeyError(f"issue not found: {key!r}")
    anc = _anchor(iss, anchor_id)
    anc["selected_frame"] = None
    anc["frame_status"] = "skipped"
    return anc


# ---------------------------------------------------------------------------
# Derived state (chip colours, completion, progress)
# ---------------------------------------------------------------------------

def anchors_resolved(issue):
    return all(a.get("frame_status") in RESOLVED_FRAME for a in issue.get("anchors", []))


def frames_extracted(issue):
    """True once the frame pipeline has produced candidates for any anchor.
    Drives the gate-3 accept-advance: with no frames yet, Accept advances;
    with frames present, Accept stays so the reviewer picks one (gate 5)."""
    return any(a.get("candidate_frames") for a in issue.get("anchors", []))


def chip_state(issue):
    """The nav-chip state per the design's interaction-state table."""
    s = issue.get("status")
    if s == "rejected":
        return "rejected"
    if s == "proposed":
        return "proposed"
    if s in KEEP_STATUSES:
        if not anchors_resolved(issue):
            return "partial"            # accepted, some facets still unpicked  ◐
        return s                         # 'accepted' ● / 'edited' ●✎
    return "proposed"


def is_complete(issue):
    """Done = triaged AND (if kept) every anchor picked-or-skipped."""
    s = issue.get("status")
    if s in ("rejected", "merged_out"):
        return True
    if s in KEEP_STATUSES:
        return anchors_resolved(issue)
    return False                         # 'proposed' = not yet triaged


def session_progress(doc):
    vis = visible_issues(doc)
    kept = [i for i in vis if i.get("status") in KEEP_STATUSES]
    frames_total = sum(len(i.get("anchors", [])) for i in kept)
    frames_done = sum(1 for i in kept for a in i["anchors"]
                      if a.get("frame_status") in RESOLVED_FRAME)
    return {
        "total": len(vis),
        "triaged": sum(1 for i in vis if i.get("status") != "proposed"),
        "confirmed": len(kept),
        "frames_total": frames_total,
        "frames_done": frames_done,
        "complete": bool(vis) and all(is_complete(i) for i in vis),
    }


def apply_action(doc, body):
    """Dispatch one console action onto the doc. Returns the affected issue."""
    op = body.get("op")
    key = body.get("key")
    if op == "accept":
        return accept_issue(doc, key)
    if op == "reject":
        return reject_issue(doc, key, body.get("reason"))
    if op == "edit":
        return edit_issue(doc, key, body.get("fields", {}))
    if op == "pick":
        return pick_frame(doc, key, body["anchor"], offset=body.get("offset"),
                          path=body.get("path"), crop=body.get("crop"),
                          caption=body.get("caption"))
    if op == "skip":
        return skip_anchor(doc, key, body["anchor"])
    raise ValueError(f"unknown op: {op!r}")


# ---------------------------------------------------------------------------
# Path-traversal guard (load-bearing — keep + regression-tested)
# ---------------------------------------------------------------------------

def parse_crop(s):
    """Parse a 'left,top,right,bottom' query value into a crop dict, or None.

    Lenient: missing/malformed input returns None (serve the full image) rather
    than erroring. Matches selected_frame.crop / html_export.embed_image order.
    """
    if not s:
        return None
    parts = s.split(",")
    if len(parts) != 4:
        return None
    try:
        left, top, right, bottom = (int(p) for p in parts)
    except ValueError:
        return None
    if right <= left or bottom <= top:
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def is_within(root, candidate):
    """
    True iff `candidate` resolves to a path inside `root`. Uses commonpath so a
    sibling like <root>-evil cannot slip past a naive prefix check, and treats a
    different drive (Windows) or a non-existent common base as outside.
    """
    root = os.path.abspath(root)
    candidate = os.path.abspath(candidate)
    try:
        return os.path.commonpath([root, candidate]) == root
    except ValueError:
        return False                     # different drives → not within


# ---------------------------------------------------------------------------
# Embedded console (HTML / CSS / JS)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Review Console</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#181818;color:#e0e0e0;min-height:100vh}
#nav{position:fixed;top:0;left:0;right:0;background:#111;padding:9px 14px;display:flex;flex-wrap:wrap;gap:5px;z-index:100;border-bottom:1px solid #2a2a2a;align-items:center}
#nav .summary{margin-left:auto;font-size:12px;color:#888;white-space:nowrap}
.chip{padding:3px 9px;border-radius:11px;font-size:11px;font-weight:700;cursor:pointer;border:2px solid transparent;letter-spacing:.02em}
.chip.proposed{background:#2a2a2a;color:#999}
.chip.accepted{background:#14391e;color:#4ade80;border-color:#22c55e}
.chip.edited{background:#14391e;color:#4ade80;border-color:#22c55e}
.chip.partial{background:#3a3115;color:#fbbf24;border-color:#d97706}
.chip.rejected{background:#3b1212;color:#f87171;border-color:#b91c1c;text-decoration:line-through}
.chip.active{outline:2px solid #60a5fa;outline-offset:2px}
#main{margin-top:54px;padding:22px;max-width:1180px;margin-left:auto;margin-right:auto}
#card{background:#1c1c1c;border:1px solid #2a2a2a;border-radius:10px;padding:20px 22px}
.cardhead{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap}
.idline{font-size:12px;font-weight:700;color:#60a5fa;letter-spacing:.06em;text-transform:uppercase}
.statusbadge{font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;text-transform:uppercase;letter-spacing:.04em}
.sb-proposed{background:#2a2a2a;color:#aaa}.sb-accepted,.sb-edited{background:#14391e;color:#4ade80}
.sb-partial{background:#3a3115;color:#fbbf24}.sb-rejected{background:#3b1212;color:#f87171}
h1.title{font-size:20px;font-weight:600;margin:4px 0 2px;width:100%}
.meta{font-size:12px;color:#9aa;margin-bottom:2px}
.sev{font-weight:700}.sev.S0,.sev.S1{color:#f87171}.sev.S2{color:#fbbf24}.sev.S3,.sev.S4{color:#9ca3af}
.gate3{display:flex;gap:10px;margin:14px 0 4px}
.btn{padding:8px 18px;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
.btn .k{opacity:.6;font-weight:400;margin-left:5px;font-size:11px}
.btn-accept{background:#15803d;color:#fff}.btn-accept:hover{background:#16a34a}
.btn-reject{background:#3a1a1a;color:#f87171;border:1px solid #7f1d1d}.btn-reject:hover{background:#4a1f1f}
.btn-edit{background:#262626;color:#ddd;border:1px solid #3a3a3a}.btn-edit:hover{background:#333}
.evidence{margin:16px 0;border-left:3px solid #3b3b8a;background:#16162a;padding:10px 14px;border-radius:0 6px 6px 0}
.evidence .lab{font-size:10px;font-weight:700;color:#818cf8;letter-spacing:.08em;margin-bottom:6px}
.ev-quote{font-size:13px;color:#cdd;line-height:1.5;margin-bottom:3px}
.ev-quote .ts{color:#a5b4fc;font-weight:700;font-size:12px;margin-left:6px}
.ev-quote .jump{color:#666;font-size:11px;margin-left:8px;cursor:pointer;text-decoration:underline}
.oe{display:flex;gap:24px;margin:12px 0;flex-wrap:wrap}
.oe .col{flex:1;min-width:220px}
.oe .lab{font-size:10px;font-weight:700;color:#60a5fa;letter-spacing:.06em;margin-bottom:4px}
.oe ul{list-style:none;font-size:13px;color:#ccc;line-height:1.5}
.oe li::before{content:'\2013 \00a0';color:#555}
.facets{margin-top:18px;border-top:1px solid #2a2a2a;padding-top:14px}
.facets>.lab{font-size:11px;font-weight:700;color:#60a5fa;letter-spacing:.06em;margin-bottom:10px}
.facet{margin-bottom:16px;padding:10px;border-radius:8px;border:1px solid #242424}
.facet.focused{border-color:#3b6;background:#15201a}
.facet .cap{font-size:13px;color:#ddd;margin-bottom:2px}
.facet .cap .ts{color:#a5b4fc;font-weight:700;font-size:12px;margin-left:6px}
.facet .fstat{font-size:11px;margin-bottom:7px}
.fstat.pending{color:#fbbf24}.fstat.selected{color:#4ade80}.fstat.skipped{color:#fb923c}.fstat.failed{color:#f87171}
.strip{display:flex;gap:7px;flex-wrap:wrap}
.cand{position:relative;border:2px solid #2a2a2a;border-radius:5px;overflow:hidden;cursor:pointer;width:128px}
.cand:hover{border-color:#60a5fa}
.cand.sel{border-color:#22c55e}
.cand img{display:block;width:100%;height:auto}
.cand .num{position:absolute;top:2px;left:2px;background:rgba(0,0,0,.7);color:#fff;font-size:10px;padding:1px 5px;border-radius:3px}
.cand .off{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.72);font-size:9px;padding:2px 4px;color:#bbb}
.cand.missing{display:flex;align-items:center;justify-content:center;height:74px;color:#555;font-size:10px;cursor:default;width:128px}
.facet .skipbtn{margin-top:6px;background:none;border:1px solid #3a3a3a;color:#888;font-size:11px;padding:3px 10px;border-radius:4px;cursor:pointer}
.facet .skipbtn:hover{color:#fb923c;border-color:#9a3412}
.editul textarea,.editul input{width:100%;background:#141414;color:#e0e0e0;border:1px solid #3a3a3a;border-radius:6px;padding:7px 9px;font-size:13px;font-family:inherit;resize:vertical;line-height:1.5}
.editul textarea:focus,.editul input:focus{outline:none;border-color:#60a5fa}
.edrow{margin-bottom:10px}.edrow label{display:block;font-size:10px;font-weight:700;color:#60a5fa;letter-spacing:.06em;margin-bottom:4px}
.editbar{display:flex;gap:10px;margin-top:6px}
.empty{text-align:center;color:#888;padding:80px 20px;font-size:15px;line-height:1.6}
.hint{position:fixed;bottom:0;left:0;right:0;background:#111;border-top:1px solid #242424;font-size:11px;color:#666;padding:6px 14px;text-align:center}
.hint b{color:#999}
/* reject reason picker */
#reasons{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:300;align-items:center;justify-content:center}
#reasons.open{display:flex}
#reasons .box{background:#1c1c1c;border:1px solid #7f1d1d;border-radius:10px;padding:22px 26px;width:360px}
#reasons h3{font-size:14px;color:#f87171;margin-bottom:14px}
#reasons .opt{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:6px;cursor:pointer;font-size:13px;color:#ddd}
#reasons .opt:hover{background:#2a1a1a}
#reasons .opt .key{background:#3a1a1a;color:#f87171;border-radius:4px;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:12px}
#reasons .foot{font-size:11px;color:#666;margin-top:12px}
/* selected-result preview (the saved crop/mark, shown on the facet) */
.savedshot{margin-bottom:10px}
.savedshot .lab{font-size:10px;font-weight:700;color:#4ade80;letter-spacing:.06em;margin-bottom:5px}
.savedshot img{max-width:340px;max-height:220px;border:2px solid #22c55e;border-radius:6px;display:block}
/* lightbox (crop / draw) */
#lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:200;flex-direction:column;align-items:center;justify-content:center;padding:16px}
#lightbox.open{display:flex}
#lb-top{display:flex;align-items:center;gap:16px;margin-bottom:12px;width:100%;max-width:92vw;justify-content:space-between}
#lb-nav{display:flex;align-items:center;gap:8px;flex-shrink:0}
#lb-prev,#lb-next{background:none;border:1px solid #444;color:#bbb;border-radius:4px;width:30px;height:30px;cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;line-height:1}
#lb-prev:hover,#lb-next:hover{background:#2a2a2a;color:#fff}
#lb-counter{font-size:12px;color:#666;min-width:36px;text-align:center}
#lb-label{font-size:13px;color:#999;flex:1;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lb-hint{font-size:12px;color:#555;flex-shrink:0}
#crop-canvas{cursor:crosshair;border:1px solid #333;max-width:90vw;max-height:70vh;display:block}
#lb-actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;justify-content:center}
.lb-btn{padding:8px 20px;border-radius:6px;border:none;cursor:pointer;font-size:14px;font-weight:500}
#continue-btn{background:#1d4ed8;color:#fff}#draw-btn{background:#7c3aed;color:#fff}
#recrop-btn,#clearmarks-btn,#clear-btn,#cancel-btn{background:#2a2a2a;color:#bbb;border:1px solid #3a3a3a}
</style>
</head>
<body>
<nav id="nav"></nav>
<div id="main"><div id="card"></div></div>

<div id="reasons"><div class="box">
  <h3>Why reject this issue?</h3>
  <div id="reason-opts"></div>
  <div class="foot">Enter / Esc &mdash; reject without a reason</div>
</div></div>

<div id="lightbox">
  <div id="lb-top">
    <div id="lb-nav">
      <button id="lb-prev">&#8592;</button>
      <span id="lb-counter">1 / 1</span>
      <button id="lb-next">&#8594;</button>
    </div>
    <span id="lb-label"></span>
    <span id="lb-hint">&#8592; / &#8594; between frames &bull; drag to crop &bull; Enter use</span>
  </div>
  <canvas id="crop-canvas"></canvas>
  <div id="lb-actions">
    <button class="lb-btn" id="draw-btn">Draw &#8594;</button>
    <button class="lb-btn" id="recrop-btn">&#8592; Re-crop</button>
    <button class="lb-btn" id="clearmarks-btn">Clear marks</button>
    <button class="lb-btn" id="continue-btn">Use frame &#8594;</button>
    <button class="lb-btn" id="clear-btn">Clear crop</button>
    <button class="lb-btn" id="cancel-btn">&#10005; Cancel</button>
  </div>
</div>

<div class="hint" id="hintbar"></div>

<script>
let DOC = __DOC_JSON__;          // {issues:[...visible...], progress:{...}}
const REASONS = __REASONS_JSON__;
let ISSUES = DOC.issues;
let currentIdx = 0;
let focusedFacet = 0;
let editing = false;

// ── derived state (mirrors build_review.py) ──
function anchorsResolved(iss){return iss.anchors.every(a=>a.frame_status==='selected'||a.frame_status==='skipped');}
function framesExtracted(iss){return iss.anchors.some(a=>(a.candidate_frames||[]).length>0);}
function chipState(iss){
  if(iss.status==='rejected')return 'rejected';
  if(iss.status==='proposed')return 'proposed';
  if(iss.status==='accepted'||iss.status==='edited')return anchorsResolved(iss)?iss.status:'partial';
  return 'proposed';
}
function fmtTs(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=Math.floor(s%60);
  return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0');}
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}
function isTyping(){const t=document.activeElement;return t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.tagName==='SELECT'||t.isContentEditable);}

// ── nav ──
function renderNav(){
  const nav=document.getElementById('nav');
  const p=DOC.progress;
  nav.innerHTML='';
  ISSUES.forEach((iss,idx)=>{
    const b=document.createElement('button');
    b.className='chip '+chipState(iss)+(idx===currentIdx?' active':'');
    b.textContent=iss.label||iss.id;
    b.title=iss.title||'';
    b.onclick=()=>{if(!editing){currentIdx=idx;focusedFacet=0;render();}};
    nav.appendChild(b);
  });
  const s=document.createElement('div');s.className='summary';
  s.textContent=`${p.total} issues · ${p.confirmed} kept · ${p.frames_done}/${p.frames_total} frames`
    +(p.complete?' · ✓ complete':'');
  nav.appendChild(s);
}

// ── card ──
function render(){
  renderNav();
  const card=document.getElementById('card');
  if(!ISSUES.length){
    card.innerHTML='<div class="empty">No issues yet.<br>Run extraction on a video + transcript, then reload.</div>';
    setHint(); return;
  }
  if(currentIdx>=ISSUES.length)currentIdx=ISSUES.length-1;
  const iss=ISSUES[currentIdx];
  if(editing){renderEdit(iss);return;}

  const cs=chipState(iss);
  const sev=(iss.severity||'').split(' ')[0];
  let h='';
  h+=`<div class="cardhead"><span class="idline">${esc(iss.label||iss.id)}</span>`
    +`<span class="statusbadge sb-${cs}">${cs}</span>`
    +`<h1 class="title">${esc(iss.title||'(untitled)')}</h1></div>`;
  h+=`<div class="meta"><span class="sev ${sev}">${esc(iss.severity||sev)}</span>`
    +` &middot; ${esc((iss.categories||[]).join(', '))}`
    +(iss.affected_area?` &middot; ${esc(iss.affected_area)}`:'')
    +(iss.reject_reason?` &middot; <span style="color:#f87171">rejected: ${esc(iss.reject_reason)}</span>`:'')
    +`</div>`;

  h+=`<div class="gate3">`
    +`<button class="btn btn-accept" onclick="doAccept()">✓ Accept<span class="k">A</span></button>`
    +`<button class="btn btn-reject" onclick="openReasons()">✗ Reject<span class="k">R</span></button>`
    +`<button class="btn btn-edit" onclick="enterEdit()">✎ Edit<span class="k">E</span></button>`
    +`</div>`;

  // evidence (one quote per anchor)
  h+=`<div class="evidence"><div class="lab">EVIDENCE</div>`;
  iss.anchors.forEach(a=>{
    h+=`<div class="ev-quote">&ldquo;${esc(a.quote)}&rdquo;`
      +`<span class="ts">@${fmtTs(a.ts_seconds)}</span>`
      +`<span class="jump" title="ts ${a.ts_seconds}s">↳ transcript</span></div>`;
  });
  h+=`</div>`;

  // observed / expected
  const ul=(arr)=>'<ul>'+(arr||[]).map(x=>`<li>${esc(x)}</li>`).join('')+'</ul>';
  h+=`<div class="oe"><div class="col"><div class="lab">OBSERVED</div>${ul(iss.observed)}</div>`
    +`<div class="col"><div class="lab">EXPECTED</div>${ul(iss.expected)}</div></div>`;

  // facets / screenshots
  h+=`<div class="facets"><div class="lab">SCREENSHOTS &middot; ${iss.anchors.length} facet${iss.anchors.length>1?'s':''}</div>`;
  iss.anchors.forEach((a,fi)=>{
    h+=`<div class="facet${fi===focusedFacet?' focused':''}" data-fi="${fi}">`
      +`<div class="cap">${esc(a.caption||'(facet)')}<span class="ts">@${fmtTs(a.ts_seconds)}</span></div>`;
    const fs=a.frame_status;
    const sl={pending:'⏳ pending — pick a frame',selected:'✓ selected',skipped:'— no screenshot'}[fs]||fs;
    h+=`<div class="fstat ${fs}">${sl}</div>`;
    // saved result: the actual cropped/marked image, so it's clear what was stored
    if(fs==='selected'&&a.selected_frame){
      let ssrc='/images?path='+encodeURIComponent(a.selected_frame.path);
      const cr=a.selected_frame.crop;
      if(cr)ssrc+=`&crop=${cr.left},${cr.top},${cr.right},${cr.bottom}`;
      h+=`<div class="savedshot"><div class="lab">✓ SAVED</div>`
        +`<img src="${ssrc}" alt="saved screenshot"></div>`;
    }
    h+=`<div class="strip">`;
    const cands=a.candidate_frames||[];
    if(!cands.length){
      h+=`<div class="cand missing">no frames yet<br>@${fmtTs(a.ts_seconds)}</div>`;
    }else{
      cands.forEach((c,ci)=>{
        const selp=a.selected_frame&&a.selected_frame.path===c.path;
        h+=`<div class="cand${selp?' sel':''}" onclick="lightboxFor(${fi},${c.offset})" title="click to view full size · ${ci+1} to pick">`
          +`<div class="num">${ci+1}</div>`
          +`<img loading="lazy" src="/images?path=${encodeURIComponent(c.path)}" alt="+${c.offset}s">`
          +`<div class="off">+${c.offset}s${ci===0?' · default':''}</div></div>`;
      });
    }
    h+=`</div><button class="skipbtn" onclick="skip(${fi})">No screenshot for this facet</button></div>`;
  });
  h+=`</div>`;
  card.innerHTML=h;
  card.querySelectorAll('.facet').forEach(f=>f.onclick=null);
  card.querySelectorAll('.jump').forEach((j,i)=>j.onclick=()=>{/* transcript jump: best-effort, ts authoritative */});
  setHint();
}

function setHint(){
  document.getElementById('hintbar').innerHTML = editing
    ? '<b>Esc</b> cancel edit · <b>Ctrl+Enter</b> save — single-key verbs are disabled while typing'
    : '<b>A</b> accept · <b>R</b> reject · <b>E</b> edit · <b>J/K</b> prev/next · <b>click</b> a frame to view · <b>1-6</b> pick frame on focused facet · <b>Tab</b> next facet · <b>Enter</b> accept + advance';
}

// ── edit mode ──
function renderEdit(iss){
  const card=document.getElementById('card');
  const ta=(id,lab,val,rows)=>`<div class="edrow"><label>${lab}</label><textarea id="${id}" rows="${rows}">${esc(val)}</textarea></div>`;
  const inp=(id,lab,val)=>`<div class="edrow"><label>${lab}</label><input id="${id}" type="text" value="${esc(val)}"></div>`;
  let h=`<div class="cardhead"><span class="idline">${esc(iss.label||iss.id)} — editing</span></div><div class="editul">`;
  h+=inp('e-title','Title',iss.title||'');
  h+=inp('e-severity','Severity (S0–S4)',iss.severity||'');
  h+=inp('e-categories','Categories (comma-separated)',(iss.categories||[]).join(', '));
  h+=inp('e-affected_area','Affected area',iss.affected_area||'');
  h+=ta('e-observed','Observed (one per line)',(iss.observed||[]).join('\n'),3);
  h+=ta('e-expected','Expected (one per line)',(iss.expected||[]).join('\n'),3);
  h+=ta('e-notes','Notes (one per line)',(iss.notes||[]).join('\n'),2);
  h+=`<div class="editbar"><button class="btn btn-accept" onclick="saveEdit()">Save edits</button>`
    +`<button class="btn btn-edit" onclick="cancelEdit()">Cancel</button></div></div>`;
  card.innerHTML=h;
  document.getElementById('e-title').focus();
  setHint();
}
function enterEdit(){if(editing)return;editing=true;render();}
function cancelEdit(){editing=false;render();}
async function saveEdit(){
  const iss=ISSUES[currentIdx];
  const lines=v=>v.split('\n').map(s=>s.trim()).filter(Boolean);
  const fields={
    title:document.getElementById('e-title').value.trim(),
    severity:document.getElementById('e-severity').value.trim(),
    categories:document.getElementById('e-categories').value.split(',').map(s=>s.trim()).filter(Boolean),
    affected_area:document.getElementById('e-affected_area').value.trim(),
    observed:lines(document.getElementById('e-observed').value),
    expected:lines(document.getElementById('e-expected').value),
    notes:lines(document.getElementById('e-notes').value),
  };
  editing=false;
  await action({op:'edit',key:iss.id,fields});
}

// ── actions ──
async function action(body){
  const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){alert('Action failed: '+await r.text());return null;}
  DOC=await r.json();ISSUES=DOC.issues;render();return DOC;
}
function curKey(){return ISSUES[currentIdx].id;}
async function doAccept(){
  // Gate-3 accept. With no frames extracted yet there is nothing to pick, so
  // advance to the next untriaged issue (triage flows). Once frames exist, stay
  // on the card so the reviewer picks one (gate 5). Enter always advances.
  const iss=ISSUES[currentIdx];const advance=!framesExtracted(iss);
  if(!await action({op:'accept',key:iss.id}))return;
  if(advance){currentIdx=nextIdx();focusedFacet=0;render();}
}
async function pick(fi,offset){const a=ISSUES[currentIdx].anchors[fi];focusedFacet=fi;await action({op:'pick',key:curKey(),anchor:a.id,offset});}
async function skip(fi){const a=ISSUES[currentIdx].anchors[fi];focusedFacet=fi;await action({op:'skip',key:curKey(),anchor:a.id});}
function nextIdx(){for(let i=currentIdx+1;i<ISSUES.length;i++)if(ISSUES[i].status==='proposed')return i;
  for(let i=0;i<currentIdx;i++)if(ISSUES[i].status==='proposed')return i;return Math.min(currentIdx+1,ISSUES.length-1);}
async function acceptAdvance(){  // Enter: accept + always advance, regardless of frames
  if(!await action({op:'accept',key:curKey()}))return;
  currentIdx=nextIdx();focusedFacet=0;render();
}

// ── reject reason picker ──
function openReasons(){
  const box=document.getElementById('reason-opts');
  box.innerHTML=REASONS.map((r,i)=>`<div class="opt" onclick="rejectWith('${r}')"><span class="key">${i+1}</span>${esc(r)}</div>`).join('');
  document.getElementById('reasons').classList.add('open');
}
function closeReasons(){document.getElementById('reasons').classList.remove('open');}
async function rejectWith(reason){closeReasons();await action({op:'reject',key:curKey(),reason});currentIdx=nextIdx();focusedFacet=0;render();}
async function rejectNoReason(){closeReasons();await action({op:'reject',key:curKey()});currentIdx=nextIdx();focusedFacet=0;render();}
function reasonsOpen(){return document.getElementById('reasons').classList.contains('open');}

// ── keyboard (the seconds lever) ──
document.addEventListener('keydown',e=>{
  if(lightboxOpen()){handleLbKey(e);return;}
  if(reasonsOpen()){
    if(e.key==='Escape'||e.key==='Enter'){e.preventDefault();rejectNoReason();return;}
    const n=parseInt(e.key);if(n>=1&&n<=REASONS.length){e.preventDefault();rejectWith(REASONS[n-1]);}
    return;
  }
  // CRITICAL focus guard: while editing / typing, single-key verbs are inert.
  if(editing||isTyping()){
    if(e.key==='Escape'){if(editing)cancelEdit();else document.activeElement.blur();}
    if(editing&&(e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();saveEdit();}
    return;
  }
  switch(e.key){
    case 'a':case 'A':e.preventDefault();doAccept();return;
    case 'r':case 'R':e.preventDefault();openReasons();return;
    case 'e':case 'E':e.preventDefault();enterEdit();return;
    case 'j':case 'J':e.preventDefault();currentIdx=Math.min(currentIdx+1,ISSUES.length-1);focusedFacet=0;render();return;
    case 'k':case 'K':e.preventDefault();currentIdx=Math.max(currentIdx-1,0);focusedFacet=0;render();return;
    case 'Tab':e.preventDefault();{const n=ISSUES[currentIdx].anchors.length;if(n)focusedFacet=(focusedFacet+1)%n;render();}return;
    case 'Enter':e.preventDefault();acceptAdvance();return;
  }
  if(e.key>='1'&&e.key<='6'){
    const a=ISSUES[currentIdx].anchors[focusedFacet];if(!a)return;
    const c=(a.candidate_frames||[])[parseInt(e.key)-1];if(c){e.preventDefault();pick(focusedFacet,c.offset);}
  }
});

// ── lightbox: crop + draw on a chosen frame ──
let lbPath=null,lbIssue=null,lbAnchor=null,lbImage=new Image(),lbMode='crop';
let cropStart=null,cropEnd=null,dragging=false;
let drawCrop=null,penStrokes=[],penDrawing=false,currentStroke=null;
const canvas=document.getElementById('crop-canvas'),cctx=canvas.getContext('2d');
function lightboxOpen(){return document.getElementById('lightbox').classList.contains('open');}
function lightboxFor(fi,offset){
  const iss=ISSUES[currentIdx];const a=iss.anchors[fi];
  const c=(a.candidate_frames||[]).find(x=>x.offset===offset);if(!c)return;
  lbIssue=iss.id;lbAnchor=a.id;lbPath=c.path;focusedFacet=fi;
  cropStart=cropEnd=null;dragging=false;drawCrop=null;penStrokes=[];penDrawing=false;currentStroke=null;lbMode='crop';
  document.getElementById('lb-label').textContent=(a.caption||'')+'  +'+offset+'s';
  const cands=a.candidate_frames||[];const ci=cands.findIndex(x=>x.offset===offset);
  document.getElementById('lb-counter').textContent=(ci+1)+' / '+cands.length;
  const showNav=cands.length>1?'':'none';
  document.getElementById('lb-prev').style.display=showNav;
  document.getElementById('lb-next').style.display=showNav;
  updateModeUI();
  document.getElementById('lightbox').classList.add('open');
  lbImage=new Image();
  lbImage.onload=()=>{const sc=Math.min(window.innerWidth*0.88/lbImage.naturalWidth,window.innerHeight*0.70/lbImage.naturalHeight,1);
    canvas.width=Math.round(lbImage.naturalWidth*sc);canvas.height=Math.round(lbImage.naturalHeight*sc);drawCropMode();};
  lbImage.src='/images?path='+encodeURIComponent(lbPath);
}
function closeLightbox(){document.getElementById('lightbox').classList.remove('open');lbPath=null;}
function navigateLightbox(delta){
  const a=ISSUES[currentIdx].anchors[focusedFacet];const cands=(a&&a.candidate_frames)||[];
  if(cands.length<2)return;
  const cur=cands.findIndex(c=>c.path===lbPath);
  const next=cands[(cur+delta+cands.length)%cands.length];
  lightboxFor(focusedFacet,next.offset);
}
function handleLbKey(e){
  if(e.key==='Escape'){closeLightbox();return;}
  if(e.key==='ArrowLeft'){e.preventDefault();navigateLightbox(-1);return;}
  if(e.key==='ArrowRight'){e.preventDefault();navigateLightbox(1);return;}
  if(e.key==='Enter'){e.preventDefault();document.getElementById('continue-btn').click();return;}
}
function getCrop(){
  if(!cropStart||!cropEnd)return null;
  const sx=lbImage.naturalWidth/canvas.width,sy=lbImage.naturalHeight/canvas.height;
  const left=Math.round(Math.min(cropStart.x,cropEnd.x)*sx),top=Math.round(Math.min(cropStart.y,cropEnd.y)*sy);
  const right=Math.round(Math.max(cropStart.x,cropEnd.x)*sx),bottom=Math.round(Math.max(cropStart.y,cropEnd.y)*sy);
  if(right-left<10||bottom-top<10)return null;return{left,top,right,bottom};
}
function updateModeUI(){
  const d=lbMode==='draw';
  document.getElementById('clear-btn').style.display=d?'none':'';
  document.getElementById('recrop-btn').style.display=d?'':'none';
  document.getElementById('clearmarks-btn').style.display=d?'':'none';
  document.getElementById('draw-btn').style.display=(!d&&getCrop())?'':'none';
  canvas.style.borderColor=d?'#7c3aed':'#333';
}
function drawCropMode(){
  cctx.clearRect(0,0,canvas.width,canvas.height);cctx.drawImage(lbImage,0,0,canvas.width,canvas.height);
  if(cropStart&&cropEnd){const x=Math.min(cropStart.x,cropEnd.x),y=Math.min(cropStart.y,cropEnd.y),
    w=Math.abs(cropEnd.x-cropStart.x),h=Math.abs(cropEnd.y-cropStart.y);
    cctx.save();cctx.fillStyle='rgba(0,0,0,.55)';cctx.fillRect(0,0,canvas.width,canvas.height);cctx.clearRect(x,y,w,h);
    const sx=lbImage.naturalWidth/canvas.width,sy=lbImage.naturalHeight/canvas.height;
    cctx.drawImage(lbImage,x*sx,y*sy,w*sx,h*sy,x,y,w,h);
    cctx.strokeStyle='#60a5fa';cctx.lineWidth=2;cctx.strokeRect(x,y,w,h);cctx.restore();}
  updateModeUI();
}
function drawDrawMode(){
  if(!drawCrop)return;const{left,top,right,bottom}=drawCrop,sw=right-left,sh=bottom-top;
  cctx.clearRect(0,0,canvas.width,canvas.height);cctx.drawImage(lbImage,left,top,sw,sh,0,0,canvas.width,canvas.height);
  penStrokes.forEach(st=>{if(st.length<2)return;cctx.beginPath();cctx.strokeStyle='#ef4444';cctx.lineWidth=3;cctx.lineCap='round';cctx.lineJoin='round';
    cctx.moveTo(st[0].x,st[0].y);for(let i=1;i<st.length;i++)cctx.lineTo(st[i].x,st[i].y);cctx.stroke();});
}
canvas.addEventListener('mousedown',e=>{const r=canvas.getBoundingClientRect(),pt={x:e.clientX-r.left,y:e.clientY-r.top};
  if(lbMode==='draw'){penDrawing=true;currentStroke=[pt];penStrokes.push(currentStroke);}else{cropStart=pt;cropEnd={...pt};dragging=true;}});
canvas.addEventListener('mousemove',e=>{const r=canvas.getBoundingClientRect(),pt={x:e.clientX-r.left,y:e.clientY-r.top};
  if(lbMode==='draw'){if(penDrawing){currentStroke.push(pt);drawDrawMode();}}else if(dragging){cropEnd=pt;drawCropMode();}});
window.addEventListener('mouseup',()=>{dragging=false;penDrawing=false;currentStroke=null;if(lbMode==='crop')updateModeUI();});
document.getElementById('draw-btn').onclick=()=>{const c=getCrop();if(!c)return;drawCrop=c;lbMode='draw';
  const sw=c.right-c.left,sh=c.bottom-c.top,sc=Math.min(window.innerWidth*0.9/sw,window.innerHeight*0.7/sh,1);
  canvas.width=Math.round(sw*sc);canvas.height=Math.round(sh*sc);updateModeUI();drawDrawMode();};
document.getElementById('recrop-btn').onclick=()=>{lbMode='crop';drawCrop=null;penStrokes=[];
  const sc=Math.min(window.innerWidth*0.88/lbImage.naturalWidth,window.innerHeight*0.70/lbImage.naturalHeight,1);
  canvas.width=Math.round(lbImage.naturalWidth*sc);canvas.height=Math.round(lbImage.naturalHeight*sc);updateModeUI();drawCropMode();};
document.getElementById('clearmarks-btn').onclick=()=>{penStrokes=[];drawDrawMode();};
document.getElementById('clear-btn').onclick=()=>{cropStart=cropEnd=null;drawCropMode();};
document.getElementById('cancel-btn').onclick=closeLightbox;
document.getElementById('lb-prev').onclick=()=>navigateLightbox(-1);
document.getElementById('lb-next').onclick=()=>navigateLightbox(1);
async function exportAnnotated(){
  const c=drawCrop;const sw=c.right-c.left,sh=c.bottom-c.top;
  const off=new OffscreenCanvas(sw,sh),octx=off.getContext('2d');octx.drawImage(lbImage,c.left,c.top,sw,sh,0,0,sw,sh);
  if(penStrokes.length){const kx=sw/canvas.width,ky=sh/canvas.height;octx.strokeStyle='#ef4444';octx.lineCap='round';octx.lineJoin='round';octx.lineWidth=3*Math.max(kx,ky);
    penStrokes.forEach(st=>{if(st.length<2)return;octx.beginPath();octx.moveTo(st[0].x*kx,st[0].y*ky);for(let i=1;i<st.length;i++)octx.lineTo(st[i].x*kx,st[i].y*ky);octx.stroke();});}
  const blob=await off.convertToBlob({type:'image/jpeg',quality:0.92});
  return new Promise(res=>{const fr=new FileReader();fr.onload=()=>res(fr.result.split(',')[1]);fr.readAsDataURL(blob);});
}
document.getElementById('continue-btn').onclick=async()=>{
  const offset=(()=>{const a=ISSUES[currentIdx].anchors.find(x=>x.id===lbAnchor);
    const c=(a.candidate_frames||[]).find(x=>x.path===lbPath);return c?c.offset:null;})();
  if(lbMode==='draw'&&penStrokes.length){
    const b64=await exportAnnotated();
    const r=await fetch('/save-image',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({issue:lbIssue,anchor:lbAnchor,image_b64:b64})});
    const {path}=await r.json();
    await action({op:'pick',key:lbIssue,anchor:lbAnchor,path});
  }else{
    await action({op:'pick',key:lbIssue,anchor:lbAnchor,offset,crop:getCrop()});
  }
  closeLightbox();
};

// ── init ──
(function(){let s=0;for(let i=0;i<ISSUES.length;i++){if(ISSUES[i].status==='proposed'){s=i;break;}}currentIdx=s;render();})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ReviewHandler(BaseHTTPRequestHandler):

    def __init__(self, issues_path, *args, **kwargs):
        self.issues_path = issues_path
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):       # quiet
        pass

    def _doc(self):
        return st.load(self.issues_path)

    def _payload(self, doc):
        return {"issues": visible_issues(doc), "progress": session_progress(doc)}

    # ── GET ──
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_html()
        elif parsed.path == "/images":
            qs = parse_qs(parsed.query)
            path = qs.get("path", [None])[0]
            if path:
                self._serve_image(unquote(path), crop=parse_crop(qs.get("crop", [None])[0]))
            else:
                self.send_error(400)
        elif parsed.path == "/state":
            self._send_json(self._payload(self._doc()))
        else:
            self.send_error(404)

    # ── POST ──
    def do_POST(self):
        if self.path == "/action":
            self._handle_action()
        elif self.path == "/save-image":
            self._handle_save_image()
        else:
            self.send_error(404)

    # ── handlers ──
    def _serve_html(self):
        doc = self._doc()
        html = HTML.replace("__DOC_JSON__", json.dumps(self._payload(doc), ensure_ascii=False))
        html = html.replace("__REASONS_JSON__", json.dumps(sorted(st.REJECT_REASONS)))
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_image(self, rel_path, crop=None):
        root = os.path.abspath(".")
        abs_path = os.path.abspath(rel_path)
        if not is_within(root, abs_path):
            self.send_error(403)
            return
        if not os.path.isfile(abs_path):
            self.send_error(404)
            return
        if crop:
            # Apply the human's crop on the fly, same semantics as html_export so the
            # console preview is byte-identical to the exported report.
            from PIL import Image            # lazy: only crop previews need Pillow
            img = Image.open(abs_path).convert("RGB")
            img = img.crop((crop["left"], crop["top"], crop["right"], crop["bottom"]))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            self._send_bytes(buf.getvalue(), "image/jpeg")
            return
        with open(abs_path, "rb") as f:
            data = f.read()
        self._send_bytes(data, "image/jpeg")

    def _handle_action(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 65_536:
            self.send_error(413, "Payload too large"); return
        body = json.loads(self.rfile.read(length))
        doc = self._doc()
        try:
            apply_action(doc, body)
        except (KeyError, ValueError) as e:
            self.send_error(400, str(e))
            return
        st.save(self.issues_path, doc)       # atomic + schema-validated
        self._send_json(self._payload(doc))

    def _handle_save_image(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 52_428_800:
            self.send_error(413, "Payload too large"); return
        try:
            body = json.loads(self.rfile.read(length))
            issue_id, anchor_id, img_b64 = body["issue"], body["anchor"], body["image_b64"]
            img_bytes = base64.b64decode(img_b64)
        except (ValueError, KeyError, TypeError) as e:
            # Malformed request → 400, matching _handle_action (never a 500 traceback).
            self.send_error(400, str(e))
            return

        frames_root = os.path.join(os.path.dirname(self.issues_path), "frames")
        out_dir = Path(frames_root) / issue_id / anchor_id
        # Defend the write path the same way we defend reads.
        if not is_within(os.path.abspath("."), out_dir):
            self.send_error(403)
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(out_dir.glob("annotated-*.jpg"))) + 1
        out_path = out_dir / f"annotated-{n}.jpg"
        out_path.write_bytes(img_bytes)
        self._send_json({"path": out_path.as_posix()})

    def _send_json(self, obj):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    def _send_bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve_issues_path(argv):
    if len(argv) > 1:
        return argv[1]
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        if cfg.get("issues_path", "").endswith(".json"):
            return cfg["issues_path"]
    return "issues.json"


def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv
    issues_path = _resolve_issues_path(argv)
    if not os.path.exists(issues_path):
        raise SystemExit(f"issues.json not found: {issues_path}")

    doc = st.load(issues_path)
    errs = st.validate(doc)
    if errs:
        raise SystemExit("issues.json is invalid:\n  - " + "\n  - ".join(errs))
    print(f"Loaded {len(visible_issues(doc))} issues from {issues_path}")

    DEFAULT_PORT = 8765
    port = DEFAULT_PORT
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            port = json.load(f).get("server_port", DEFAULT_PORT)

    handler = functools.partial(ReviewHandler, issues_path)
    server = HTTPServer(("localhost", port), handler)
    url = f"http://localhost:{port}"
    print(f"Review console at {url}\nPress Ctrl+C to stop.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
