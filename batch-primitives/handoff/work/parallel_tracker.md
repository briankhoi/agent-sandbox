# Parallel work tracker (coordinator)

Coordinator brief: `handoff/work/parallel_plan_prompt.md`. Prompts live in `handoff/work/prompts/` (shared rules block in `_common.md`, copied into each prompt). Base URL for links: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/prompts/`.

| Item | Prompt | Status | Last result |
| :-- | :-- | :-- | :-- |
| A. Task 3 design-doc updates (**accepted 23:05**, `ac653680`) | `A-task3-design-doc.md`, fix `A2-task3-fix.md` | `00781c63` accurate (subagent PASS 15:08), fix prompt A2 for 4 minimality nits (Brian's wording at proposal:58, duplicated PR 2 bullet, extra rerun sentence, placeholder branch) | `00781c63` |
| B. 4.4 type plan + transport mypy fix | `B-types-plan.md` | plan written (`type_tightening_plan.md`); **handed to coordinator 2** (`coordinator2_prompt.md`) 21:51 | |
| C. 4.1 PR 8 plan | `C-pr8-plan.md` | plan written (`pr8_plan.md`, edited by `eca64789`); **handed to coordinator 2** 21:51 | |
| D. 4.6 groundwork | not written | after A; (e) needs new PR numbers from Brian | |
| E. Task 5 RL re-commit | `E-rl-recommit.md` | **accepted 14:56** (verified by subagent: trees and messages equal, A=C=Brian, parent `769b7f2e`, no trailers) | `e606b9bd` -> `166a15c8`; Brian relays to stack conversation with the stale same-tree note for Task 6 step 6 |
| F1. Task 6 step 1 upstream naming check | subagent, no prompt | **done 12:32**: nothing found on #1988-#1991; coordinator re-checked human comments (only `/ok-to-test` from vicentefb, janetkuo) | CodeRabbit on #1989 (pullrequestreview-5606953886) suggests `members` as a tuple on `GroupReady`; carries over to `GroupOutcome` if still open |
| F11. Task 6 step 11 Go notes rename | not written | after D | |
| G. 4.2 async memory probe | `G-async-mem-probe.md` | report in (21:49): not arena fragmentation, terminated handles retained in `_active_connection_sandboxes` (~0.7 MB each); verification running | `origin/bench/async-mem-probe` `2ad745c0` |

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

## Conventions (from 2026-10-22 21:50)
- Task conversations write reports to `handoff/work/results/<prompt file name>`; Brian passes the link.
- Coordinator 2 (`handoff/work/coordinator2_prompt.md`, tracker `coordinator2_tracker.md`) owns B and C from here. Coordinator 1 keeps A, D, E/G, F and the sequential list.
- Brian's answers to A's decisions (2026-10-22): PR 8 = pagination (attach and release re-list) + metadata-only re-list in release() + skip the deletecollection body; members() cache and wake-ups stay dropped. The Pacing PR stays as a later, conditional item. Recorded in notes by A2.
- `review/notes-stale-fix` (`eca64789`) fast-forwarded into `batch-primitives-notes` at 21:49.

## Log (newest last)
- 23:10 A2 accepted; G2 decision brief `coord1/2026-10-22-2310-a2-g2-verdicts.md`, prompt `G3-memleak-fix2.md` waits on Brian; D running as a subagent (results `results/D-small-items.md`). Replies to Brian go in `handoff/work/coord1/` (Brian: "communicate over .md").
