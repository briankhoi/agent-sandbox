# Parallel work tracker (coordinator)

Coordinator brief: `handoff/work/parallel_plan_prompt.md`. Prompts live in `handoff/work/prompts/` (shared rules block in `_common.md`, copied into each prompt). Base URL for links: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/prompts/`.

| Item | Prompt | Status | Last result |
| :-- | :-- | :-- | :-- |
| A. Task 3 design-doc updates | `A-task3-design-doc.md` | prompt ready, must land before stack Task 6 step 10 | |
| B. 4.4 type plan + transport mypy fix | `B-types-plan.md` | prompt ready | |
| C. 4.1 PR 8 plan | `C-pr8-plan.md` | prompt ready (plan phase only) | |
| D. 4.6 groundwork | not written | after A; (e) needs new PR numbers from Brian | |
| E. Task 5 RL re-commit | `E-rl-recommit.md` | prompt ready (head `e606b9bd` checked 12:33) | |
| F1. Task 6 step 1 upstream naming check | subagent, no prompt | **done 12:32**: nothing found on #1988-#1991; coordinator re-checked human comments (only `/ok-to-test` from vicentefb, janetkuo) | CodeRabbit on #1989 (pullrequestreview-5606953886) suggests `members` as a tuple on `GroupReady`; carries over to `GroupOutcome` if still open |
| F11. Task 6 step 11 Go notes rename | not written | after D | |
| G. 4.2 async memory probe | `G-async-mem-probe.md` | prompt ready, reviewed by a fresh subagent | |

## Facts checked by the coordinator (2026-10-22 12:30)
- Live heads equal `state.md` (PR 2 `f3b61c61`, transport `5affc426`, PR 7 `a26979f1`, upstream `255d15fc`, RL `e606b9bd`, `bench/batch-claim` `4a205e9f`, `go-batch-design-notes` `441ec93a`).
- Kind: only cluster `agent-sandbox` exists; current context `kind-agent-sandbox` (not `kind-batch-bench` as the notes say; that cluster is gone). `go-batch-design-notes` is checked out in another conversation's worktree (`.../01d4a753.../scratch/notes-wt`).
