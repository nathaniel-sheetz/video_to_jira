"""
Tests for html_export.py — the static report export (schema v2).

Run either way:
    python -m pytest test_html_export.py
    python test_html_export.py

Image embedding is injected (no Pillow / no JPEGs needed) except for the one
test that exercises the real guard. Covers the export filter (only accepted/
edited), exclusion of rejected/proposed/merged-out, incomplete-facet detection
and banner, deterministic output, skipped-vs-picked facet rendering, and the
is_within read-path guard.
"""

import os

import issues_store as st
import html_export as hx


def _anchor(aid="anc_1", ts=100, fs="selected", sel=True):
    return {
        "id": aid, "caption": "the bug", "quote": "it is broken", "ts_seconds": ts,
        "transcript_ref": {"line_start": 1, "line_end": 2},
        "candidate_frames": [{"path": f"frames/iss/{aid}/+00s.jpg", "offset": 0, "rank": 1, "score": None}],
        "selected_frame": ({"path": f"frames/iss/{aid}/+00s.jpg", "offset": 0, "crop": None, "caption": "the bug"}
                           if sel else None),
        "frame_status": fs,
    }


def _issue(iid="iss_a", status="accepted", anchors=None, title="Paste drops a line"):
    return {
        "id": iid, "label": "VID-001", "status": status, "severity": "S1",
        "title": title,
        "confidence": "High", "categories": ["Functional"],
        "affected_area": "Editor", "affected_roles": ["Author"],
        "observed": ["only 4 of 5 lines pasted"], "expected": ["all lines pasted"], "notes": [],
        "anchors": anchors if anchors is not None else [_anchor()],
        "grouping": {"is_group": False},
        "provenance": {"origin": "test", "audit_added": False, "human_edited": False},
        "jira": {"issue_type": "Bug", "project_key": "", "labels": [], "exported_at": None},
    }


def _doc(issues, generated="2026-06-04T00:00:00Z"):
    return {"schema_version": 2, "session": {"id": "t", "generated_at": generated}, "issues": issues}


FAKE = lambda path, crop=None: "data:image/jpeg;base64,FAKE"


# ── export filter ──────────────────────────────────────────────────────────

def test_export_includes_only_accepted_and_edited():
    doc = _doc([
        _issue("iss_acc", status="accepted"),
        _issue("iss_ed", status="edited"),
        _issue("iss_prop", status="proposed"),
        _issue("iss_rej", status="rejected"),
        _issue("iss_dead", status="merged_out", anchors=[]),
    ])
    ids = [i["id"] for i in hx.export_issues(doc)]
    assert ids == ["iss_acc", "iss_ed"]


def test_excluded_issues_absent_from_html():
    doc = _doc([
        _issue("iss_acc", status="accepted", title="KEEP ME"),
        _issue("iss_rej", status="rejected", title="DROP REJECTED"),
        _issue("iss_prop", status="proposed", title="DROP PROPOSED"),
    ])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert "KEEP ME" in out
    assert "DROP REJECTED" not in out
    assert "DROP PROPOSED" not in out


# ── content ────────────────────────────────────────────────────────────────

def test_render_includes_evidence_and_embedded_shot():
    doc = _doc([_issue()])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert "it is broken" in out                 # evidence quote
    assert "@1:40" in out                         # ts 100s formatted
    assert "data:image/jpeg;base64,FAKE" in out   # embedded screenshot
    assert "only 4 of 5 lines pasted" in out      # observed


def test_skipped_facet_shows_no_screenshot():
    doc = _doc([_issue(anchors=[_anchor(fs="skipped", sel=False)])])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert "no screenshot for this facet" in out
    assert "data:image" not in out


def test_edited_issue_gets_badge():
    doc = _doc([_issue(status="edited")])
    assert "edited" in hx.render_html(doc, embed_fn=FAKE)


def test_missing_image_file_renders_placeholder():
    doc = _doc([_issue()])
    out = hx.render_html(doc, embed_fn=lambda p, c=None: None)   # embed failed
    assert "screenshot file missing" in out


# ── incompleteness ─────────────────────────────────────────────────────────

def test_unresolved_anchors_detected():
    iss = _issue(anchors=[_anchor("anc_1", fs="selected"), _anchor("anc_2", fs="pending", sel=False)])
    assert [a["id"] for a in hx.unresolved_anchors(iss)] == ["anc_2"]


def test_incomplete_banner_lists_offending_issue():
    doc = _doc([_issue("iss_a", anchors=[_anchor(fs="pending", sel=False)])])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert "facets with no screenshot picked" in out
    assert "VID-001" in out


def test_no_banner_when_all_resolved():
    doc = _doc([_issue()])
    assert "no screenshot picked yet" not in hx.render_html(doc, embed_fn=FAKE)


def test_empty_export_message():
    doc = _doc([_issue(status="proposed")])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert "No accepted or edited issues" in out


# ── determinism ────────────────────────────────────────────────────────────

def test_render_is_deterministic():
    doc = _doc([_issue("iss_a"), _issue("iss_b", title="second")])
    assert hx.render_html(doc, embed_fn=FAKE) == hx.render_html(doc, embed_fn=FAKE)


def test_uses_session_generated_at_not_wallclock():
    out = hx.render_html(_doc([_issue()], generated="2026-06-04T00:00:00Z"), embed_fn=FAKE)
    assert "2026-06-04T00:00:00Z" in out


def test_ts_formatting():
    assert hx.fmt_ts(65) == "1:05"
    assert hx.fmt_ts(3725) == "1:02:05"


# ── embed guard (read-path safety) ─────────────────────────────────────────

def test_embed_blocks_traversal():
    root = os.path.abspath(".")
    assert hx.embed_image("../../../../etc/passwd", root=root) is None


def test_embed_missing_file_returns_none():
    assert hx.embed_image("frames/does/not/exist.jpg") is None


# ── TOC and navigation ─────────────────────────────────────────────────────

def test_toc_rendered_when_issues_present():
    doc = _doc([_issue()])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert 'class="toc"' in out
    assert 'href="#iss-VID-001"' in out


def test_toc_absent_when_no_issues():
    doc = _doc([_issue(status="proposed")])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert 'class="toc"' not in out


def test_article_id_anchor_matches_toc_href():
    doc = _doc([_issue()])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert 'id="iss-VID-001"' in out


def test_totop_link_present_in_article():
    doc = _doc([_issue()])
    out = hx.render_html(doc, embed_fn=FAKE)
    assert 'class="totop"' in out
    assert 'href="#top"' in out


def test_toc_severity_fallback_shows_question_mark():
    iss = _issue()
    iss["severity"] = ""
    doc = _doc([iss])
    out = hx.render_html(doc, embed_fn=FAKE)
    toc_section = out[out.index('class="toc"'):out.index('</nav>')] if 'class="toc"' in out else ""
    assert "?" in toc_section


# ── helper unit tests ───────────────────────────────────────────────────────

def test_list_block_cls_parameter():
    result = hx._list_block("Observed", ["foo"], cls="block observed")
    assert 'class="block observed"' in result
    assert "foo" in result


def test_issue_anchor_uses_label():
    assert hx._issue_anchor({"id": "iss_1", "label": "VID-001"}) == "iss-VID-001"


def test_issue_anchor_falls_back_to_id():
    assert hx._issue_anchor({"id": "iss_1", "label": None}) == "iss-iss_1"


def test_issue_anchor_escapes_html_chars():
    result = hx._issue_anchor({"id": "iss_1", "label": 'A&B'})
    assert "&amp;" in result


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
