#!/usr/bin/env python3
"""
html_export.py — static report export for the review console (schema v2).

Renders a single self-contained HTML file from issues.json: every accepted/edited
issue, its evidence (quote + timestamp), and the human-confirmed screenshot for
each anchor (cropped and embedded as a base64 JPEG, so the report is one portable
file with no server and no loose images).

Deterministic by construction — it reads the session's recorded generated_at, not
the wall clock — so re-exporting the same issues.json yields byte-identical output
(a Phase-1 success criterion). Rejected, proposed, and merged-out issues never
reach the report; accepted issues whose facets aren't all picked-or-skipped are
surfaced in a warning banner rather than shipped silently.

Image embedding is injected (`embed_fn`) so the renderer is fully testable without
real JPEGs, and the read path is guarded by the same is_within check the console uses.

Usage:
    python html_export.py [path/to/issues.json] [-o report.html]
"""

from __future__ import annotations

import base64
import html
import io
import os
import sys

import issues_store as st
from build_review import is_within          # reuse the console's hardened guard

SEV_LABEL = {"S0": "Blocker", "S1": "Critical", "S2": "Major",
             "S3": "Minor", "S4": "Trivial"}


# ---------------------------------------------------------------------------
# Export model (pure)
# ---------------------------------------------------------------------------

def export_issues(doc):
    """Issues that reach the report, in document order: accepted or edited only."""
    return st.active_issues(doc)


def unresolved_anchors(issue):
    """Anchors on a kept issue with no pick/skip yet — review is incomplete."""
    return [a for a in issue.get("anchors", [])
            if a.get("frame_status") not in ("selected", "skipped")]


def fmt_ts(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Image embedding (the only I/O — injectable)
# ---------------------------------------------------------------------------

def embed_image(path, crop=None, *, root=None):
    """
    Read `path`, optionally crop it, and return a base64 image/jpeg data URI.
    Returns None if the path escapes the project root or is missing — a report
    must never reach outside the repo to embed a file. Crop is (left,top,right,
    bottom) in original-pixel coords, matching selected_frame.crop.
    """
    from PIL import Image                    # local import: only the CLI path needs Pillow
    root = root or os.path.abspath(".")
    abs_path = os.path.abspath(path)
    if not is_within(root, abs_path) or not os.path.isfile(abs_path):
        return None
    img = Image.open(abs_path)
    if crop:
        img = img.crop((crop["left"], crop["top"], crop["right"], crop["bottom"]))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Rendering (pure given embed_fn)
# ---------------------------------------------------------------------------

def _esc(text):
    return html.escape("" if text is None else str(text))


def _list_block(label, items):
    items = [i for i in (items or []) if str(i).strip()]
    if not items:
        return ""
    lis = "".join(f"<li>{_esc(i)}</li>" for i in items)
    return f'<div class="block"><div class="lab">{label}</div><ul>{lis}</ul></div>'


def _facet_block(anchor, embed_fn):
    cap = _esc(anchor.get("caption") or "")
    ts = fmt_ts(anchor.get("ts_seconds", 0))
    sel = anchor.get("selected_frame")
    if anchor.get("frame_status") == "skipped" or not sel:
        body = '<div class="noshot">— no screenshot for this facet</div>'
    else:
        uri = embed_fn(sel["path"], sel.get("crop"))
        body = (f'<img class="shot" src="{uri}" alt="{cap}">' if uri
                else '<div class="noshot">⚠ screenshot file missing</div>')
    return (f'<div class="facet"><div class="fcap">{cap}'
            f'<span class="ts">@{ts}</span></div>{body}</div>')


def render_html(doc, embed_fn=embed_image):
    issues = export_issues(doc)
    session = doc.get("session", {})
    generated = session.get("generated_at", "")

    # severity tally + incompleteness, for the header/banner
    sev_counts = {}
    incomplete = []
    for iss in issues:
        sev = iss.get("severity", "?")
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
        if unresolved_anchors(iss):
            incomplete.append(iss)

    tally = " · ".join(f"{n}×{s}" for s, n in sorted(sev_counts.items()))
    parts = [_HEAD]
    parts.append(f'<header><h1>Review Report</h1>'
                 f'<div class="sub">{len(issues)} issue(s){" · " + _esc(tally) if tally else ""}'
                 f'{" · " + _esc(generated) if generated else ""}</div></header>')

    if incomplete:
        rows = ", ".join(_esc(i.get("label") or i["id"]) for i in incomplete)
        parts.append(f'<div class="warn"><b>{len(incomplete)} accepted issue(s) have facets '
                     f'with no screenshot picked yet:</b> {rows}. '
                     f'They are included below but the review is not complete.</div>')

    if not issues:
        parts.append('<div class="empty">No accepted or edited issues to export yet.</div>')

    for iss in issues:
        sev = iss.get("severity", "")
        sev_txt = f"{sev} {SEV_LABEL.get(sev, '')}".strip()
        cats = ", ".join(iss.get("categories", []))
        edited = '<span class="badge">edited</span>' if iss.get("status") == "edited" else ""
        meta = " · ".join(filter(None, [
            f'<span class="sev {_esc(sev)}">{_esc(sev_txt)}</span>',
            _esc(cats),
            _esc(iss.get("affected_area")),
            ("Roles: " + _esc(", ".join(iss.get("affected_roles", [])))
             if iss.get("affected_roles") else ""),
        ]))

        ev = "".join(
            f'<div class="quote">“{_esc(a.get("quote"))}”'
            f'<span class="ts">@{fmt_ts(a.get("ts_seconds", 0))}</span></div>'
            for a in iss.get("anchors", [])
        )
        facets = "".join(_facet_block(a, embed_fn) for a in iss.get("anchors", []))

        parts.append(
            f'<article><div class="ihead"><span class="id">{_esc(iss.get("label") or iss["id"])}'
            f'</span>{edited}<h2>{_esc(iss.get("title") or "(untitled)")}</h2></div>'
            f'<div class="meta">{meta}</div>'
            f'<div class="evidence"><div class="lab">Evidence</div>{ev}</div>'
            f'{_list_block("Observed", iss.get("observed"))}'
            f'{_list_block("Expected", iss.get("expected"))}'
            f'{_list_block("Notes", iss.get("notes"))}'
            f'<div class="block"><div class="lab">Screenshots</div>'
            f'<div class="facets">{facets}</div></div></article>'
        )

    parts.append("</body></html>")
    return "\n".join(parts)


_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Review Report</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f7f9;color:#1f2328;line-height:1.5;padding:32px 20px}
header,article,.warn,.empty{max-width:900px;margin:0 auto}
header{margin-bottom:22px}
header h1{font-size:26px}
.sub{color:#666;font-size:13px;margin-top:4px}
.warn{background:#fff8e1;border:1px solid #f0c36d;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#7a5b00}
.empty{background:#fff;border:1px solid #e2e4e8;border-radius:8px;padding:40px;text-align:center;color:#888}
article{background:#fff;border:1px solid #e2e4e8;border-radius:10px;padding:22px 26px;margin-bottom:18px}
.ihead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.id{font-size:12px;font-weight:700;color:#2563eb;letter-spacing:.05em}
.badge{font-size:10px;font-weight:700;background:#e7f5ec;color:#15803d;padding:2px 7px;border-radius:9px;text-transform:uppercase}
.ihead h2{font-size:19px;font-weight:600;width:100%}
.meta{font-size:12px;color:#666;margin:4px 0 14px}
.sev{font-weight:700}.sev.S0,.sev.S1{color:#dc2626}.sev.S2{color:#d97706}.sev.S3,.sev.S4{color:#6b7280}
.block{margin:12px 0}
.lab{font-size:10px;font-weight:700;color:#2563eb;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.block ul{list-style:none}
.block li{font-size:14px;padding-left:16px;position:relative}
.block li::before{content:'–';position:absolute;left:0;color:#aaa}
.evidence{background:#f0f3ff;border-left:3px solid #6366f1;border-radius:0 6px 6px 0;padding:10px 14px;margin:12px 0}
.quote{font-size:13px;color:#3a3f6b;margin:3px 0}
.ts{color:#6366f1;font-weight:700;font-size:12px;margin-left:6px}
.facets{display:flex;flex-wrap:wrap;gap:16px}
.facet{flex:1;min-width:260px;max-width:440px}
.fcap{font-size:12px;color:#444;margin-bottom:5px}
.shot{width:100%;border:1px solid #d6d8dd;border-radius:6px;display:block}
.noshot{font-size:12px;color:#999;padding:18px;border:1px dashed #d6d8dd;border-radius:6px;text-align:center}
</style></head><body>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_issues_path(args):
    for a in args:
        if a.endswith(".json"):
            return a
    if os.path.exists("config.json"):
        import json
        with open("config.json") as f:
            cfg = json.load(f)
        if cfg.get("issues_path", "").endswith(".json"):
            return cfg["issues_path"]
    return "issues.json"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    issues_path = _resolve_issues_path(argv)
    out_path = None
    if "-o" in argv:
        out_path = argv[argv.index("-o") + 1]
    if out_path is None:
        out_path = os.path.join(os.path.dirname(issues_path), "review_report.html")

    if not os.path.exists(issues_path):
        raise SystemExit(f"issues.json not found: {issues_path}")
    doc = st.load(issues_path)
    errs = st.validate(doc)
    if errs:
        raise SystemExit("issues.json is invalid:\n  - " + "\n  - ".join(errs))

    html_text = render_html(doc)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_text)

    exported = export_issues(doc)
    n = len(exported)
    incomplete = sum(1 for i in exported if unresolved_anchors(i))
    note = f" ({incomplete} with unpicked facets)" if incomplete else ""
    print(f"Wrote {out_path} — {n} issue(s){note}.")


if __name__ == "__main__":
    main()
