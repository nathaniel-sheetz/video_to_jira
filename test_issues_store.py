"""
Tests for issues_store.py — the single-source-of-truth module.

Run either way:
    python -m pytest test_issues_store.py        # if pytest is installed
    python test_issues_store.py                  # standalone runner, no deps

Covers the eng-review test diagram: validation rejects (dup ids, empty anchors,
missing evidence, bad enums), renumber (sequential / sorted / idempotent /
id-preserving), atomic save round-trip, merge preserves anchors+frames, split
moves anchors without losing picks, edit snapshots the original.
"""

import copy
import json
import os
import tempfile

import issues_store as st

FIXTURE = os.path.join("projects", "2-clipboard-20260604", "issues.json")


# ---------------------------------------------------------------------------
# Builders for synthetic docs (no dependency on the on-disk fixture)
# ---------------------------------------------------------------------------

def _anchor(aid="anc_1", ts=100, quote="it is broken", frame=None, fs="pending"):
    return {
        "id": aid, "caption": "cap", "quote": quote, "ts_seconds": ts,
        "transcript_ref": {"line_start": 1, "line_end": 2},
        "candidate_frames": [], "selected_frame": frame, "frame_status": fs,
    }


def _issue(iid="iss_a", label="VID-001", sev="S1", anchors=None, status="proposed",
           cats=None):
    return {
        "id": iid, "label": label, "status": status, "severity": sev,
        "confidence": "High", "categories": cats or ["Functional"],
        "affected_area": "x", "affected_roles": ["All users"],
        "observed": ["o"], "expected": ["e"], "notes": [],
        "anchors": anchors if anchors is not None else [_anchor()],
        "grouping": {"is_group": False},
        "provenance": {"origin": "test", "audit_added": False, "human_edited": False},
        "jira": {"issue_type": "Bug", "project_key": "", "labels": [], "exported_at": None},
    }


def _doc(issues):
    return {"schema_version": 2, "session": {"id": "t"}, "issues": issues}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_minimal_doc_valid():
    assert st.validate(_doc([_issue()])) == []


def test_fixture_validates_clean():
    if not os.path.exists(FIXTURE):
        return  # fixture is gitignored project data; skip when absent (e.g. CI)
    doc = st.load(FIXTURE)
    errs = st.validate(doc)
    assert errs == [], "real fixture should validate:\n" + "\n".join(errs)
    assert len(doc["issues"]) == 44


def test_rejects_duplicate_issue_id():
    doc = _doc([_issue(iid="iss_dup", label="VID-001"),
                _issue(iid="iss_dup", label="VID-002")])
    assert any("duplicate issue id" in e for e in st.validate(doc))


def test_rejects_duplicate_anchor_id():
    iss = _issue(anchors=[_anchor("anc_1"), _anchor("anc_1", ts=200)])
    assert any("duplicate anchor id" in e for e in st.validate(_doc([iss])))


def test_rejects_empty_anchors():
    assert any("anchors is empty" in e for e in st.validate(_doc([_issue(anchors=[])])))


def test_merged_out_may_have_empty_anchors():
    iss = _issue(anchors=[], status="merged_out")
    assert not any("anchors is empty" in e for e in st.validate(_doc([iss])))


def test_rejects_missing_evidence_on_proposed():
    iss = _issue(anchors=[_anchor(quote="   ")])
    assert any("missing evidence quote" in e for e in st.validate(_doc([iss])))


def test_rejects_bad_severity():
    assert any("severity" in e for e in st.validate(_doc([_issue(sev="S9")])))


def test_rejects_non_enum_category():
    assert any("categories not in enum" in e
               for e in st.validate(_doc([_issue(cats=["UX"])])))


def test_validate_or_raise():
    raised = False
    try:
        st.validate_or_raise(_doc([_issue(sev="nope")]))
    except ValueError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# Renumber
# ---------------------------------------------------------------------------

def test_renumber_sequential_and_severity_sorted():
    doc = _doc([
        _issue(iid="iss_c", label="OLD-C", sev="S3", anchors=[_anchor(ts=10)]),
        _issue(iid="iss_a", label="OLD-A", sev="S1", anchors=[_anchor(ts=500)]),
        _issue(iid="iss_b", label="OLD-B", sev="S1", anchors=[_anchor(ts=100)]),
    ])
    st.renumber(doc)
    labels = [i["label"] for i in doc["issues"]]
    assert labels == ["VID-001", "VID-002", "VID-003"]
    # S1 issues first, and within S1 the earlier timestamp wins
    assert [i["id"] for i in doc["issues"]] == ["iss_b", "iss_a", "iss_c"]


def test_renumber_preserves_source_label_and_ids():
    doc = _doc([_issue(iid="iss_a", label="VID-019.1", sev="S2")])
    st.renumber(doc)
    iss = doc["issues"][0]
    assert iss["id"] == "iss_a"  # stable id untouched
    assert iss["provenance"]["source_label"] == "VID-019.1"


def test_renumber_idempotent():
    doc = _doc([
        _issue(iid="iss_a", label="A", sev="S2", anchors=[_anchor(ts=10)]),
        _issue(iid="iss_b", label="B", sev="S1", anchors=[_anchor(ts=20)]),
    ])
    st.renumber(doc)
    first = json.dumps(doc, sort_keys=True)
    st.renumber(doc)
    assert json.dumps(doc, sort_keys=True) == first


# ---------------------------------------------------------------------------
# Atomic save round-trip
# ---------------------------------------------------------------------------

def test_atomic_save_roundtrip():
    doc = _doc([_issue()])
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "issues.json")
        st.save(p, doc)
        assert st.load(p) == doc
        # No temp leftovers in the directory.
        assert os.listdir(d) == ["issues.json"]


def test_save_refuses_invalid():
    doc = _doc([_issue(sev="bad")])
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "issues.json")
        raised = False
        try:
            st.save(p, doc)
        except ValueError:
            raised = True
        assert raised
        assert not os.path.exists(p)  # nothing written on invalid input


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def test_merge_preserves_anchors_and_frames():
    frame = {"path": "f.jpg", "offset": 2, "crop": None, "caption": "c"}
    target = _issue(iid="iss_t", label="VID-001", sev="S3", anchors=[_anchor("anc_1", ts=10)])
    source = _issue(iid="iss_s", label="VID-002", sev="S2",
                    anchors=[_anchor("anc_1", ts=20, frame=frame, fs="selected")])
    doc = _doc([target, source])
    st.merge(doc, ["iss_s"], into="iss_t")

    assert len(target["anchors"]) == 2
    assert source["status"] == "merged_out"
    assert source["merged_into"] == "iss_t"
    assert source["anchors"] == []
    assert target["severity"] == "S2"          # most severe wins
    assert target["grouping"]["is_group"] is True
    # the selected frame survived the move
    moved = [a for a in target["anchors"] if a["selected_frame"]]
    assert len(moved) == 1 and moved[0]["selected_frame"]["path"] == "f.jpg"
    assert st.validate(doc) == []


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

def test_split_moves_anchors_without_losing_picks():
    frame = {"path": "keep.jpg", "offset": 5, "crop": None, "caption": "c"}
    iss = _issue(iid="iss_x", label="VID-001", anchors=[
        _anchor("anc_1", ts=10),
        _anchor("anc_2", ts=20, frame=frame, fs="selected"),
    ])
    doc = _doc([iss])
    new = st.split(doc, "iss_x", ["anc_2"])

    assert [a["id"] for a in iss["anchors"]] == ["anc_1"]
    assert len(new["anchors"]) == 1
    assert new["anchors"][0]["selected_frame"]["path"] == "keep.jpg"  # pick survived
    assert new["id"] != iss["id"] and new["id"].startswith("iss_")
    assert new["provenance"]["split_from"] == "iss_x"
    st.renumber(doc)
    assert st.validate(doc) == []


def test_split_refuses_to_empty_original():
    iss = _issue(iid="iss_x", anchors=[_anchor("anc_1")])
    raised = False
    try:
        st.split(_doc([iss]), "iss_x", ["anc_1"])
    except ValueError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# Edit snapshot
# ---------------------------------------------------------------------------

def test_apply_edit_snapshots_original_once():
    iss = _issue()
    iss["observed"] = ["agent original"]
    st.apply_edit(iss, {"observed": ["human v1"]})
    assert iss["status"] == "edited"
    assert iss["original"]["observed"] == ["agent original"]
    # second edit must NOT overwrite the agent snapshot
    st.apply_edit(iss, {"observed": ["human v2"]})
    assert iss["original"]["observed"] == ["agent original"]
    assert iss["observed"] == ["human v2"]


def test_apply_edit_snapshot_is_a_copy():
    iss = _issue()
    iss["observed"] = ["agent"]
    st.apply_edit(iss, {"observed": ["human"]})
    iss["observed"].append("mutate")
    assert iss["original"]["observed"] == ["agent"]  # snapshot not aliased


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
