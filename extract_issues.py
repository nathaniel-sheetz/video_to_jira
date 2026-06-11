#!/usr/bin/env python3
"""
extract_issues.py — transcript -> issues.json extraction harness (schema v2).

This is the one piece of the designed pipeline that turns a cleaned transcript
into a grounded `issues.json`. Today that file is hand-built by a throwaway
fixture; this module is the automated path.

Design mirror of extract_frames.py: the *fuzzy* part is injected. There it was
the ffmpeg call; here it is the agent reasoning of passes 1-3. Everything this
module owns is deterministic and testable without a model:

    pass 1  window-extract   agent  -> candidate issues per overlapping window
    pass 2  merge / group     agent  -> deduped, grouped candidate list
    pass 3  adversarial audit  agent  -> recovered misses / false-positive flags
    pass 4  validate           CODE   -> renumber + issues_store.validate_or_raise

The agent passes are plain callables passed in (or driven out-of-process via the
`extract-issues` skill runbook + the CLI). This module owns the harness around
them: parsing the transcript into cues, slicing overlapping windows for recall,
resolving each anchor's verbatim quote to an *authoritative* ts_seconds, building
schema-v2 issues, and the hard validate gate. If a quote can't be traced back to
the transcript, or an issue has no title, the run fails loudly — never half-output.

Grounding contract (the load-bearing guarantee):
  * `quote` MUST be a verbatim substring of the transcript (whitespace/case
    tolerant). If it isn't, extraction raises GroundingError. No quote, no issue.
  * `ts_seconds` is ALWAYS derived from the transcript cue that carries the quote
    (the start of the first cue the quote spans) — never trusted from the agent.
    A quote can cross cue boundaries; ts is the first cue's start. This is what
    extract_frames.py later seeks to.
  * `transcript_ref` is best-effort context: the agent may supply a wider
    {line_start,line_end}; otherwise it is the tight span of the matched cues.

Candidate shape the agent emits (per issue), consumed by assemble_issue:

    {
      "title": "Single-select dropdown requires an extra 'Done' click to close",
      "severity": "S1",
      "categories": ["Functional", "UI"],
      "confidence": "High",                # optional, advisory
      "affected_area": "Perform task - dropdown inputs",
      "affected_roles": ["All users"],     # optional
      "observed": ["..."], "expected": ["..."], "notes": ["..."],
      "origin": "window_extract",          # or "audit_added" / "human_grouped"
      "group": {"is_group": true, "rule": "...", "source_labels": [...]},  # optional
      "anchors": [
        {"caption": "...", "quote": "<verbatim substring>",
         "line_hint": 544,                  # optional: disambiguates repeats
         "transcript_ref": {"line_start": 544, "line_end": 551}}  # optional context
      ]
    }

Usage:
    python extract_issues.py windows [transcript.txt] [--size N] [--overlap M]
        Dump the overlapping windows the agent reads in pass 1 (JSON).
    python extract_issues.py assemble candidates.json [--config config.json]
        Deterministic passes: assemble the agent's grouped+audited candidates
        against the transcript, renumber, validate, and save through issues_store.
        Writes config.json:issues_path so the rest of the pipeline can find it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys

import config
import issues_store as st

from config import CONFIG_FILE, DEFAULT_OFFSETS

# Pass-1 windowing defaults, in cue count. Tuned for the dense ~73-min
# 2_Task.txt (~870 cues): ~10 windows, 30-cue overlap so a defect described
# across a window boundary still appears whole in at least one window. This is
# the mitigation for the recall failure the one-shot draft showed.
DEFAULT_WINDOW_SIZE = 120
DEFAULT_WINDOW_OVERLAP = 30

# How many consecutive cues a single quote may span when resolving grounding.
# A spoken complaint occasionally runs across 2-3 short cues ("...I shouldn't
# have to hit the done button / to get rid of the drawer."). Bounded so a
# too-loose quote can't silently match halfway across the transcript.
MAX_QUOTE_SPAN_CUES = 6

# Grouping policy (deterministic half of the Grouping Rule): these categories are
# triaged and assigned by a dev individually, so they never get folded into a
# multi-anchor group, regardless of how trivial a facet looks.
UNGROUPABLE_CATEGORIES = {"Functional", "Data integrity", "Permissions"}


class GroundingError(ValueError):
    """An anchor's quote could not be traced to a verbatim spot in the transcript."""


# ---------------------------------------------------------------------------
# Transcript parsing  (cleaned VTT: "hh:mm:ss.mmm --> ...\n<text>\n\n...")
# ---------------------------------------------------------------------------

def _parse_timestamp(ts_line):
    """'00:05:19.341 --> ...' -> 319 (whole seconds, fractional dropped)."""
    start = ts_line.split("-->", 1)[0].strip()
    parts = start.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        raise ValueError(f"unparseable timestamp: {ts_line!r}")
    sec = float(s.replace(",", "."))
    return int(h) * 3600 + int(m) * 60 + int(sec)


def _norm(text):
    """Collapse whitespace + lowercase for tolerant verbatim matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_transcript(text):
    """
    Parse a cleaned-VTT transcript into a list of cues, each:

        {ts_seconds, text, line_start, line_end}

    line_start is the 1-based line number of the cue's timestamp line; line_end
    is its last text line. Both index into the transcript file so they can fill
    an anchor's transcript_ref. `text` is the cue's spoken text (timestamp and
    blank lines stripped, multi-line cues joined with a space).
    """
    lines = text.split("\n")
    cues = []
    i = 0
    n = len(lines)
    while i < n:
        if " --> " in lines[i]:
            ts_line_no = i + 1                       # 1-based
            ts_seconds = _parse_timestamp(lines[i])
            j = i + 1
            parts = []
            while j < n and lines[j].strip() != "":
                parts.append(lines[j].strip())
                j += 1
            if parts:                                # ignore an empty cue
                cues.append({
                    "ts_seconds": ts_seconds,
                    "text": " ".join(parts),
                    "line_start": ts_line_no,
                    "line_end": j,                   # last text line (1-based)
                })
            i = j
        else:
            i += 1
    return cues


# ---------------------------------------------------------------------------
# Pass 1 harness: overlapping windows over the cue stream
# ---------------------------------------------------------------------------

def _mmss(sec):
    return f"{sec // 60:02d}:{sec % 60:02d}"


def window_transcript(cues, size=DEFAULT_WINDOW_SIZE, overlap=DEFAULT_WINDOW_OVERLAP):
    """
    Slice the cue stream into overlapping windows for the recall pass. Each
    window is a dict:

        {index, cue_start, cue_end, line_start, line_end,
         ts_start, ts_end, text}

    `text` renders each cue as "[Lnnn] (mm:ss) spoken text" so the agent can cite
    a line_hint and a verbatim quote straight out of what it reads. Overlap is in
    cues; a defect that straddles a boundary still appears whole in one window.
    """
    if size <= 0:
        raise ValueError("window size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be >= 0 and < size")

    step = size - overlap
    windows = []
    for idx, start in enumerate(range(0, max(1, len(cues)), step)):
        chunk = cues[start:start + size]
        if not chunk:
            break
        rendered = "\n".join(
            f"[L{c['line_start']}] ({_mmss(c['ts_seconds'])}) {c['text']}"
            for c in chunk
        )
        windows.append({
            "index": idx,
            "cue_start": start,
            "cue_end": start + len(chunk),
            "line_start": chunk[0]["line_start"],
            "line_end": chunk[-1]["line_end"],
            "ts_start": chunk[0]["ts_seconds"],
            "ts_end": chunk[-1]["ts_seconds"],
            "text": rendered,
        })
        if start + size >= len(cues):
            break
    return windows


# ---------------------------------------------------------------------------
# Grounding: resolve a verbatim quote -> authoritative ts_seconds + ref
# ---------------------------------------------------------------------------

def resolve_quote(quote, cues, line_hint=None):
    """
    Find `quote` verbatim in the transcript and return (ts_seconds, ref) where
    ref = {line_start, line_end} spans the cues the quote covers. A quote may run
    across several cues; ts_seconds is the start of the cue the quote *begins* in
    (what the frame pipeline seeks to).

    Matching is whitespace- and case-tolerant but otherwise exact: the quote must
    be a contiguous substring of the spoken text. If it appears more than once,
    the occurrence nearest `line_hint` wins; without a hint, the first wins.
    Raises GroundingError if the quote is nowhere in the transcript.

    Implementation: normalize every cue, join into one string, and remember each
    cue's char span in it. The quote's start char maps to the first cue, its end
    char to the last — so a match can never be misattributed to a neighbouring
    cue the quote merely abuts.
    """
    needle = _norm(quote)
    if not needle:
        raise GroundingError("empty quote")

    joined_parts = []
    spans = []          # (char_start, char_end_exclusive) per cue, aligned to cues
    pos = 0
    for c in cues:
        norm = _norm(c["text"])
        spans.append((pos, pos + len(norm)))
        joined_parts.append(norm)
        pos += len(norm) + 1            # +1 for the single space separator
    joined = " ".join(joined_parts)

    def cue_at(char_pos):
        for idx, (a, b) in enumerate(spans):
            if a <= char_pos < b:
                return idx
        return len(cues) - 1            # trailing separator -> last cue

    occurrences = []                    # (start_cue, end_cue)
    start = joined.find(needle)
    while start != -1:
        s = cue_at(start)
        e = cue_at(start + len(needle) - 1)
        occurrences.append((s, e))
        start = joined.find(needle, start + 1)

    if not occurrences:
        raise GroundingError(f"quote not found verbatim in transcript: {quote!r}")

    if line_hint is not None:
        occurrences.sort(key=lambda m: abs(cues[m[0]]["line_start"] - line_hint))
    s, e = occurrences[0]
    if e - s + 1 > MAX_QUOTE_SPAN_CUES:
        # A verbatim match this long is almost always a pasted paragraph, not a
        # single grounded utterance — refuse it so one anchor can't claim a huge
        # span of the transcript and a misleading start time.
        raise GroundingError(
            f"quote spans {e - s + 1} cues (> {MAX_QUOTE_SPAN_CUES}); "
            f"tighten it to a single utterance: {quote!r}")
    ref = {"line_start": cues[s]["line_start"], "line_end": cues[e]["line_end"]}
    return cues[s]["ts_seconds"], ref


# ---------------------------------------------------------------------------
# Grouping policy (deterministic half of the Grouping Rule)
# ---------------------------------------------------------------------------

def assert_groupable(candidate):
    """
    Enforce the one part of the Grouping Rule that is policy, not judgment: a
    deliberately grouped issue (`group.is_group` true — several *distinct* small
    defects bundled into one item) may not carry a Functional / Data integrity /
    Permissions category. The same-screen / same-fix-domain test stays with the
    agent (pass 2); this is the hard floor under it.

    Keyed on the explicit `is_group` flag, NOT anchor count: a single defect can
    legitimately carry several evidence anchors (e.g. one overdue-status bug shown
    on both the badge and the due time) without being a "group". Raises ValueError
    on violation.
    """
    if not candidate.get("group", {}).get("is_group"):
        return
    bad = UNGROUPABLE_CATEGORIES.intersection(candidate.get("categories", []))
    if bad:
        raise ValueError(
            f"grouped issue {candidate.get('title')!r} has ungroupable "
            f"categories {sorted(bad)} — those stay one-issue-one-item"
        )


# ---------------------------------------------------------------------------
# Assembly: agent candidate -> schema-v2 issue (through issues_store helpers)
# ---------------------------------------------------------------------------

def assemble_issue(doc, candidate, cues, *, default_origin="window_extract"):
    """
    Turn one agent candidate into a schema-v2 issue and append it to `doc`.
    Resolves every anchor's quote to an authoritative ts_seconds + transcript_ref,
    mints stable ids, and stamps provenance. Returns the new issue.

    Raises GroundingError if any anchor quote can't be traced, ValueError if the
    candidate violates the grouping policy or is missing a title. (Pass 4 also
    re-checks the title via issues_store; failing fast here gives a better error.)
    """
    if not (candidate.get("title") or "").strip():
        raise ValueError(f"candidate missing title: {candidate!r}")
    assert_groupable(candidate)

    raw_anchors = candidate.get("anchors") or []
    if not raw_anchors:
        raise ValueError(f"candidate {candidate['title']!r} has no anchors")

    issue = {
        "id": st.new_issue_id(doc),
        "label": "",
        "status": "proposed",
        "title": candidate["title"].strip(),
        "severity": candidate["severity"],
        "confidence": candidate.get("confidence", "High"),
        "categories": list(candidate.get("categories", [])),
        "affected_area": candidate.get("affected_area", ""),
        "affected_roles": list(candidate.get("affected_roles", ["All users"])),
        "observed": list(candidate.get("observed", [])),
        "expected": list(candidate.get("expected", [])),
        "notes": list(candidate.get("notes", [])),
        "anchors": [],
        # is_group means "distinct defects deliberately bundled", not "has >1
        # anchor". A single defect with several evidence facets stays is_group
        # false. Only an explicit group from pass 2 sets it true.
        "grouping": dict(candidate.get("group") or {"is_group": False}),
        "provenance": {
            "origin": candidate.get("origin", default_origin),
            "audit_added": candidate.get("origin") == "audit_added"
                           or candidate.get("audit_added", False),
            "human_edited": False,
        },
        "jira": {"issue_type": "Bug", "project_key": "", "labels": [], "exported_at": None},
    }
    doc["issues"].append(issue)   # append first so new_anchor_id sees the issue

    for raw in raw_anchors:
        quote = (raw.get("quote") or "").strip()
        ts_seconds, derived_ref = resolve_quote(quote, cues, raw.get("line_hint"))
        ref = raw.get("transcript_ref") or derived_ref
        issue["anchors"].append({
            "id": st.new_anchor_id(issue),
            "caption": raw.get("caption", ""),
            "quote": quote,
            "ts_seconds": ts_seconds,
            "transcript_ref": ref,
            "candidate_frames": [],
            "selected_frame": None,
            "frame_status": "pending",
        })
    return issue


def new_doc(session):
    """An empty schema-v2 doc with the session block filled in."""
    return {"schema_version": st.SCHEMA_VERSION, "session": dict(session), "issues": []}


# ---------------------------------------------------------------------------
# Pass 4: the hard validate gate
# ---------------------------------------------------------------------------

def finalize(doc):
    """
    Renumber (sequential VID-NNN labels) then validate. This is the only pass
    that can fail the run: it enforces the category enum, severity, grounding,
    required title, and ts type via issues_store. Returns `doc` for chaining;
    raises ValueError if invalid.
    """
    st.renumber(doc)
    st.validate_or_raise(doc)
    return doc


# ---------------------------------------------------------------------------
# Full four-pass orchestration (agent passes injected, like extract_frames)
# ---------------------------------------------------------------------------

def extract(transcript_text, session, *, pass1_fn,
            pass2_fn=None, pass3_fn=None,
            window_size=DEFAULT_WINDOW_SIZE, overlap=DEFAULT_WINDOW_OVERLAP):
    """
    Run the four passes end to end and return a validated schema-v2 doc.

    pass1_fn(window) -> [candidate, ...]     called per overlapping window (recall)
    pass2_fn(candidates) -> [candidate, ...] dedupe + group (default: identity)
    pass3_fn(transcript_text, draft_summary) -> [candidate, ...]  audit recoveries

    The pass_* callables are the agent. Wiring them as injected functions keeps
    this orchestration testable with canned outputs (no model), exactly as
    extract_frames.extract_all injects the ffmpeg call.
    """
    cues = parse_transcript(transcript_text)
    windows = window_transcript(cues, window_size, overlap)

    raw = []
    for w in windows:
        raw.extend(pass1_fn(w) or [])

    candidates = pass2_fn(raw) if pass2_fn else raw

    doc = new_doc(session)
    for cand in candidates:
        assemble_issue(doc, cand, cues)

    if pass3_fn:
        summary = [
            {"title": i["title"], "severity": i["severity"],
             "categories": i["categories"],
             "quotes": [a["quote"] for a in i["anchors"]]}
            for i in doc["issues"]
        ]
        for cand in pass3_fn(transcript_text, summary) or []:
            assemble_issue(doc, cand, cues, default_origin="audit_added")

    return finalize(doc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _session_id_from_path(transcript):
    """`projects/<session>/input/x.txt` -> `<session>` (skip a wrapping input/)."""
    parent = os.path.basename(os.path.dirname(os.path.abspath(transcript)))
    if parent.lower() == "input":
        parent = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(transcript))))
    return parent or "session"


def _resolve_transcript(arg, cfg):
    if arg:
        return arg
    t = cfg.get("transcript_path") or cfg.get("transcript")
    if t:
        return t
    sys.exit("No transcript given and none in config.json (transcript_path).")


def _cmd_windows(args):
    cfg, _ = config.load_config(config=args.config)
    path = _resolve_transcript(args.transcript, cfg)
    with open(path, encoding="utf-8") as f:
        cues = parse_transcript(f.read())
    windows = window_transcript(cues, args.size, args.overlap)
    if args.text:
        for w in windows:
            print(f"\n===== window {w['index']} "
                  f"(L{w['line_start']}-{w['line_end']}, "
                  f"{_mmss(w['ts_start'])}-{_mmss(w['ts_end'])}) =====")
            print(w["text"])
    else:
        print(json.dumps(windows, indent=2, ensure_ascii=False))
    print(f"\n{len(windows)} windows over {len(cues)} cues "
          f"(size {args.size}, overlap {args.overlap}).", file=sys.stderr)


def _cmd_assemble(args):
    cfg, _ = config.load_config(config=args.config)
    transcript = _resolve_transcript(args.transcript, cfg)
    with open(transcript, encoding="utf-8") as f:
        cues = parse_transcript(f.read())

    with open(args.candidates, encoding="utf-8") as f:
        payload = json.load(f)
    # Accept either a bare list of candidates or {session, issues:[...]}.
    candidates = payload["issues"] if isinstance(payload, dict) else payload
    session_in = payload.get("session", {}) if isinstance(payload, dict) else {}

    session = {
        "id": session_in.get("id") or cfg.get("session_id")
              or _session_id_from_path(transcript),
        "video": cfg.get("video_path") or session_in.get("video", ""),
        "transcript": transcript,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
                          .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "frame_offsets_seconds": cfg.get("frame_offsets_seconds") or DEFAULT_OFFSETS,
    }

    doc = new_doc(session)
    for cand in candidates:
        assemble_issue(doc, cand, cues)
    finalize(doc)

    out = args.out or cfg.get("issues_path") or "issues.json"
    st.save(out, doc)   # atomic + re-validates

    # Make the rest of the pipeline (extract_frames, console) find it.
    if not args.out and os.path.exists(args.config):
        cfg["issues_path"] = out
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    n_anchors = sum(len(i["anchors"]) for i in doc["issues"])
    print(f"Wrote {out}: {len(doc['issues'])} issues, {n_anchors} anchors "
          f"(validated through issues_store).")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None,
                        help="config.json (paths, offsets). Default: config.json")
    parser.add_argument("--project",
                        help="project name -> projects/<name>/config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pw = sub.add_parser("windows", help="dump pass-1 windows for the agent to read")
    pw.add_argument("transcript", nargs="?", help="transcript .txt (default: config)")
    pw.add_argument("--size", type=int, default=DEFAULT_WINDOW_SIZE)
    pw.add_argument("--overlap", type=int, default=DEFAULT_WINDOW_OVERLAP)
    pw.add_argument("--text", action="store_true", help="human-readable, not JSON")
    pw.set_defaults(func=_cmd_windows)

    pa = sub.add_parser("assemble",
                        help="assemble agent candidates -> validated issues.json")
    pa.add_argument("candidates", help="JSON: a candidate list or {session,issues}")
    pa.add_argument("transcript", nargs="?", help="transcript .txt (default: config)")
    pa.add_argument("--out", help="output path (default: config.issues_path)")
    pa.set_defaults(func=_cmd_assemble)

    args = parser.parse_args(argv)
    # --project is sugar for projects/<name>/config.json; --config names it
    # directly; neither falls back to the repo-root config.json.
    args.config = config.resolve_config_path(args.project, args.config)
    args.func(args)


if __name__ == "__main__":
    main()
