"""
Tests for build_review.py — the review console's server/data layer.

Run either way:
    python -m pytest test_build_review.py
    python test_build_review.py

The keyboard focus-guard and card rendering are browser-side (verify with
/qa against a running server). What's unit-testable lives here: the gate
state-transitions (accept/reject/edit/pick/skip), the derived chip/completion/
progress state, the action dispatcher, and — per the eng review — a regression
test for the /images path-traversal guard.
"""

import os

import issues_store as st
import build_review as br


def _anchor(aid="anc_1", ts=100, fs="pending", cands=None, sel=None):
    return {
        "id": aid, "caption": "cap", "quote": "broken", "ts_seconds": ts,
        "transcript_ref": {"line_start": 1, "line_end": 2},
        "candidate_frames": cands if cands is not None else [
            {"path": f"frames/iss/{aid}/+00s.jpg", "offset": 0, "rank": 1, "score": None},
            {"path": f"frames/iss/{aid}/+02s.jpg", "offset": 2, "rank": 2, "score": None},
        ],
        "selected_frame": sel, "frame_status": fs,
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


def _doc(issues):
    return {"schema_version": 2, "session": {"id": "t"}, "issues": issues}


# ── gate 3: accept / reject / edit ─────────────────────────────────────────

def test_accept_sets_status_and_clears_reason():
    doc = _doc([_issue(status="rejected")])
    doc["issues"][0]["reject_reason"] = "duplicate"
    br.accept_issue(doc, "iss_a")
    assert doc["issues"][0]["status"] == "accepted"
    assert "reject_reason" not in doc["issues"][0]


def test_accept_keeps_edited_status():
    doc = _doc([_issue(status="edited")])
    br.accept_issue(doc, "iss_a")
    assert doc["issues"][0]["status"] == "edited"   # edit is already a keep state


def test_reject_records_enum_reason():
    doc = _doc([_issue()])
    br.reject_issue(doc, "iss_a", "narration")
    assert doc["issues"][0]["status"] == "rejected"
    assert doc["issues"][0]["reject_reason"] == "narration"


def test_reject_rejects_bad_reason():
    doc = _doc([_issue()])
    try:
        br.reject_issue(doc, "iss_a", "because-i-said-so")
    except ValueError:
        return
    assert False, "bad reason should raise"


def test_reject_without_reason_is_allowed():
    doc = _doc([_issue()])
    br.reject_issue(doc, "iss_a")
    assert doc["issues"][0]["status"] == "rejected"
    assert "reject_reason" not in doc["issues"][0]


def test_edit_snapshots_original_via_store():
    doc = _doc([_issue()])
    br.edit_issue(doc, "iss_a", {"observed": ["human edit"], "bogus": "dropped"})
    iss = doc["issues"][0]
    assert iss["status"] == "edited"
    assert iss["observed"] == ["human edit"]
    assert iss["original"]["observed"] == ["o"]     # agent snapshot kept
    assert "bogus" not in iss                        # non-editable field filtered out


# ── gate 5: pick / skip ────────────────────────────────────────────────────

def test_pick_by_offset_resolves_candidate_path():
    doc = _doc([_issue()])
    br.pick_frame(doc, "iss_a", "anc_1", offset=2)
    anc = doc["issues"][0]["anchors"][0]
    assert anc["frame_status"] == "selected"
    assert anc["selected_frame"]["path"].endswith("+02s.jpg")
    assert anc["selected_frame"]["caption"] == "cap"


def test_pick_by_explicit_path():
    doc = _doc([_issue()])
    br.pick_frame(doc, "iss_a", "anc_1", path="frames/iss/anc_1/annotated-1.jpg", crop={"left": 1, "top": 2, "right": 3, "bottom": 4})
    sf = doc["issues"][0]["anchors"][0]["selected_frame"]
    assert sf["path"].endswith("annotated-1.jpg")
    assert sf["crop"] == {"left": 1, "top": 2, "right": 3, "bottom": 4}


def test_pick_bad_offset_raises():
    doc = _doc([_issue()])
    try:
        br.pick_frame(doc, "iss_a", "anc_1", offset=99)
    except ValueError:
        return
    assert False


def test_skip_clears_selection():
    doc = _doc([_issue(anchors=[_anchor(fs="selected", sel={"path": "x"})])])
    br.skip_anchor(doc, "iss_a", "anc_1")
    anc = doc["issues"][0]["anchors"][0]
    assert anc["frame_status"] == "skipped"
    assert anc["selected_frame"] is None


# ── derived state ──────────────────────────────────────────────────────────

def test_chip_state_proposed_and_rejected():
    assert br.chip_state(_issue(status="proposed")) == "proposed"
    assert br.chip_state(_issue(status="rejected")) == "rejected"


def test_chip_state_partial_until_all_facets_resolved():
    iss = _issue(status="accepted", anchors=[_anchor("anc_1", fs="selected"),
                                             _anchor("anc_2", fs="pending")])
    assert br.chip_state(iss) == "partial"
    iss["anchors"][1]["frame_status"] = "skipped"
    assert br.chip_state(iss) == "accepted"


def test_is_complete_semantics():
    assert br.is_complete(_issue(status="rejected"))            # triaged-out
    assert not br.is_complete(_issue(status="proposed"))        # not triaged
    accepted = _issue(status="accepted", anchors=[_anchor(fs="pending")])
    assert not br.is_complete(accepted)                         # facet unpicked
    accepted["anchors"][0]["frame_status"] = "selected"
    assert br.is_complete(accepted)


def test_frames_extracted():
    # No anchor has candidate_frames yet (pre-extraction) → False.
    pre = _issue(anchors=[_anchor("anc_1", cands=[]), _anchor("anc_2", cands=[])])
    assert not br.frames_extracted(pre)
    # Any anchor with candidates → True (frames have been extracted).
    post = _issue(anchors=[_anchor("anc_1", cands=[]), _anchor("anc_2")])
    assert br.frames_extracted(post)


def test_html_accept_advances_when_no_frames():
    # Browser-side: doAccept must consult framesExtracted so Accept advances during
    # pre-extraction triage but stays once frames exist. Guards against a future edit
    # dropping the frame-aware advance.
    assert "function framesExtracted(" in br.HTML
    assert "framesExtracted(iss)" in br.HTML, \
        "doAccept no longer gates its advance on whether frames are extracted"


def test_visible_excludes_merged_out():
    doc = _doc([_issue("iss_a"), _issue("iss_dead", status="merged_out", anchors=[])])
    vis = br.visible_issues(doc)
    assert [i["id"] for i in vis] == ["iss_a"]


def test_session_progress_counts():
    doc = _doc([
        _issue("iss_a", status="accepted", anchors=[_anchor(fs="selected")]),
        _issue("iss_b", status="proposed"),
        _issue("iss_c", status="rejected"),
    ])
    p = br.session_progress(doc)
    assert p["total"] == 3
    assert p["triaged"] == 2          # accepted + rejected
    assert p["confirmed"] == 1        # only the accepted one reaches export
    assert p["frames_total"] == 1 and p["frames_done"] == 1
    assert p["complete"] is False     # iss_b still proposed


# ── action dispatcher + atomic persistence ─────────────────────────────────

def test_apply_action_dispatch_and_roundtrip(tmp_path=None):
    import tempfile
    doc = _doc([_issue()])
    br.apply_action(doc, {"op": "accept", "key": "iss_a"})
    br.apply_action(doc, {"op": "pick", "key": "iss_a", "anchor": "anc_1", "offset": 0})
    assert doc["issues"][0]["status"] == "accepted"
    assert doc["issues"][0]["anchors"][0]["frame_status"] == "selected"
    assert st.validate(doc) == []
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "issues.json")
        st.save(p, doc)               # raises if a transition produced an invalid doc
        assert st.load(p) == doc


def test_accept_missing_key_raises():
    try:
        br.accept_issue(_doc([_issue()]), "no_such_key")
    except KeyError:
        return
    assert False, "missing key should raise KeyError"


def test_reject_missing_key_raises():
    try:
        br.reject_issue(_doc([_issue()]), "no_such_key")
    except KeyError:
        return
    assert False, "missing key should raise KeyError"


def test_skip_missing_key_raises():
    try:
        br.skip_anchor(_doc([_issue()]), "no_such_key", "anc_1")
    except KeyError:
        return
    assert False, "missing key should raise KeyError"


def test_pick_missing_key_raises():
    try:
        br.pick_frame(_doc([_issue()]), "no_such_key", "anc_1", offset=0)
    except KeyError:
        return
    assert False, "missing key should raise KeyError"


def test_apply_action_dispatch_reject():
    doc = _doc([_issue()])
    br.apply_action(doc, {"op": "reject", "key": "iss_a", "reason": "narration"})
    assert doc["issues"][0]["status"] == "rejected"
    assert doc["issues"][0]["reject_reason"] == "narration"


def test_apply_action_dispatch_edit():
    doc = _doc([_issue()])
    br.apply_action(doc, {"op": "edit", "key": "iss_a",
                          "fields": {"observed": ["edited"]}})
    assert doc["issues"][0]["status"] == "edited"
    assert doc["issues"][0]["observed"] == ["edited"]


def test_apply_action_dispatch_skip():
    doc = _doc([_issue(anchors=[_anchor(fs="selected", sel={"path": "x"})])])
    br.apply_action(doc, {"op": "skip", "key": "iss_a", "anchor": "anc_1"})
    assert doc["issues"][0]["anchors"][0]["frame_status"] == "skipped"


def test_apply_action_unknown_op_raises():
    try:
        br.apply_action(_doc([_issue()]), {"op": "frobnicate", "key": "iss_a"})
    except ValueError:
        return
    assert False


# ── /images path-traversal guard (regression — eng review) ─────────────────

def test_is_within_allows_inside():
    root = os.path.abspath(".")
    assert br.is_within(root, os.path.join(root, "frames", "iss", "anc", "+00s.jpg"))


def test_is_within_blocks_parent_escape():
    root = os.path.abspath(".")
    assert not br.is_within(root, os.path.join(root, "..", "..", "etc", "passwd"))


def test_is_within_blocks_sibling_prefix():
    # the classic naive-prefix hole: '<root>-evil' must not count as inside '<root>'
    root = os.path.abspath("project")
    assert not br.is_within(root, os.path.abspath("project-evil/secret.jpg"))


def test_is_within_blocks_absolute_outside():
    root = os.path.abspath(".")
    assert not br.is_within(root, os.path.abspath(os.sep + "etc"))


# ── keyboard focus-guard (regression — design review CRITICAL requirement) ──
# The guard itself runs in the browser; this asserts the dispatch logic is still
# wired so single-key verbs go inert while a text field is focused. Catches a
# future edit that drops the guard and reintroduces "typing 'a' fires Accept".

def test_parse_crop():
    assert br.parse_crop("10,20,300,200") == {"left": 10, "top": 20, "right": 300, "bottom": 200}
    assert br.parse_crop(None) is None
    assert br.parse_crop("") is None
    assert br.parse_crop("1,2,3") is None            # wrong arity
    assert br.parse_crop("a,b,c,d") is None           # non-int
    assert br.parse_crop("300,20,10,200") is None     # right<=left (empty crop)


def test_html_lightbox_has_frame_navigation():
    # Browser-side: the full-screen viewer must offer prev/next + arrow keys, and
    # clicking a candidate must open the viewer (not silently drop nav as the v2
    # rebuild did). String-assert the wiring survives future edits.
    for token in ('function navigateLightbox(', 'id="lb-prev"', 'id="lb-next"',
                  'id="lb-counter"', "ArrowLeft", "ArrowRight", 'onclick="lightboxFor('):
        assert token in br.HTML, f"lightbox navigation missing: {token}"


def test_html_shows_saved_selection():
    # The saved crop/mark must be rendered back on the facet, with the server-crop
    # query param when a crop is stored, so the reviewer sees what was saved.
    assert "savedshot" in br.HTML
    assert "&crop=" in br.HTML, "saved-crop preview no longer requests the server crop"


def test_html_keeps_single_key_focus_guard():
    assert "editing||isTyping()" in br.HTML, \
        "focus-guard removed: single-key verbs would fire while typing in Edit mode"


def test_html_istyping_covers_text_inputs():
    # isTyping must treat INPUT/TEXTAREA/SELECT/contentEditable as 'typing'.
    for token in ("INPUT", "TEXTAREA", "SELECT", "isContentEditable"):
        assert token in br.HTML, f"isTyping no longer guards {token}"


# ── standalone runner ──────────────────────────────────────────────────────

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
