# Parallel work tracker (coordinator)

Coordinator brief: `handoff/work/parallel_plan_prompt.md`. Prompts live in `handoff/work/prompts/` (shared rules block in `_common.md`, copied into each prompt). Base URL for links: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/prompts/`.

| Item | Prompt | Status | Last result |
| :-- | :-- | :-- | :-- |
| A. Task 3 design-doc updates | `A-task3-design-doc.md` | prompt ready, must land before stack Task 6 step 10 | |
| B. 4.4 type plan + transport mypy fix | `B-types-plan.md` | prompt ready | |
| C. 4.1 PR 8 plan | `C-pr8-plan.md` | prompt ready (plan phase only) | |
| D. 4.6 groundwork | not written | after A; (e) needs new PR numbers from Brian | |
| E. Task 5 RL re-commit | `E-rl-recommit.md` | **accepted 14:56** (verified by subagent: trees and messages equal, A=C=Brian, parent `769b7f2e`, no trailers) | `e606b9bd` -> `166a15c8`; Brian relays to stack conversation with the stale same-tree note for Task 6 step 6 |
| F1. Task 6 step 1 upstream naming check | subagent, no prompt | **done 12:32**: nothing found on #1988-#1991; coordinator re-checked human comments (only `/ok-to-test` from vicentefb, janetkuo) | CodeRabbit on #1989 (pullrequestreview-5606953886) suggests `members` as a tuple on `GroupReady`; carries over to `GroupOutcome` if still open |
| F11. Task 6 step 11 Go notes rename | not written | after D | |
| G. 4.2 async memory probe | `G-async-mem-probe.md` | prompt ready, reviewed by a fresh subagent | |

## Facts checked by the coordinator (2026-10-22 12:30)
- Live heads equal `state.md` (PR 2 `f3b61c61`, transport `5affc426`, PR 7 `a26979f1`, upstream `255d15fc`, RL `e606b9bd`, `bench/batch-claim` `4a205e9f`, `go-batch-design-notes` `441ec93a`).
- Kind: only cluster `agent-sandbox` exists; current context `kind-agent-sandbox` (not `kind-batch-bench` as the notes say; that cluster is gone). `go-batch-design-notes` is checked out in another conversation's worktree (`.../01d4a753.../scratch/notes-wt`).

## Verification checklist (coordinator, every report; strict for code changes)
Verify in a fresh `inherit` subagent (never flash), given the pushed SHAs and the rules, not the task's own findings. Brian, 2026-10-22: be especially strict on the "Minimal fixes" rules in `working_rules.md` (added `a2ae0d54` after a bloated transport fix).
1. Claims match the pushed commits/files (SHAs, trees, authors, no trailers, lease pushes).
2. Minimal change: what data already existed where the fix runs? Any new state, field, parameter, wider type, duplicated data or logic must carry a one-sentence justification; reject if a smaller change has the same behavior.
3. No production change made to suit a test double.
4. Best practice: idiomatic, DRY, sync/async parity (surface, docstrings, comments, tests), comments explain why, no drive-by changes, one owning test per contract, mutation-checked.
5. Checks run: changed tests both shells, mypy per commit, pycodestyle on changed lines, docs regen no diff.
6. Plans and recommendations: alternatives with costs, smallest option considered first, facts marked measured/verified/inferred.
