# extract-issues — transcript → grounded `issues.json` (Phase 2, Step 4)

The automated path from a cleaned transcript to a schema-v2 `issues.json`. Replaces
the throwaway `build_fixture.py`. This is a **four-pass agent workflow** wrapped
around a deterministic harness (`extract_issues.py`):

| Pass | Name | Owner | What it does |
|---|---|---|---|
| 1 | window-extract | **you (agent)** | Read each overlapping window; emit candidate issues, each grounded with a verbatim quote. Bias to recall. |
| 2 | merge / group | **you (agent)** | Dedupe cross-window repeats; apply the Grouping Rule to bundle only same-screen / same-fix-domain trivial defects. |
| 3 | adversarial audit | **you (agent), fresh read** | Re-read the full transcript + the draft list; report ONLY misses, mis-severities, false positives. |
| 4 | validate | **code** | `extract_issues.assemble` → resolve grounding, `renumber`, `validate_or_raise`, atomic save. The only pass that can fail the run. |

The harness owns everything deterministic: windowing, resolving each quote to an
**authoritative `ts_seconds`**, building the schema, and the hard validate gate.
You own the three fuzzy passes. You hand the harness a JSON list of candidates; it
does Pass 4.

Run all four passes — do not shortcut to a single read. The windowing is what
keeps recall up on a long transcript; the audit is the second reader that catches
what the first drops.

## Run it

```bash
# 1. See the windows you'll read (line + timestamp prefixed per cue).
python extract_issues.py windows projects/<session>/input/<t>.txt --text
#    JSON form (for tooling): drop --text. Tune --size / --overlap (cues).

# 4. After you've produced candidates.json (passes 1-3 below), assemble + validate:
python extract_issues.py assemble candidates.json projects/<session>/input/<t>.txt
#    Writes config.json:issues_path and an atomic, validated issues.json.
#    Any ungrounded quote, missing title, or bad enum aborts the write.
```

`assemble` reads `config.json` for `video_path`, `frame_offsets_seconds`, and the
output `issues_path`. It re-derives every `ts_seconds` from the transcript — your
candidates never set timestamps.

## Candidate contract (what you emit)

`candidates.json` is a JSON array of issues (or `{"session": {...}, "issues": [...]}`):

```json
{
  "title": "Single-select dropdown requires an extra 'Done' click to close",
  "severity": "S1",
  "categories": ["Functional", "UI"],
  "confidence": "High",
  "affected_area": "Perform task - dropdown inputs",
  "affected_roles": ["All users"],
  "observed": ["A single-select dropdown requires clicking the selection and then 'Done'."],
  "expected": ["No separate 'Done' is needed; it closes on selection."],
  "notes": [],
  "origin": "window_extract",
  "anchors": [
    {
      "caption": "Single-select requires extra 'Done' click",
      "quote": "I shouldn't have to hit the done button to get rid of the drawer",
      "line_hint": 548
    }
  ]
}
```

- **`quote` must be a verbatim substring of the transcript** (whitespace/case are
  tolerated; wording is not). The harness finds it and sets `ts_seconds` to the
  start of the cue it begins in. If it can't find the quote, the run fails — so
  copy the quote straight from the window text, don't paraphrase.
- **`title` is required and must be scannable.** A title-less issue fails Pass 4.
  Style/quality bar: the 44 titles in `build_fixture.py` `TITLES`.
- `line_hint` (optional) is the `[Lnnn]` from the window; it disambiguates a
  phrase the tester repeats. `transcript_ref` (optional) sets a wider context
  span — otherwise the harness uses the tight matched-cue span.
- `origin`: `window_extract` (pass 1) or `audit_added` (pass 3 recoveries). Audit
  recoveries are stamped `provenance.audit_added = true`.

## Pass 1 — window-extract (recall)

For each window from `extract_issues.py windows`:
- Treat as an issue any line where the tester says it's wrong / broken / confusing
  / inconsistent / should-be-different, plus silent failures and called-out edge
  cases (see `projects/2-clipboard-20260604/input/llm_prompt.md` for the trigger
  list and the S0–S4 rubric + 7-category enum — both are authoritative).
- One verbatim `quote` per anchor. If unsure it's a real defect, still include it
  and set `"confidence": "Low"` — recall first; the human and the audit prune.
- Windows overlap by design; a defect described across a boundary will appear
  whole in one window. Emitting it from both is fine — Pass 2 dedupes.

## Pass 2 — merge / group

- **Dedupe:** the same underlying defect mentioned in multiple windows → one
  issue. Keep separate when the root cause likely differs (same symptom, different
  feature area).
- **Group** (`group.is_group: true` + `source_labels`) bundles *distinct* small
  defects into one issue. Only when **all** hold: same screen / component family,
  same fix domain, each small and trivially fixable, and **none** is Functional /
  Data integrity / Permissions. Severity of a group = its most severe facet. The
  harness hard-blocks grouping a Functional/Data-integrity/Permissions issue
  (`assert_groupable`); the same-screen / same-fix-domain judgment is yours.
- **Multiple anchors ≠ a group.** One defect can carry several evidence anchors
  (e.g. an overdue-status bug shown on both the badge and the due time) — leave
  `group` unset; `is_group` stays false and the ungroupable-category floor does
  not apply. Set `is_group: true` only for a deliberate bundle of separate defects.
- Grouping spares devs a pile of tiny same-surface tickets — never group merely to
  shrink the list, never across fix domains.

## Pass 3 — adversarial audit (fresh read)

Run this as a **second, independent reading** — not a continuation of the pass-1/2
thread, or it rubber-stamps its own output. Re-read the full transcript and the
draft list (titles + severities + quotes) and report ONLY:
- **misses** — defects the draft dropped (emit as new candidates, `origin:"audit_added"`),
- **mis-severities** — wrong S-level vs the rubric,
- **false positives** — explanatory narration drafted as a defect.

Fold accepted recoveries into `candidates.json` before assembling.

## Pass 4 — validate (code, the hard gate)

`assemble` runs `issues_store.renumber` + `validate_or_raise`. It enforces: the
category enum, severity ∈ S0–S4, a non-empty `title`, a verbatim-grounded `quote`
+ integer `ts_seconds` per anchor, and the full anchor shape (`candidate_frames:
[]`, `selected_frame: null`, `frame_status:"pending"`). On any failure it raises
and writes nothing — never a half file.

## Operational notes

- **Do not re-run over a reviewed `issues.json`.** Each run mints fresh `iss_*`
  ids, so re-extracting onto a reviewed doc orphans its picked frames. A re-run is
  a new session file.
- **Window size/overlap** (`--size` / `--overlap`, in cues) is the recall knob;
  defaults 120 / 30 (~10 windows on `2_Task.txt`). Widen overlap if you find
  defects split across a boundary getting dropped.
