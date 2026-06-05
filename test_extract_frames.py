"""
Tests for extract_frames.py — the frame pipeline (schema v2).

Run either way:
    python -m pytest test_extract_frames.py     # if pytest is installed
    python test_extract_frames.py                # standalone runner, no deps

No real video or ffmpeg: the extractor is injected. Covers the path layout
(frames/<issue.id>/<anchor.id>/+NNs.jpg), candidate ranking, idempotent re-runs
(existing files skipped, force overrides), status filtering (rejected/merged_out
skipped), that selection state is never touched, negative-offset clamping, and a
full round-trip back through issues_store.save (atomic + validated).
"""

import os
import tempfile

import issues_store as st
import extract_frames as ef


# ---------------------------------------------------------------------------
# Builders (a minimal valid doc; one issue, one anchor unless overridden)
# ---------------------------------------------------------------------------

def _anchor(aid="anc_1", ts=100):
    return {
        "id": aid, "caption": "cap", "quote": "it is broken", "ts_seconds": ts,
        "transcript_ref": {"line_start": 1, "line_end": 2},
        "candidate_frames": [], "selected_frame": None, "frame_status": "pending",
    }


def _issue(iid="iss_a", status="proposed", anchors=None):
    return {
        "id": iid, "label": "VID-001", "status": status, "severity": "S1",
        "title": "a title",
        "confidence": "High", "categories": ["Functional"],
        "affected_area": "x", "affected_roles": ["All users"],
        "observed": ["o"], "expected": ["e"], "notes": [],
        "anchors": anchors if anchors is not None else [_anchor()],
        "grouping": {"is_group": False},
        "provenance": {"origin": "test", "audit_added": False, "human_edited": False},
        "jira": {"issue_type": "Bug", "project_key": "", "labels": [], "exported_at": None},
    }


def _doc(issues, offsets=(0, 2, 5)):
    return {"schema_version": 2,
            "session": {"id": "t", "frame_offsets_seconds": list(offsets)},
            "issues": issues}


class FakeExtractor:
    """Records calls and, like ffmpeg, writes the output file so re-runs skip it."""

    def __init__(self, *, succeed=True, write_file=True):
        self.calls = []
        self.succeed = succeed
        self.write_file = write_file

    def __call__(self, video, seek, out_path):
        self.calls.append((video, seek, out_path))
        if self.succeed and self.write_file:
            open(out_path, "w").close()
        return self.succeed


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_frame_name_format():
    assert ef.frame_name(0) == "+00s.jpg"
    assert ef.frame_name(2) == "+02s.jpg"
    assert ef.frame_name(30) == "+30s.jpg"
    assert ef.frame_name(-5) == "-05s.jpg"


def test_candidate_path_keyed_on_stable_ids():
    p = ef.candidate_path("frames", "iss_ab", "anc_3", 10)
    assert p == "frames/iss_ab/anc_3/+10s.jpg"


def test_build_candidates_ranks_in_offset_order():
    cands = ef.build_candidates("frames", "iss_a", "anc_1", [0, 2, 5, 10])
    assert [c["rank"] for c in cands] == [1, 2, 3, 4]
    assert [c["offset"] for c in cands] == [0, 2, 5, 10]
    assert all(c["score"] is None for c in cands)   # vision-rank is Phase 2


def test_session_offsets_fallback():
    assert ef.session_offsets({"session": {}}, default=[1, 2]) == [1, 2]
    assert ef.session_offsets(_doc([], offsets=(0, 9))) == [0, 9]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extract_all_populates_candidates_and_calls_once_per_offset():
    doc = _doc([_issue()])
    fake = FakeExtractor()
    with tempfile.TemporaryDirectory() as d:
        summary = ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=fake)
    anc = doc["issues"][0]["anchors"][0]
    assert len(anc["candidate_frames"]) == 3          # offsets (0, 2, 5)
    assert summary == {"anchors": 1, "frames_extracted": 3, "frames_skipped": 0,
                       "offsets": [0, 2, 5]}
    assert len(fake.calls) == 3


def test_seek_is_ts_plus_offset():
    doc = _doc([_issue(anchors=[_anchor(ts=100)])], offsets=(0, 2, 5))
    fake = FakeExtractor()
    with tempfile.TemporaryDirectory() as d:
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=fake)
    assert sorted(seek for _, seek, _ in fake.calls) == [100, 102, 105]


def test_negative_seek_clamped_to_zero():
    doc = _doc([_issue(anchors=[_anchor(ts=1)])], offsets=(-5, 0, 2))
    fake = FakeExtractor()
    with tempfile.TemporaryDirectory() as d:
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=fake)
    seeks = sorted(seek for _, seek, _ in fake.calls)
    assert seeks == [0, 1, 3]    # 1 + (-5) clamped to 0; never negative


def test_idempotent_rerun_skips_existing():
    doc = _doc([_issue()])
    with tempfile.TemporaryDirectory() as d:
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=FakeExtractor())
        second = FakeExtractor()
        summary = ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=second)
    assert second.calls == []                      # nothing re-extracted
    assert summary["frames_skipped"] == 3
    # candidates are still fully repopulated on the skip path
    assert len(doc["issues"][0]["anchors"][0]["candidate_frames"]) == 3


def test_force_reextracts():
    doc = _doc([_issue()])
    with tempfile.TemporaryDirectory() as d:
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=FakeExtractor())
        forced = FakeExtractor()
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=forced, force=True)
    assert len(forced.calls) == 3


def test_skips_rejected_and_merged_out():
    doc = _doc([
        _issue(iid="iss_ok", status="accepted"),
        _issue(iid="iss_rej", status="rejected"),
        _issue(iid="iss_dead", status="merged_out", anchors=[]),
    ])
    fake = FakeExtractor()
    with tempfile.TemporaryDirectory() as d:
        summary = ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=fake)
    assert summary["anchors"] == 1                 # only the accepted issue's anchor
    # the rejected issue's anchor was left untouched
    assert doc["issues"][1]["anchors"][0]["candidate_frames"] == []


def test_selection_state_untouched():
    frame = {"path": "old.jpg", "offset": 2, "crop": None, "caption": "c"}
    anc = _anchor()
    anc["selected_frame"] = frame
    anc["frame_status"] = "selected"
    doc = _doc([_issue(anchors=[anc])])
    with tempfile.TemporaryDirectory() as d:
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=FakeExtractor())
    # gate-5 is the human's; extraction only refreshes candidates
    assert anc["selected_frame"] == frame
    assert anc["frame_status"] == "selected"
    assert len(anc["candidate_frames"]) == 3


def test_failed_extraction_counts_as_skipped():
    doc = _doc([_issue()])
    fake = FakeExtractor(succeed=False, write_file=False)
    with tempfile.TemporaryDirectory() as d:
        summary = ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=fake)
    assert summary["frames_extracted"] == 0
    assert summary["frames_skipped"] == 3
    # candidates are still recorded so the console can show "no frame at @ts"
    assert len(doc["issues"][0]["anchors"][0]["candidate_frames"]) == 3


# ---------------------------------------------------------------------------
# Round-trip through issues_store (atomic save + validation)
# ---------------------------------------------------------------------------

def test_result_validates_and_roundtrips():
    doc = _doc([_issue()])
    with tempfile.TemporaryDirectory() as d:
        ef.extract_all(doc, video="v.mp4", frames_root=d, extract_fn=FakeExtractor())
        assert st.validate(doc) == []
        p = os.path.join(d, "issues.json")
        st.save(p, doc)                 # raises if extraction produced an invalid doc
        assert st.load(p) == doc


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
