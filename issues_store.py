"""
issues_store.py — single source of truth for the review-console pipeline.

Every stage (extraction skill pass 4, frame pipeline, console, html export) reads
and writes issues.json through this module. It owns the schema, validation, and an
ATOMIC save so a crash mid-write can never corrupt a session.

Schema v2 (one session per file):

    {
      "schema_version": 2,
      "session": { id, video, transcript, frame_offsets_seconds, ... },
      "issues": [
        {
          "id":      "iss_xxxx",        # stable, opaque, NEVER reused
          "label":   "VID-NNN",         # display only; reassigned by renumber()
          "status":  proposed|accepted|rejected|edited|merged_out,
          "severity": S0..S4,           # grouped issue = most severe facet
          "categories": [enum...],
          "anchors": [                  # one facet = one screenshot
            {
              "id": "anc_N",            # unique within the issue
              "caption", "quote",       # quote = the gate-3 grounding
              "ts_seconds": int,        # authoritative; drives ffmpeg seek
              "transcript_ref": {line_start, line_end},
              "candidate_frames": [ {path, offset, rank, score} ],
              "selected_frame": null | {path, offset, crop, caption},
              "frame_status": pending|selected|skipped
            }
          ],
          "grouping":  { is_group, ... },
          "provenance":{ origin, audit_added, human_edited, source_label },
          "original":  {...}            # snapshot of agent fields, set on first edit
        }
      ]
    }

Identity rule: `id` is the key that survives renumber/merge/split; `label` is cosmetic.
Frames are keyed on disk by `frames/<issue.id>/<anchor.id>/+NNs.jpg`.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile

SCHEMA_VERSION = 2

SEVERITIES = ["S0", "S1", "S2", "S3", "S4"]
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}  # S0 most severe (rank 0)

CATEGORIES = {
    "Functional", "Data integrity", "Permissions",
    "UI", "Labeling", "Error handling", "Performance",
}
STATUSES = {"proposed", "accepted", "rejected", "edited", "merged_out"}
FRAME_STATUSES = {"pending", "selected", "skipped"}
REJECT_REASONS = {"narration", "duplicate", "not-a-defect", "wont-fix"}

# Issue-level fields snapshotted under `original` the first time a human edits.
EDITABLE_FIELDS = ("title", "severity", "categories", "affected_area",
                   "observed", "expected", "notes")


# ---------------------------------------------------------------------------
# Load / save  (save is ATOMIC — temp file + os.replace)
# ---------------------------------------------------------------------------

def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, doc, *, validate_first=True):
    """Validate, then write atomically. A crash can never leave a half file."""
    if validate_first:
        validate_or_raise(doc)
    data = json.dumps(doc, indent=2, ensure_ascii=False)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".issues-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX and Windows
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(doc):
    """Return a list of human-readable error strings. Empty list == valid."""
    errs = []

    if not isinstance(doc, dict):
        return ["root is not an object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}, got {doc.get('schema_version')!r}")

    issues = doc.get("issues")
    if not isinstance(issues, list):
        return errs + ["'issues' must be a list"]

    seen_ids = set()
    seen_labels = {}
    for n, iss in enumerate(issues):
        where = f"issue[{n}]"
        iid = iss.get("id")
        if not iid:
            errs.append(f"{where}: missing id")
        else:
            where = f"issue {iid}"
            if iid in seen_ids:
                errs.append(f"{where}: duplicate issue id")
            seen_ids.add(iid)

        label = iss.get("label")
        if label:
            if label in seen_labels:
                errs.append(f"{where}: duplicate label {label!r} (also {seen_labels[label]})")
            seen_labels[label] = iid

        status = iss.get("status")
        if status not in STATUSES:
            errs.append(f"{where}: status {status!r} not in {sorted(STATUSES)}")

        sev = iss.get("severity")
        if sev not in SEV_RANK:
            errs.append(f"{where}: severity {sev!r} not in {SEVERITIES}")

        cats = iss.get("categories")
        if not isinstance(cats, list) or not cats:
            errs.append(f"{where}: categories must be a non-empty list")
        else:
            bad = [c for c in cats if c not in CATEGORIES]
            if bad:
                errs.append(f"{where}: categories not in enum: {bad}")

        if status == "rejected":
            reason = iss.get("reject_reason")
            if reason is not None and reason not in REJECT_REASONS:
                errs.append(f"{where}: reject_reason {reason!r} not in {sorted(REJECT_REASONS)}")

        anchors = iss.get("anchors")
        if not isinstance(anchors, list):
            errs.append(f"{where}: anchors must be a list")
            continue
        # A merged-out tombstone has had its anchors moved onto the target.
        if not anchors and status != "merged_out":
            errs.append(f"{where}: anchors is empty (status={status})")

        seen_anchor_ids = set()
        for an, anc in enumerate(anchors):
            aw = f"{where} anchor[{an}]"
            aid = anc.get("id")
            if not aid:
                errs.append(f"{aw}: missing anchor id")
            else:
                aw = f"{where}/{aid}"
                if aid in seen_anchor_ids:
                    errs.append(f"{aw}: duplicate anchor id within issue")
                seen_anchor_ids.add(aid)

            # Grounding: a proposed/accepted/edited anchor must carry its evidence quote.
            if status in ("proposed", "accepted", "edited"):
                if not (anc.get("quote") or "").strip():
                    errs.append(f"{aw}: missing evidence quote (status={status})")
            ts = anc.get("ts_seconds")
            if not isinstance(ts, int) or ts < 0:
                errs.append(f"{aw}: ts_seconds must be a non-negative int, got {ts!r}")

            fs = anc.get("frame_status")
            if fs not in FRAME_STATUSES:
                errs.append(f"{aw}: frame_status {fs!r} not in {sorted(FRAME_STATUSES)}")
            if not isinstance(anc.get("candidate_frames", []), list):
                errs.append(f"{aw}: candidate_frames must be a list")
            sel = anc.get("selected_frame")
            if sel is not None and not isinstance(sel, dict):
                errs.append(f"{aw}: selected_frame must be null or an object")

    return errs


def validate_or_raise(doc):
    errs = validate(doc)
    if errs:
        raise ValueError("issues.json invalid:\n  - " + "\n  - ".join(errs))


# ---------------------------------------------------------------------------
# Lookup + id generation
# ---------------------------------------------------------------------------

def find_issue(doc, key):
    """Resolve an issue by stable id or by label. Returns the issue dict or None."""
    for iss in doc["issues"]:
        if iss.get("id") == key or iss.get("label") == key:
            return iss
    return None


def new_issue_id(doc):
    existing = {i.get("id") for i in doc["issues"]}
    while True:
        candidate = "iss_" + secrets.token_hex(2)
        if candidate not in existing:
            return candidate


def new_anchor_id(issue):
    nums = []
    for a in issue.get("anchors", []):
        aid = a.get("id", "")
        if aid.startswith("anc_") and aid[4:].isdigit():
            nums.append(int(aid[4:]))
    return f"anc_{(max(nums) + 1) if nums else 1}"


# ---------------------------------------------------------------------------
# Renumber  (cosmetic labels only; ids are never touched)
# ---------------------------------------------------------------------------

def _first_ts(issue):
    return min((a.get("ts_seconds", 0) for a in issue.get("anchors", [])), default=0)


def renumber(doc):
    """
    Sort issues by (severity, first anchor timestamp) and assign sequential
    VID-NNN labels. Stable ids are preserved; the first label each issue ever
    had is preserved under provenance.source_label. Idempotent.
    """
    issues = sorted(doc["issues"], key=lambda i: (SEV_RANK[i["severity"]], _first_ts(i)))
    for n, iss in enumerate(issues, 1):
        prov = iss.setdefault("provenance", {})
        if "source_label" not in prov and iss.get("label"):
            prov["source_label"] = iss["label"]
        iss["label"] = f"VID-{n:03d}"
    doc["issues"] = issues
    return doc


# ---------------------------------------------------------------------------
# Edit with provenance snapshot
# ---------------------------------------------------------------------------

def apply_edit(issue, fields):
    """
    Apply human edits to an issue. The first time an issue is edited, snapshot
    the agent's original editable fields under `original` so the agent-vs-human
    diff (the extraction-quality signal) is never lost.
    """
    if "original" not in issue:
        issue["original"] = {k: _clone(issue.get(k)) for k in EDITABLE_FIELDS if k in issue}
    issue.update(fields)
    issue["status"] = "edited"
    issue.setdefault("provenance", {})["human_edited"] = True
    return issue


# ---------------------------------------------------------------------------
# Merge / split  (preserve every anchor and its selected frame)
# ---------------------------------------------------------------------------

def merge(doc, source_keys, into):
    """
    Fold each source issue's anchors into the target. Sources become merged_out
    tombstones (kept for identity + reversibility, excluded from export). The
    target takes the most severe severity among all involved. Anchors keep their
    evidence and selected_frame; they are re-ided to stay unique within the target.
    """
    target = find_issue(doc, into)
    if target is None:
        raise KeyError(f"merge target not found: {into!r}")

    severities = [target["severity"]]
    source_labels = list(target.get("grouping", {}).get("source_labels", []))

    for key in source_keys:
        src = find_issue(doc, key)
        if src is None:
            raise KeyError(f"merge source not found: {key!r}")
        if src["id"] == target["id"]:
            raise ValueError("cannot merge an issue into itself")
        severities.append(src["severity"])
        source_labels.append(src.get("label") or src["id"])
        for anc in src.get("anchors", []):
            anc = dict(anc)
            anc["id"] = new_anchor_id(target)
            target.setdefault("anchors", []).append(anc)
        src["anchors"] = []
        src["status"] = "merged_out"
        src["merged_into"] = target["id"]

    target["severity"] = min(severities, key=lambda s: SEV_RANK[s])
    g = target.setdefault("grouping", {})
    g["is_group"] = True
    g["source_labels"] = source_labels
    return target


def split(doc, issue_key, anchor_ids):
    """
    Move the named anchors off `issue_key` into a brand-new issue (new stable id,
    label assigned on next renumber). Each moved anchor carries its evidence and
    selected_frame intact. Returns the new issue.
    """
    src = find_issue(doc, issue_key)
    if src is None:
        raise KeyError(f"split source not found: {issue_key!r}")

    wanted = set(anchor_ids)
    moving = [a for a in src["anchors"] if a.get("id") in wanted]
    if not moving:
        raise ValueError("no matching anchors to split out")
    if len(moving) == len(src["anchors"]):
        raise ValueError("split would leave the original with no anchors")

    src["anchors"] = [a for a in src["anchors"] if a.get("id") not in wanted]

    new_issue = {
        "id": new_issue_id(doc),
        "label": "",
        "status": "proposed",
        "severity": src["severity"],
        "confidence": src.get("confidence", "High"),
        "categories": list(src.get("categories", [])),
        "affected_area": src.get("affected_area", ""),
        "affected_roles": list(src.get("affected_roles", [])),
        "observed": [],
        "expected": [],
        "notes": [],
        "anchors": moving,
        "grouping": {"is_group": len(moving) > 1},
        "provenance": {"origin": "human_split", "audit_added": False,
                       "human_edited": True, "split_from": src["id"]},
        "jira": {"issue_type": "Bug", "project_key": "", "labels": [], "exported_at": None},
    }
    doc["issues"].append(new_issue)
    return new_issue


# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------

def active_issues(doc):
    """Issues that should reach export: accepted or edited, never rejected/tombstoned."""
    return [i for i in doc["issues"] if i.get("status") in ("accepted", "edited")]


def _clone(value):
    return json.loads(json.dumps(value)) if isinstance(value, (list, dict)) else value
