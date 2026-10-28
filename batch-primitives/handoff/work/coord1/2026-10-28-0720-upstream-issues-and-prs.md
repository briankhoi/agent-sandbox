# Concise self-contained Issues 1–3 and PRs 1–3 in `issues-final.md`

Requests from Brian:
- "reword. first of all, the \"What happened?\" section needs to be way more concise and to the point. secondly, stop referring to branches or directories or whatever. you are working on my fork. we are filing an upstream issue. they do not have access to my fork. it should be self contained, minimal, and concise as possible/needed. reword."
- "also, for the issues. i plan to just file issues 1 - 3. and make PRs 1-3. so please stop referring to like issue 1, 2, 3 etc like the issues + PRs should be self contained too. and make the repro scripts minimal. make it as easy on the upstream maintainers as possible"
- "also, you have PR descritpions + titles + release notes too for me to paste or no? the release note is this section, should be 1 sentence max"

## Sections in `issues-final.md`

- **Issue 1 (`#2082`):** [`issues-final.md#L5`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L5)
- **Issue 2:** [`issues-final.md#L62`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L62) — minimal 13-line direct `AsyncSandbox` repro with zero mocks or cross-issue workarounds, verified against `main` (`True`/`False`) and PR 2 (`False`/`False`).
- **Issue 3:** [`issues-final.md#L111`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L111)
- **PR 1 (`fix/python-sdk-release-terminated-handles`):** [`issues-final.md#L166`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L166) (`Fixes #2082`, 1-sentence release note)
- **PR 2 (`fix/python-sdk-free-closed-connectors`):** [`issues-final.md#L205`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L205) (`Fixes #<issue 2>`, 1-sentence release note)
- **PR 3 (`fix/python-sdk-release-client-exit-hook`):** [`issues-final.md#L243`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L243) (`Fixes #<issue 3>`, 1-sentence release note, stacked note for `#<PR 1>`)
