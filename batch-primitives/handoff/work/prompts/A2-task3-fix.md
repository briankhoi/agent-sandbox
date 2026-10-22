# A2. Task 3 fix: trim commit 00781c63 to the minimal change

Paste the block below into the conversation that ran prompt A (preferred), or into a fresh one. Re-pasting is safe: check each item against the current file first.

````
## Context and rules (same as prompt A)
Notes worktree /usr/local/google/home/brianknguyen/agent-sandbox-notes, branch `batch-primitives-notes`, directory `batch-primitives/`. Commits authored by `Brian Nguyen <brianknguyen@google.com>`, no `Co-Authored-By`/`Claude-Session` trailers, never `--no-verify`, plain push to `origin` only (never force). `timeout 60 git pull --rebase` right before committing and again before pushing; `git add` only the files you changed. Never comment on GitHub or open PRs. Do not edit `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Any subagent uses the `inherit` model.
Rules that apply here (`handoff/working_rules.md`, `handoff/project_instructions.md`): leave Brian's wording alone except where the code it describes changed, and then change it minimally. Minimal fixes: the smallest change with the same effect; no duplicated information.

## Goal
The coordinator's independent review of `00781c63` passed it on accuracy, but found text beyond the minimal change. Fix these four items in one follow-up commit (prefix `notes:`), changing nothing else:
1. `batch_claim_proposal.md:58`: Brian's original said "Each group's first `MinReady` claims go first". Keep "go first" (the code change does not require "are created first"). Drop the added rationale clause "so the first groups can be consumed while later groups are still being created" unless the paragraph is wrong without it (it is not; the A/B example already removes the ambiguity). Keep the A/B example and the corrected ordinal sentence. Target: Brian's original paragraph with only the words that described the interleaved order replaced.
2. `review_carryover_notes.md`, roadmap PR 2 bullet: it repeats the full order description and quote already in the PR 2 row. Shorten it to one line pointing at the row, for example: The fill's create order (PR 2 row above) moved here from PR 7: "ok to move group order into PR 2 and keep other items in PR 7/8" (Brian, 2026-10-21 18:28).
3. `review_carryover_notes.md`, PR 2 row: cut the rerun-review sentence ("measured ... 0.65 s vs 0.16 s ... sections 2 and 3A, which proposed it for PR 7 with groups sorted by min_ready ...") to: The group-by-group order replaced that interleaving (evidence: `handoff/refs/benchmark_rerun_review.md` 3A). Keep the history of the earlier interleaved order and its 2026-10-13 approval as they are.
4. `handoff/refs/work_plan.md`, PR 8 row: replace the placeholder branch `feat/batch-8-...` with "not created". Leave the labeled members()/wake-ups proposal note as is.

## Checks
- `git diff 00781c63 HEAD` touches only these four spots.
- No em dashes added, quote verbatim, no new detail.
- Self-check: one fresh `inherit` subagent given only the diff of `00781c63^..HEAD` for `batch_claim_proposal.md` and the rule "Brian's wording kept except where the described code changed; smallest change; is there a smaller change with the same behavior? does it duplicate data?" Resolve its findings.

## Report
One-line summary; the final before/after of `batch_claim_proposal.md:58` (before = the text at `00781c63^`, after = the new text), each in its own fenced block, for Brian's Google Doc; self-check result; commit link `https://github.com/briankhoi/agent-sandbox/commit/<sha>`.
````
