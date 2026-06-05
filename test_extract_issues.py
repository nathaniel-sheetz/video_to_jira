"""
Tests for extract_issues.py — the transcript -> issues.json harness (schema v2).

Run either way:
    python -m pytest test_extract_issues.py     # if pytest is installed
    python test_extract_issues.py                # standalone runner, no deps

No model: the agent passes are injected as plain callables, exactly as
test_extract_frames injects the ffmpeg call. Grounding is checked against the
real 2_Task.txt so the ts_seconds derivation is verified against the same
transcript the fixture was hand-grounded on.
"""

import json
import os
import tempfile

import issues_store as st
import extract_issues as ex

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_TRANSCRIPT = os.path.join(
    HERE, "projects", "2-clipboard-20260604", "input", "2_Task.txt"
)

# A small synthetic transcript exercising single- and multi-cue quotes plus a
# repeated phrase (for line_hint disambiguation). Mirrors cleaned-VTT shape.
SAMPLE = """\
00:00:01.000 --> 00:00:03.000
Opening the monitor screen now.

00:05:19.341 --> 00:05:22.220
this should be showing as a planned event.

00:14:26.584 --> 00:14:29.113
close immediately. I shouldn't have to hit the done button

00:14:29.113 --> 00:14:30.184
to get rid of the drawer.

00:20:00.000 --> 00:20:02.000
the badge is wrong here.

00:30:00.000 --> 00:30:02.000
the badge is wrong here.
"""


def _session():
    return {"id": "t", "video": "v.mp4", "transcript": "t.txt",
            "frame_offsets_seconds": [0, 2, 5]}


def _cand(title="A real defect", sev="S1", cats=("Functional",),
          anchors=None, **extra):
    c = {"title": title, "severity": sev, "categories": list(cats),
         "affected_area": "x", "observed": ["o"], "expected": ["e"],
         "anchors": anchors or [{"caption": "c", "quote": "the badge is wrong here"}]}
    c.update(extra)
    return c


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def test_parse_timestamp_forms():
    assert ex._parse_timestamp("00:05:19.341 --> 00:05:22.220") == 319
    assert ex._parse_timestamp("01:00:00.000 --> 01:00:01.000") == 3600
    assert ex._parse_timestamp("12:30.500 --> 12:31.000") == 750


def test_parse_timestamp_unparseable_raises():
    try:
        ex._parse_timestamp("not a timestamp")
    except ValueError:
        return
    assert False, "unparseable timestamp should raise ValueError"


def test_parse_transcript_cues_and_lines():
    cues = ex.parse_transcript(SAMPLE)
    assert len(cues) == 6
    first = cues[0]
    assert first["ts_seconds"] == 1
    assert first["text"] == "Opening the monitor screen now."
    # line_start is the timestamp line (1-based), line_end the text line.
    assert first["line_start"] == 1 and first["line_end"] == 2
    assert cues[1]["ts_seconds"] == 319


def test_parse_real_transcript_nonempty():
    with open(REAL_TRANSCRIPT, encoding="utf-8") as f:
        cues = ex.parse_transcript(f.read())
    assert len(cues) > 500          # ~73 min of dense cues
    assert all(c["text"] for c in cues)
    assert all(c["ts_seconds"] >= 0 for c in cues)


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------

def test_windows_overlap_and_cover():
    cues = ex.parse_transcript(SAMPLE)
    ws = ex.window_transcript(cues, size=3, overlap=1)
    # Every cue index appears in at least one window.
    covered = set()
    for w in ws:
        covered.update(range(w["cue_start"], w["cue_end"]))
    assert covered == set(range(len(cues)))
    # Consecutive windows share `overlap` cues.
    assert ws[1]["cue_start"] == ws[0]["cue_end"] - 1


def test_window_text_carries_line_and_time():
    cues = ex.parse_transcript(SAMPLE)
    w = ex.window_transcript(cues, size=2, overlap=0)[0]
    assert "[L1]" in w["text"] and "(00:01)" in w["text"]


def test_window_overlap_validation():
    cues = ex.parse_transcript(SAMPLE)
    for bad in (lambda: ex.window_transcript(cues, size=3, overlap=3),
                lambda: ex.window_transcript(cues, size=0, overlap=0)):
        try:
            bad()
            assert False, "expected ValueError"
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Grounding resolution (ts authoritative, multi-cue, verbatim guard)
# ---------------------------------------------------------------------------

def test_resolve_single_cue_quote():
    cues = ex.parse_transcript(SAMPLE)
    ts, ref = ex.resolve_quote("this should be showing as a planned event", cues)
    assert ts == 319
    assert ref == {"line_start": 4, "line_end": 5}


def test_resolve_multi_cue_quote_takes_first_cue_ts():
    cues = ex.parse_transcript(SAMPLE)
    # This phrase straddles two cues; ts is the start of the FIRST.
    ts, ref = ex.resolve_quote(
        "I shouldn't have to hit the done button to get rid of the drawer", cues)
    assert ts == 866                       # 00:14:26
    assert ref["line_start"] == 7 and ref["line_end"] == 11


def test_resolve_against_real_transcript():
    # Same anchors the hand fixture grounded — ts must match exactly.
    with open(REAL_TRANSCRIPT, encoding="utf-8") as f:
        cues = ex.parse_transcript(f.read())
    ts, _ = ex.resolve_quote("this should be showing as a planned event", cues)
    assert ts == 319
    ts2, _ = ex.resolve_quote(
        "I shouldn't have to hit the done button to get rid of the drawer", cues)
    assert ts2 == 866


def test_resolve_line_hint_disambiguates_repeat():
    cues = ex.parse_transcript(SAMPLE)
    ts_near, _ = ex.resolve_quote("the badge is wrong here", cues, line_hint=13)
    ts_far, _ = ex.resolve_quote("the badge is wrong here", cues, line_hint=16)
    assert ts_near == 1200                 # 00:20:00, cue at line 13
    assert ts_far == 1800                  # 00:30:00, cue at line 16


def test_resolve_case_and_whitespace_tolerant():
    cues = ex.parse_transcript(SAMPLE)
    ts, _ = ex.resolve_quote("THIS   should be showing\n as a planned event", cues)
    assert ts == 319


def test_resolve_missing_quote_raises():
    cues = ex.parse_transcript(SAMPLE)
    try:
        ex.resolve_quote("this phrase is nowhere in the transcript", cues)
        assert False, "expected GroundingError"
    except ex.GroundingError:
        pass


def test_resolve_empty_quote_raises():
    cues = ex.parse_transcript(SAMPLE)
    try:
        ex.resolve_quote("", cues)
        assert False, "empty quote should raise GroundingError"
    except ex.GroundingError:
        pass


def test_resolve_over_span_raises():
    # Build a transcript where a long quote spans more than MAX_QUOTE_SPAN_CUES.
    # 8 short cues so a quote pulling all of them exceeds the 6-cue limit.
    long_transcript = "\n\n".join(
        f"00:0{i}:00.000 --> 00:0{i}:01.000\nword{i} content here"
        for i in range(8)
    )
    cues = ex.parse_transcript(long_transcript)
    # Join text from all 8 cues — guaranteed to span > MAX_QUOTE_SPAN_CUES.
    full_quote = " ".join(c["text"] for c in cues)
    try:
        ex.resolve_quote(full_quote, cues)
        assert False, "over-span quote should raise GroundingError"
    except ex.GroundingError as e:
        assert "cues" in str(e)


# ---------------------------------------------------------------------------
# Grouping policy
# ---------------------------------------------------------------------------

def test_grouping_blocks_functional_cluster():
    grouped = _cand(cats=["Functional"],
                    anchors=[{"caption": "c", "quote": "x"},
                             {"caption": "c2", "quote": "y"}],
                    group={"is_group": True})
    try:
        ex.assert_groupable(grouped)
        assert False, "Functional defects must not be grouped"
    except ValueError:
        pass


def test_grouping_allows_ui_cluster():
    grouped = _cand(cats=["UI"],
                    anchors=[{"caption": "c", "quote": "x"},
                             {"caption": "c2", "quote": "y"}],
                    group={"is_group": True})
    ex.assert_groupable(grouped)            # no raise


def test_single_anchor_functional_is_fine():
    ex.assert_groupable(_cand(cats=["Functional"]))   # not a group; no raise


def test_multi_anchor_data_integrity_not_a_group_is_fine():
    # One defect, several evidence facets (badge + due time) — not is_group, so
    # the ungroupable-category floor does not apply. Mirrors fixture iss_a9d4.
    multi = _cand(cats=["Data integrity"],
                  anchors=[{"caption": "badge", "quote": "x"},
                           {"caption": "due", "quote": "y"}])
    ex.assert_groupable(multi)             # no raise — is_group not set


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_assemble_builds_valid_issue():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    iss = ex.assemble_issue(doc, _cand(), cues)
    assert iss["id"].startswith("iss_")
    a = iss["anchors"][0]
    assert a["id"] == "anc_1"
    assert a["ts_seconds"] == 1200          # derived, not supplied by the candidate
    assert a["candidate_frames"] == [] and a["selected_frame"] is None
    assert a["frame_status"] == "pending"
    assert iss["status"] == "proposed"
    assert st.validate(doc) == []


def test_assemble_ignores_agent_supplied_ts():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    cand = _cand(anchors=[{"caption": "c", "quote": "the badge is wrong here",
                           "ts_seconds": 999999}])   # agent lies; harness ignores
    iss = ex.assemble_issue(doc, cand, cues)
    assert iss["anchors"][0]["ts_seconds"] == 1200


def test_assemble_keeps_agent_transcript_ref():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    cand = _cand(anchors=[{"caption": "c", "quote": "the badge is wrong here",
                           "transcript_ref": {"line_start": 1, "line_end": 30}}])
    iss = ex.assemble_issue(doc, cand, cues)
    assert iss["anchors"][0]["transcript_ref"] == {"line_start": 1, "line_end": 30}


def test_assemble_missing_title_raises():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    try:
        ex.assemble_issue(doc, _cand(title="  "), cues)
        assert False, "expected ValueError for empty title"
    except ValueError:
        pass


def test_assemble_audit_origin_provenance():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    iss = ex.assemble_issue(doc, _cand(origin="audit_added"), cues)
    assert iss["provenance"]["origin"] == "audit_added"
    assert iss["provenance"]["audit_added"] is True


def test_assemble_bad_quote_raises_grounding():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    cand = _cand(anchors=[{"caption": "c", "quote": "not in the transcript at all"}])
    try:
        ex.assemble_issue(doc, cand, cues)
        assert False, "expected GroundingError"
    except ex.GroundingError:
        pass


# ---------------------------------------------------------------------------
# Pass 4 + full orchestration
# ---------------------------------------------------------------------------

def test_finalize_renumbers_and_validates():
    cues = ex.parse_transcript(SAMPLE)
    doc = ex.new_doc(_session())
    ex.assemble_issue(doc, _cand(sev="S2", title="minor"), cues)
    ex.assemble_issue(doc, _cand(sev="S1", title="major"), cues)
    ex.finalize(doc)
    # Sorted by severity: S1 first -> VID-001.
    assert doc["issues"][0]["severity"] == "S1"
    assert doc["issues"][0]["label"] == "VID-001"
    assert doc["issues"][1]["label"] == "VID-002"


def test_full_extract_round_trip():
    """Inject all three agent passes; assert a saveable, validated doc."""
    def pass1(window):
        # Recall pass: only emit from the window that contains the phrase.
        if "planned event" in window["text"]:
            return [_cand(title="Wrong status badge", sev="S1",
                          anchors=[{"caption": "status",
                                    "quote": "this should be showing as a planned event"}])]
        return []

    def pass2(cands):
        return cands            # nothing to dedupe in this tiny case

    def pass3(_transcript, _summary):
        # Audit recovers a missed UI nit.
        return [_cand(title="Drawer won't close on its own", sev="S2", cats=["UI"],
                      anchors=[{"caption": "drawer",
                                "quote": "I shouldn't have to hit the done button "
                                         "to get rid of the drawer"}])]

    doc = ex.extract(SAMPLE, _session(), pass1_fn=pass1, pass2_fn=pass2,
                     pass3_fn=pass3, window_size=3, overlap=1)

    assert st.validate(doc) == []
    titles = {i["title"] for i in doc["issues"]}
    assert titles == {"Wrong status badge", "Drawer won't close on its own"}
    audit = next(i for i in doc["issues"] if i["title"].startswith("Drawer"))
    assert audit["provenance"]["origin"] == "audit_added"

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "issues.json")
        st.save(p, doc)                    # raises if extraction produced invalid doc
        assert st.load(p)["schema_version"] == 2


def test_extract_no_duplicate_ids_across_passes():
    def pass1(window):
        return [_cand(title=f"win{window['index']}")] if window["index"] == 0 else []

    doc = ex.extract(SAMPLE, _session(), pass1_fn=pass1,
                     pass3_fn=lambda *_: [_cand(title="audited")],
                     window_size=3, overlap=1)
    ids = [i["id"] for i in doc["issues"]]
    assert len(ids) == len(set(ids))       # stable, unique ids minted per issue


# ---------------------------------------------------------------------------
# CLI assemble (deterministic passes against the real transcript)
# ---------------------------------------------------------------------------

def test_cli_assemble_writes_validated_issues():
    candidates = [
        _cand(title="Wrong status badge", sev="S1", cats=["Functional"],
              anchors=[{"caption": "status",
                        "quote": "this should be showing as a planned event"}]),
        _cand(title="Single-select needs an extra Done click", sev="S1",
              cats=["Functional", "UI"],
              anchors=[{"caption": "drawer",
                        "quote": "I shouldn't have to hit the done button "
                                 "to get rid of the drawer"}]),
    ]
    with tempfile.TemporaryDirectory() as d:
        cand_path = os.path.join(d, "candidates.json")
        out_path = os.path.join(d, "issues.json")
        with open(cand_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)
        ex.main(["assemble", cand_path, REAL_TRANSCRIPT, "--out", out_path])

        doc = st.load(out_path)
        assert st.validate(doc) == []
        assert len(doc["issues"]) == 2
        by_title = {i["title"]: i for i in doc["issues"]}
        assert by_title["Wrong status badge"]["anchors"][0]["ts_seconds"] == 319
        assert (by_title["Single-select needs an extra Done click"]
                ["anchors"][0]["ts_seconds"] == 866)


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
