# Coordinator 2 tracker (items B and C)

Brief: `handoff/work/coordinator2_prompt.md`. Prompts in `handoff/work/prompts/` (`B*`, `C*`), results in `handoff/work/results/` (`B*`, `C*`). Link base: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/`.

| Item | Prompt | Status | Last result | Open decisions |
| :-- | :-- | :-- | :-- | :-- |
| B. Task 4.4 type tightening + transport mypy fix | `prompts/B-types-plan.md` | plan `type_tightening_plan.md` written (pinned at old heads, PR 7 `a26979f1`); verification running 2026-10-22 22:15 | report with Brian (requested) | D1 to D5 in the plan, pending verification |
| C. Task 4.1 PR 8 | `prompts/C-pr8-plan.md` | plan `pr8_plan.md` written (pinned PR 7 `a26979f1`, edited `eca64789`); verified 22:19: ACCEPT WITH FIXES (re-pin to `deee6702`; async metadata-only via direct `call_api` header, no second client; sync route is a decision; line 96 hedge stale; measurement plan needs new `par-` name, v1beta4 kind copy, bench knobs for 5 s timeout and N=10k, BENCH_CONTEXT from `2ad745c0`, re-list item count persisted; test fixtures need a default count; drain with `stream()`, release in `finally`; one shared page iterator per helper; drop managedFields trim) | report pasted by Brian 22:17 (not in results/) | plan decisions 1 to 5, pending verification |

## Live heads at start (2026-10-22 22:13)
upstream `feature/batch-claim` `d07e8941`; TLS `0e586d47`; PR 2 `520a37b1`; PR 5 `defa5411`; PR 6 `b1e13656`; transport `760f8796` (pool commit `df4dbeb2`); reaper `ab0635f9`; PR 7 `deee6702`; `bench/batch-claim` `4a205e9f`; `bench/async-mem-probe` `2ad745c0`.

## Sequencing
B's mypy fix folds into transport `df4dbeb2` (review copy `review/types/...`, only after Brian says the stack has settled), handed to the stack conversation for sync. C's implementation (`feat/batch-8-<slug>` off live PR 7) starts after B's sync lands, or rebases onto the new PR 7 head.

## Verification of code results (fresh `inherit` subagent, every time)
Brian, 2026-10-22 22:51: "for results containing code and whatnot, please verify that they adhere to the working rules and are best practices/concise, efficient, analyze the related surface area to see if that needs changing, etc. you can use a subagent for that though"
Checklist: pushed SHAs/trees/authors/no trailers; working rules (Minimal fixes: smaller change with same behavior? new state, duplicated data or logic, wider type?); no production change for a test double; best practice, concise, efficient, DRY, sync/async parity, comment style; related surface area (callers, sibling methods, the other shell, tests, docs, mocks/fixtures) checked for needed or missed changes; checks actually run (changed tests both shells, mypy per commit vs base, pycodestyle changed lines, docs regen no diff).

## Brian's answers (verbatim)
- 2026-10-22 (`temp-prompts/pr8_scope_and_pacing_answers.md`): "`members()` caching and wake-up changes stay dropped and are not in PR 8 (measured: `members()` 9 ms at 10k claims, wake-ups 0.05 to 0.08 cores). The metadata-only re-list in `release()` IS in PR 8." "PR 8 = claim-list pagination (attach and release re-list), metadata-only re-list in `release()`, and skip parsing the deletecollection body. Follow tasks.md 4.1." "The roadmap's Pacing PR stays, as a later, conditional item".
