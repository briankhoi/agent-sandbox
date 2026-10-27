# Coordinator 2 tracker (items B and C)

Brief: `handoff/work/coordinator2_prompt.md`. Prompts in `handoff/work/prompts/` (`B*`, `C*`), results in `handoff/work/results/` (`B*`, `C*`). Link base: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/`.

| Item | Prompt | Status | Last result | Open decisions |
| :-- | :-- | :-- | :-- | :-- |
| B. Task 4.4 type tightening + transport mypy fix | `prompts/B-types-plan.md`, `prompts/B2-types-fold.md` (ran 10-26; result `results/B2-types-fold.md` verified 23:05: ACCEPT WITH FIXES, missing Raises line), fix `prompts/B3-raises-docstring.md` (reviewed, pushed 23:07) | plan `type_tightening_plan.md` written (pinned at old heads); verified 22:59 (rerun after reboot): ACCEPT WITH FIXES (fix A larger than needed: one-line cast B or A2 smallest, all clean on 36.1.0 and 33.3.0 at `df4dbeb2` and `deee6702`; stub wording wrong; D5 must say the force-reinstall works around kubernetes 36.0.0, already excluded; re-pin SHAs 780b8aa8->df4dbeb2 etc.; typed helper in PR 6 conflicts with PR 7 `d99b3c46`; add `object` keep rows) | report pasted by Brian 22:17 | B approved 00:16 (one-line cast in transport; reaper's own loose types folded into reaper; open-PR and upstream types plus Lease typing in one types PR after PR 7, later). B3 and B5 not decided. Stack not settled (Brian 00:13) |
| C. Task 4.1 PR 8 | `prompts/C-pr8-plan.md` | plan `pr8_plan.md` written (pinned PR 7 `a26979f1`, edited `eca64789`); verified 22:19: ACCEPT WITH FIXES (re-pin to `deee6702`; async metadata-only via direct `call_api` header, no second client; sync route is a decision; line 96 hedge stale; measurement plan needs new `par-` name, v1beta4 kind copy, bench knobs for 5 s timeout and N=10k, BENCH_CONTEXT from `2ad745c0`, re-list item count persisted; test fixtures need a default count; drain with `stream()`, release in `finally`; one shared page iterator per helper; drop managedFields trim) | report pasted by Brian 22:17 (not in results/) | Part 3 approved 00:21. Open: Part 1, sync metadata route (dedicated client recommended), page 500, section 7 correction, metadata-only deletecollection check (optional add-on), managedFields measure only, start after B lands | plan decisions 1 to 5, pending verification |

## Live heads at start (2026-10-22 22:13)
upstream `feature/batch-claim` `d07e8941`; TLS `0e586d47`; PR 2 `520a37b1`; PR 5 `defa5411`; PR 6 `b1e13656`; transport `760f8796` (pool commit `df4dbeb2`); reaper `ab0635f9`; PR 7 `deee6702`; `bench/batch-claim` `4a205e9f`; `bench/async-mem-probe` `2ad745c0`.

## Sequencing
B's mypy fix folds into transport `df4dbeb2` (review copy `review/types/...`, only after Brian says the stack has settled), handed to the stack conversation for sync. C's implementation (`feat/batch-8-<slug>` off live PR 7) starts after B's sync lands, or rebases onto the new PR 7 head.

## Verification of code results (fresh `inherit` subagent, every time)
Brian, 2026-10-22 22:51: "for results containing code and whatnot, please verify that they adhere to the working rules and are best practices/concise, efficient, analyze the related surface area to see if that needs changing, etc. you can use a subagent for that though"
Checklist: pushed SHAs/trees/authors/no trailers; working rules (Minimal fixes: smaller change with same behavior? new state, duplicated data or logic, wider type?); no production change for a test double; best practice, concise, efficient, DRY, sync/async parity, comment style; related surface area (callers, sibling methods, the other shell, tests, docs, mocks/fixtures) checked for needed or missed changes; checks actually run (changed tests both shells, mypy per commit vs base, pycodestyle changed lines, docs regen no diff).

## Brian's answers (verbatim)
- 2026-10-23 00:13: "stack has not settled"
- 2026-10-23 00:16 (B): "and then for b, the stuff that needs types after PR 7 mainly reaper we just fold into their respective PRs right? if so, i approve B" (coordinator 2 confirmed the reading: unopened branches transport, reaper, PR 7 fold their own types; open PRs and upstream code go to one later types PR after PR 7)
- 2026-10-23 00:21 (C): "okay i approve 3 then" (Part 3, don't parse the deletecollection response)
- 2026-10-27 20:37-20:38 (C): page 500 "approve"; start after the B sync "yes"; on the optional items (metadata-only re-list, metadata-only deletecollection, managedFields trim) as separate measured last commits: "approve of proposal but not the drop 20%, just report how much it saves an dlet me make that call". Prompt `prompts/C1-pr8-impl.md` written 20:40, under review.
- 2026-10-27 01:22: "approve 1 and 2" (B copies: transport cast, reaper folds). "for the separate types PR, i support your recommendation" (types PR after PR 8). Asked: is the stub gap fixed upstream, else a fix prompt; Lease typing best practice (subagent). Pausing for a rename of major fields in 13/N and up; Brian will give the rename link on resume.
- 2026-10-26 23:15 (B2 choices: BatchError raise, `dict[str, str]`, `_continue=` direct): "i approved of the b2 decisions"
- 2026-10-22 (`temp-prompts/pr8_scope_and_pacing_answers.md`): "`members()` caching and wake-up changes stay dropped and are not in PR 8 (measured: `members()` 9 ms at 10k claims, wake-ups 0.05 to 0.08 cores). The metadata-only re-list in `release()` IS in PR 8." "PR 8 = claim-list pagination (attach and release re-list), metadata-only re-list in `release()`, and skip parsing the deletecollection body. Follow tasks.md 4.1." "The roadmap's Pacing PR stays, as a later, conditional item".

## B2 copies (10-26 23:01, verified)
live -> copy: transport `64bc8872 -> 041c0b02`, reaper `f03fb3cb -> 64f38bf5` (B3 will replace), PR 7 `fa10f75b -> 819458d6` (B3 will replace). PR 6 `fde1f963`. Verifier recommends keeping all three B2 choices (BatchError raise, `dict[str, str]`, `_continue=` direct).

## C status (10-26 23:00)
Coordinator 2 changed its recommendation on the section 7 fact (release peak is the deletecollection reply): drop metadata-only re-list and metadata-only deletecollection; measure managedFields. Awaiting Brian.

## B3 accepted (10-26 23:26, verified by subagent)
Sync table for the stack conversation (live -> copy): `feat/batch-transport-ltlfdm` `64bc8872 -> 041c0b02`, `feat/batch-reaper-ymtd6t` `f03fb3cb -> 817d7554`, `feat/batch-7a-create-path` `fa10f75b -> bb23b672`. review-diff: transport `8a666513`, reaper `279bafe6`. Waiting for Brian's review of the copies, then the stack conversation syncs.

## Resume here (paused 2026-10-27 01:28 for the 13/N+ rename)
- B copies restacked onto the #1996 restack and approved by Brian ("approve 1 and 2"): transport `20f08f2b -> 6e47d433`, reaper `ede7f3b9 -> bf9fe2e9`, PR 7 `ec50d5e2 -> 2f40fe52`; review-diff transport `9aece81e`, reaper `734b09be`. Brian relays the sync to the stack conversation. If the rename lands first, restack these copies onto the renamed heads (check `git diff <live PR7> <copy PR7>` equals `git diff fa10f75b bb23b672`, adjusting only for renamed identifiers).
- B4 prompt `prompts/B4-asyncio-stub-fix.md` (reviewed, fixed, pushed `06aee0eb`): upstream kubernetes_asyncio `api_client` stub fix, patch and drafts only. Not run yet.
- Types PR (after PR 8, Brian supports): Lease typing research done (subagent 737f227a, prototype diff in its scratch `lease_parts.diff`). Recommendation: one shared `lease_parts(lease) -> (metadata, spec)` helper in `batch_utils.py` raising `BatchError` when metadata or spec is None; spec fields stay Optional (None is a real state). Open: plain BatchError vs BatchLeaseExpiredError; one helper vs spec-only; the async `holder_identity` stub types `str`, and a `# type: ignore[assignment]` would fail on 33.3.0 (`warn_unused_ignores`), so needs another approach (coordinator 2 note).
- C (PR 8): open decisions: drop metadata-only re-list and deletecollection (coordinator 2 recommends drop, on the section 7 fact), managedFields measure only, page 500, start after B sync. On resume, ask Brian for the rename link and inspect it before writing C1.

## Resumed 2026-10-27 20:33 (after the group_outcomes rename sync)
- Live: PR 3 `469b8d77`, PR 4 `a85caf4d`, PR 5 `7f1d6d5d`, PR 6 `51c1814a`, transport `9a26b3c6`, reaper `75ad2006`, PR 7 `a8f80aa6`.
- Types copies already rebased by the rename sync: transport `9fa22b54`, reaper `e531baf3`, PR 7 `91d5ffc4`. Verified by coordinator 2: `git diff a8f80aa6 91d5ffc4` equals the approved patch `git diff fa10f75b bb23b672` (index lines aside); range-diff `!` only on the pool, Lease-ownership and reaper commits; chain linear; 8 commits Brian, no trailers; mypy clean at `91d5ffc4` on 36.1.0 and 33.3.0. Local `review/types/*` refs reset to these. Sync only from these heads.
- My notes and prompts contain no `iter_ready_groups`/`GroupReady` (grep). Task 6 steps 9 to 11 belong to the stack conversation (pr_table/pr_board PR 3 row, notes) and coordinator 1 (Go notes); not done here to avoid conflicting edits.
