---
name: build-html
description: >-
  Build the self-contained HTML report from a reviewed session's issues.json.
  Use after process-session — when the user has triaged issues in the review
  console and says to continue / build the report / build the HTML. Acts on a
  project folder under projects/.
---

# build-html — Phase B: reviewed issues.json → report.html

The follow-up to `process-session`. Run after the user has finished triage in the
review console. From the repo root:

```
python html_export.py --project <project> -o "projects/<project>/review_report.html"
```

This reads `projects/<project>/issues.json` (resolved from the project config),
renders one section per accepted/edited issue with its evidence and the picked
screenshot embedded as base64, and writes a single portable HTML file. Report the
output path.

## Resolving `<project>`
- If the user named a project, use it.
- If they just said "continue" / "build the report" in the same session as a prior
  `process-session`, use that project.
- Otherwise ask, or fall back to the repo-root `config.json` if one exists (run
  `python html_export.py` with no `--project` — back-compat path).

## Notes
- Accepted issues whose facets aren't all picked-or-skipped still export, but surface
  in a warning banner. If the console reports unpicked facets, mention it; the report
  is still produced.
- The review server from `process-session` does not need to be running for this step.
