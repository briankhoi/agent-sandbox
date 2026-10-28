# Parallel work tracker (coordinator)

Coordinator brief: `handoff/work/parallel_plan_prompt.md`. Prompts live in `handoff/work/prompts/` (shared rules block in `_common.md`, copied into each prompt). Base URL for links: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/prompts/`.

| Item | Prompt | Status | Last result |
| :-- | :-- | :-- | :-- |
| A. Task 3 design-doc updates (**accepted 23:05**, `ac653680`) | `A-task3-design-doc.md`, fix `A2-task3-fix.md` | `00781c63` accurate (subagent PASS 15:08), fix prompt A2 for 4 minimality nits (Brian's wording at proposal:58, duplicated PR 2 bullet, extra rerun sentence, placeholder branch) | `00781c63` |
| B. 4.4 type plan + transport mypy fix | `B-types-plan.md` | plan written (`type_tightening_plan.md`); **handed to coordinator 2** (`coordinator2_prompt.md`) 21:51 | |
| C. 4.1 PR 8 plan | `C-pr8-plan.md` | plan written (`pr8_plan.md`, edited by `eca64789`); **handed to coordinator 2** 21:51 | |
| D. 4.6 groundwork | subagent | **done 23:12**, decisions in `coord1/2026-10-22-2315-d-decisions.md` (7 items; #1990 targets main) | `results/D-small-items.md` `4a5ad938` |
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
- 23:15 D decisions brief pushed; waiting on Brian for D 1-7 and G3.
- 23:39 Brian rejected lazy prune (a) (1k terminate then no creates). Independent audit `results/leak-fix-audit.md` (`4e4ee715`): hook in terminate + cycle break. Brief `coord1/2026-10-22-2350-leak-fix-decision.md`; G3 to be rewritten after Brian answers. #1990 retargeted by Brian.
- 2026-10-23 22:39 Brian approved the hook (decision 1) and two PRs with one issue each. G3 rewritten and reviewed; waiting on decision 3 (sync loop fix) to fill the placeholder, and whether to draft issues 3/4 (list_active_sandboxes drops live claims; atexit pins clients).
- 2026-10-26 18:35 Brian: yes to drafting issues 3 and 4. Connector close audit `results/connector-close-audit.md` (`cf820fd7`): sync close() does two jobs (naming/intent), recommends a small connector method for PR 2 in both shells. Brief `coord1/2026-10-26-1835-connector-close.md` with 3 decisions; G3 still has PENDING lines.
- 2026-10-26 19:08 Brian: PR 2 audit approach yes, PR 3 (exit hook) yes, issue 5 yes (not named in #1785). G3 filled and reviewed; PR 3 implementation waits for Brian to pick an option (async close() is not final; trade-offs).
- 2026-10-26 21:35 G3 done: PR 1 `c2c006ee`, PR 2 `b3574fa9` verified PASS. Exit hook audit `results/exit-hook-audit.md` (`89419a66`) recommends option (e). Brief `coord1/2026-10-26-2135-leak-fix-verdict.md` with 5 decisions.
- 2026-10-26 23:28 Hook audit `results/hook-registry-audit.md` and use-after-close audit `results/use-after-close-audit.md` agree with Brian. G4 prompt (PR 1 registry-bound hook, PR 2 sentence, PR 3 option e stacked on PR 1, combined verification) reviewed and pushed. After G4: coordinator runs a fresh verifier on the built branches.

## Resume here (paused by Brian 2026-10-27 01:30 UTC)
Brian paused this conversation while another agent renames major fields in PRs 13/N and up. **On resume, ask Brian for the rename link (commit or compare) and read it before any work on the stack or the Go notes.**

| Item | State | Next |
| :-- | :-- | :-- |
| G leak fix (3 PRs on `main`) | G6 verified PASS (`coord1/2026-10-28-0615-g6-verified-ready-to-open.md`); heads PR 1 `9feeb8a9`, PR 2 `fbbbb944`, PR 3 `368cedb6` | Brian files issues A, B, 4, 5 (3 optional) from `results/issues-final.md`, opens PR 1, PR 2, then PR 3 after PR 1 (`results/G6-writing.md`), and gives numbers for `pr_board.md` |
| D small items | decisions brief `coord1/2026-10-22-2315-d-decisions.md`; #1990 base fixed by Brian | Brian's answers to items 2 to 7 (branch deletion of 49 stale `review/*` needs his explicit OK) |
| F step 11 (Go notes rename) | not started | after the stack's Task 6 rename and the new field rename; read the rename link first |
| B, C | owned by coordinator 2 | none for coordinator 1 |
| `par-mem` kind cluster | still running (memory probe) | ask Brian before deleting |
- 2026-10-27 20:40 Resumed after the group_outcomes rename sync. No local copies of renamed branches; nothing of coordinator 1's built on them. Task 6 step 10 (proposal, carryover, work_plan) and step 9 (pr_board) done in `492a556b`; step 11 Go done in `af1dd651` (old head 441ec93a). state.md, tasks.md, pr_table.md, project_instructions.md left to the stack conversation. Open question: Go-name prose `IterReadyGroups` in the proposal (lines ~90-182).
- 2026-10-27 20:55 G5 verified PASS: PR 2 `e62610f9` on main `c4ed9ba9`, PR 3 `afae285e` on PR 1 `0a54a028`. All three ready for Brian to open; issues A, B, 4, 5 (3 optional) to file. D on hold until everything else is done (Brian). par-mem deletion pending Brian's yes.
- 2026-10-27 21:17 par-mem kind cluster deleted (Brian: "clean up par-mem"). kind-agent-sandbox untouched, context unchanged.
- 2026-10-27 22:55 Brian: leave the ~0.5 KB per closed client as is ("leave as is for now"). Issues rewritten into the bug template with verified repro scripts (`results/issues-final.md`, `63f27ea1`, repro check PASS). Writing audit against upstream AGENTS.md Writing section (`results/writing-audit.md`); G6 prompt applies it plus the repro check's text fixes.
- 2026-10-28 06:15 G6 verified PASS by fresh `inherit` verifier (`coord1/2026-10-28-0615-g6-verified-ready-to-open.md`): PR 1 `9feeb8a9`, PR 2 `fbbbb944` (includes `sandbox.py:199-202` sync/async split comment), PR 3 `368cedb6` on `9feeb8a9`, `bench/async-mem-probe` `2ec456da`. Zero AST diffs, tests/mypy/pycodestyle/merge-tree clean, Writing compliance verified across code, commits, PR texts, and all 5 issues. Ready for Brian to file issues A, B, 4, 5 (3 optional) and open PRs 1, 2, then 3.
