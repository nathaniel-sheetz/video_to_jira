#!/usr/bin/env python3
"""
extract_frames.py — frame pipeline for the review-console (schema v2).

Driven entirely by issues.json (via issues_store), never by markdown. For every
anchor on every extractable issue it seeks the video at `anchor.ts_seconds + offset`
for each session offset and writes one JPEG per offset, then records the results
as the anchor's `candidate_frames[]`. The human picks one at gate 5; this stage
only *proposes* — it never sets `selected_frame` (vision ranking is Phase 2).

Disk layout (keyed on stable ids, so renumber/merge/split never orphan a frame):

    <frames_root>/<issue.id>/<anchor.id>/+NNs.jpg

candidate_frames entry shape:

    { "path": "<repo-relative posix>", "offset": int, "rank": int, "score": null }

`rank` is offset order in v1 (1 = first offset); `score` stays null until the
Phase-2 vision-rank pass fills it in. The ffmpeg call is injected (`extract_fn`)
so the whole pipeline is testable without a real video.

Usage:
    python extract_frames.py [path/to/issues.json]
    # falls back to config.json's issues_path, then ./issues.json
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import config
import issues_store as st

from config import CONFIG_FILE, DEFAULT_OFFSETS

# Tombstones (merged_out) carry no anchors; rejected issues are dead — neither
# needs frames. Everything a human might still confirm does.
EXTRACTABLE_STATUSES = {"proposed", "accepted", "edited"}


# ---------------------------------------------------------------------------
# Pure helpers (no ffmpeg, no disk) — the testable core
# ---------------------------------------------------------------------------

def session_offsets(doc, default=DEFAULT_OFFSETS):
    return doc.get("session", {}).get("frame_offsets_seconds") or list(default)


def default_frames_root(issues_path):
    """
    Frames live beside the issues.json that describes them. Kept relative (not
    abspath'd) so the candidate paths written into issues.json stay portable —
    the file must not bake in one machine's absolute layout.
    """
    return os.path.join(os.path.dirname(issues_path), "frames") or "frames"


def frame_name(offset):
    """0 -> '+00s.jpg', 2 -> '+02s.jpg', -5 -> '-05s.jpg'."""
    sign = "-" if offset < 0 else "+"
    return f"{sign}{abs(offset):02d}s.jpg"


def candidate_path(frames_root, issue_id, anchor_id, offset):
    """Repo-relative posix path the console/html can load directly."""
    p = Path(frames_root) / issue_id / anchor_id / frame_name(offset)
    return p.as_posix()


def build_candidates(frames_root, issue_id, anchor_id, offsets):
    """The candidate_frames list for one anchor — paths only, ffmpeg not run yet."""
    return [
        {
            "path": candidate_path(frames_root, issue_id, anchor_id, offset),
            "offset": offset,
            "rank": i + 1,          # v1: offset order; vision-rank reorders in Phase 2
            "score": None,
        }
        for i, offset in enumerate(offsets)
    ]


# ---------------------------------------------------------------------------
# ffmpeg side effect (default extractor)
# ---------------------------------------------------------------------------

def ffmpeg_extract(video, seek_seconds, out_path, ffmpeg="ffmpeg"):
    """Grab a single frame at `seek_seconds`. Returns True on success."""
    cmd = [
        ffmpeg, "-y",
        "-ss", str(seek_seconds),
        "-i", video,
        "-vframes", "1",
        "-q:v", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"    [WARN] ffmpeg failed for {out_path}")
        print(f"    {result.stderr.decode(errors='replace')[-400:].strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Extraction (mutates the doc in place)
# ---------------------------------------------------------------------------

def extract_anchor(anchor, *, video, frames_root, issue_id, offsets,
                   extract_fn, force=False):
    """
    Populate one anchor's candidate_frames and run the extractor for each offset.
    Idempotent: an offset whose file already exists is skipped unless `force`.
    Selection (`selected_frame`, `frame_status`) is the human's gate-5 call and is
    left untouched. Returns (extracted, skipped) counts.
    """
    anchor_id = anchor["id"]
    base = anchor["ts_seconds"]
    candidates = build_candidates(frames_root, issue_id, anchor_id, offsets)

    extracted = skipped = 0
    for cand in candidates:
        out_path = cand["path"]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(out_path) and not force:
            skipped += 1
            continue
        seek = max(0, base + cand["offset"])   # never seek before the start of the video
        if extract_fn(video, seek, out_path):
            extracted += 1
        else:
            skipped += 1

    anchor["candidate_frames"] = candidates
    return extracted, skipped


def extract_all(doc, *, video, frames_root, offsets=None, extract_fn=ffmpeg_extract,
                force=False):
    """
    Walk every extractable issue/anchor and populate candidate_frames. Mutates
    `doc` in place; the caller persists it (atomically) via issues_store.save.
    Returns a summary dict.
    """
    if offsets is None:
        offsets = session_offsets(doc)

    anchors = frames_extracted = frames_skipped = 0
    for issue in doc["issues"]:
        if issue.get("status") not in EXTRACTABLE_STATUSES:
            continue
        for anchor in issue.get("anchors", []):
            e, s = extract_anchor(
                anchor, video=video, frames_root=frames_root,
                issue_id=issue["id"], offsets=offsets,
                extract_fn=extract_fn, force=force,
            )
            anchors += 1
            frames_extracted += e
            frames_skipped += s

    return {
        "anchors": anchors,
        "frames_extracted": frames_extracted,
        "frames_skipped": frames_skipped,
        "offsets": offsets,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description="Extract candidate frames into issues.json.")
    p.add_argument("issues", nargs="?", help="issues.json (default: config.issues_path)")
    p.add_argument("--project", help="project name -> projects/<name>/config.json")
    p.add_argument("--config", dest="config_path", help="config.json path")
    p.add_argument("--force", action="store_true",
                   help="re-extract frames even if the JPEG already exists")
    args = p.parse_args(argv)

    cfg, _ = config.load_config(project=args.project, config=args.config_path)
    issues_path = config.resolve_issues_path(args.issues, cfg)
    if not os.path.exists(issues_path):
        sys.exit(f"issues.json not found: {issues_path}")

    doc = st.load(issues_path)

    # config overrides the session's recorded video path (it may be a
    # placeholder in a hand-made fixture); ffmpeg binary likewise.
    video = cfg.get("video_path") or doc.get("session", {}).get("video")
    ffmpeg_bin = cfg.get("ffmpeg_path", "ffmpeg")
    frames_root = cfg.get("frames_root") or default_frames_root(issues_path)

    if not video or not os.path.exists(video):
        sys.exit(f"Video not found: {video!r} (set video_path in {CONFIG_FILE})")

    def extractor(v, seek, out):
        return ffmpeg_extract(v, seek, out, ffmpeg=ffmpeg_bin)

    print(f"Extracting frames for {issues_path}")
    print(f"  video:  {video}")
    print(f"  frames: {frames_root}/<issue.id>/<anchor.id>/")
    summary = extract_all(doc, video=video, frames_root=frames_root,
                          extract_fn=extractor, force=args.force)

    st.save(issues_path, doc)   # atomic + validates the schema before writing
    print(f"\nDone. {summary['anchors']} anchors · "
          f"{summary['frames_extracted']} extracted · "
          f"{summary['frames_skipped']} skipped (offsets {summary['offsets']}).")


if __name__ == "__main__":
    main()
