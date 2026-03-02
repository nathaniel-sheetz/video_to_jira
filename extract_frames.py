#!/usr/bin/env python3
"""
extract_frames.py  —  Step 3: Extract candidate frames from the video.

Reads config.json and the issues markdown file, then for every issue's
starting timestamp runs ffmpeg at each configured offset to produce JPEGs.

Usage:
    python extract_frames.py
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


CONFIG_FILE = "config.json"


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------

def parse_issues(md_path):
    """
    Parse all VID issues from a single consolidated markdown file.

    Each issue must start with a line matching:
        # VID-NNN - Issue Title

    The Timestamps metadata line must match:
        - **Timestamps:** ~HH:MM:SS–HH:MM:SS, ...

    Only the *start* timestamp of each range is used for frame extraction.
    Returns a list of dicts with keys: id, title, timestamp_list.
    """
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

        issue = {"id": header.group(1), "title": header.group(2).strip()}

        # Find the Timestamps metadata line
        ts_raw = ""
        for line in lines[1:15]:
            m = re.match(r"-\s+\*\*Timestamps:\*\*\s*(.*)", line, re.IGNORECASE)
            if m:
                ts_raw = m.group(1).strip()
                break

        # Extract the opening timestamp of every range  (e.g. "~00:20:16–00:21:59")
        # Pattern matches the first HH:MM:SS in each comma-separated segment.
        issue["timestamp_list"] = re.findall(r"(\d{2}:\d{2}:\d{2})", ts_raw)[::2] or \
                                   re.findall(r"(\d{2}:\d{2}:\d{2})", ts_raw)
        issues.append(issue)

    return issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts_to_seconds(ts):
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def seconds_to_hms(total):
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def ts_to_dir_name(ts):
    """'00:20:16' → 'ts-00-20-16'"""
    return "ts-" + ts.replace(":", "-")


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def extract_frame(video_path, seek_ts, out_path, ffmpeg="ffmpeg"):
    cmd = [
        ffmpeg, "-y",
        "-ss", seek_ts,
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"    [WARN] ffmpeg failed for {out_path}")
        stderr_text = result.stderr.decode(errors='replace')
        # Print last 400 chars — ffmpeg header is verbose, error is at the end
        print(f"    {stderr_text[-400:].strip()}")


def main():
    if not os.path.exists(CONFIG_FILE):
        sys.exit(f"Config file not found: {CONFIG_FILE}")

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    video_path   = config["video_path"]
    issues_path  = config["issues_path"]
    output_dir   = str(Path(config["output_dir"]) / Path(video_path).stem)
    offsets      = config["frame_offsets_seconds"]
    ffmpeg_bin   = config.get("ffmpeg_path", "ffmpeg")

    if not os.path.exists(video_path):
        sys.exit(f"Video not found: {video_path}")
    if not os.path.exists(issues_path):
        sys.exit(f"Issues file not found: {issues_path}")

    issues = parse_issues(issues_path)
    print(f"Parsed {len(issues)} issues from {issues_path}")

    total_frames = sum(len(i["timestamp_list"]) * len(offsets) for i in issues)
    print(f"Extracting up to {total_frames} frames into '{output_dir}/'")
    print()

    done = 0
    for issue in issues:
        vid = issue["id"]
        if not issue["timestamp_list"]:
            print(f"  {vid}: no timestamps — skipping")
            continue

        for ts in issue["timestamp_list"]:
            base_sec = ts_to_seconds(ts)
            ts_dir   = ts_to_dir_name(ts)
            out_folder = Path(output_dir) / vid / ts_dir
            out_folder.mkdir(parents=True, exist_ok=True)

            for offset in offsets:
                target_sec = base_sec + offset
                seek_ts    = seconds_to_hms(target_sec)
                label      = f"+{offset:02d}s"
                out_file   = str(out_folder / f"{label}.jpg")

                if os.path.exists(out_file):
                    print(f"  {vid}/{ts_dir}/{label}.jpg  (exists, skipped)")
                else:
                    print(f"  {vid}/{ts_dir}/{label}.jpg  @ {seek_ts}")
                    extract_frame(video_path, seek_ts, out_file, ffmpeg=ffmpeg_bin)

                done += 1

    print(f"\nDone. {done} frames processed.")


if __name__ == "__main__":
    main()
