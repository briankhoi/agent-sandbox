# Stack state (as of 2026-10-22 01:15 UTC)

Always `git fetch origin && git fetch upstream` and use the live heads. The hashes below are a snapshot, and anything newer than this file wins. If the live state differs from this file in a way that matters, tell Brian.

## Glossary (two numbering systems)
- **Upstream "k/N"** is the title suffix of each upstream PR, in merge order. "PR 6/N" means upstream PR #1984 (branch `feat/batch-1-fu5-connect-checks`), not the SDK feature "PR 6" (acquire). Brian's message: "5/N also just got merged so it would rebase and get PR 6/N ready for review."
- **SDK "PR n"** names the feature PRs in the notes: PR 2 claim_batch, PR 3 events, PR 4 wait_for_quorum, PR 5 release, PR 6 acquire, PR 7 (= "7a", create-path pacing), PR 8 (= "7b", pagination/release list work, not built). Brian calls 7a "PR 7" and 7b "PR 8".
- **fuN** are the follow-ups to PR 1 (fu1 to fu9) that Brian split out of the original PR 1 after review.
- Upstream base branch for the whole stack is `feature/batch-claim` (maintainer: Janet Kuo). Upstream `main` is separate; the fork's `origin/main` lags.

## Merged upstream
PR 1 #1742 (squashed as `3a1607bc`), fu1 (2/N), fu2 (3/N), merge-main #2005, fu3 (4/N) #1982 (upstream tip `4bb0db30`), and **fu4 (5/N) #1983, merged 2026-10-21 as squash commit `cc4a40bf` (tree `663a0951`, equal to the tree of our fu4 branch), directly on `4bb0db30`**. Squash merges keep the tree but change hashes. After 5/N the lowest open PR is fu5 (6/N).

## Branch order (linear, bottom to top)
Each branch sits on the one before it. Upstream PR numbers from the project's tracking (Brian opens PRs himself, so check):

| Upstream | Branch | Head (10-22 01:05) | Upstream PR |
| :-- | :-- | :-- | :-- |
| 5/N | `feat/batch-1-fu4-terminating-claims` | `2b8424a1` | #1983 **merged** |
| 6/N | `feat/batch-1-fu5-connect-checks` | `e295ca75` | #1984 open |
| 7/N | `feat/batch-1-fu6-group-order` | `6d6f9b1f` | #1985 |
| 8/N | `feat/batch-1-fu7-client-cleanup` | `4127bcde` | #1986 |
| 9/N | `feat/batch-1-fu8-task-names`, `feat/batch-1-fu9-concurrent-detach` (fu9 contains fu8) | fu8 `da3893bf`, fu9 `8178a4c4` | #1996 (fu9 branch) |
| 10/N | `feat/batch-2-claim` (PR 2) | `f293a7e3` | #1824 |
| 11/N | `feat/batch-3-group-consumers` (PR 3) | `58a7b78b` | #1988 |
| 12/N | `feat/batch-4-group-quorum` (PR 4) | `d7ac54d7` | #1989 |
| 13/N | `feat/batch-5-release-group-76a8m3` (PR 5) | `ba690d94` | #1990 |
| 14/N | `feat/batch-6-acquire-nbbp5j` (PR 6) | `2f520b67` | #1991 |
| 15/N (inferred) | `feat/batch-transport-ltlfdm` | `668f20c9` | not opened |
| 16/N (inferred) | `feat/batch-reaper-ymtd6t` | `8bc78f11` | not opened |
| 17/N (inferred) | `feat/batch-7a-create-path` (PR 7, top) | `24a4e9a1` | not opened |

These heads are the Task 1 restack onto the 5/N merge (upstream `cc4a40bf`), synced by atomic push on 2026-10-22 01:05 after Brian's OK. Every commit's tree is unchanged from the 22:15 heads (fu5 `c4cbce8e` ... PR 7 `769b7f2e`); only the base moved. fu4 is no longer in the stack. Pending review copies on top of these: `review/tls-eof/*` (Task 2, pushed 2026-10-22 01:15, not synced): the async `_is_transport_error` retries a TLS EOF, folded into PR 2's "add claim_batch and release". Heads PR 2 `871aeb63`, PR 3 `5327de63`, PR 4 `6586b882`, PR 5 `468bb627`, PR 6 `f6430b1a`, transport `852f9693`, reaper `e1382970`, PR 7 `b6b920ea`. Sync side waits on Brian's TLS PR decision (`feat/batch-ssl-eof-retry`). Numbers for transport (15/N), reaper (16/N) and PR 7 (17/N) are inferred from the stack order, since none is opened; Brian decides. `refs/pr_table.md` was renumbered to match this table and holds titles and description text.

Side branches: `feat/batch-ssl-eof-retry` (`8937ccdb`, TLS EOF retry, based on upstream `4bb0db30`, not opened upstream; the old `claude/project-thread-0ysqcj` is kept as a backup), `fix/python-terminal-pod-reasons` (upstream #1917, Brian's). `bench/batch-claim` holds the SDK benchmark harness (`test/benchmarks/batch-claim/`, `bench.py`). `bench/rl-claim-mode` and `claude/project-thread-c7716c` are RL work (not yours). Many `review/*` and `review-diff/*` branches on `origin` are old review copies, safe to ignore (deleting needs Brian's OK).

## What the stack contains (one line each)
fu4 terminating claims become LOST. fu5 `connect()` checks readiness and `err()` before the cached connection. fu6 `get_batch` orders groups by lowest ordinal. fu7 client cleanup closes batch connections. fu8 names async tasks. fu9 concurrent `detach()`. PR 2 `claim_batch` and `release`, with the fill created **group by group in caller order, each group's first `min_ready` claims first** (moved here from PR 7 on 2026-10-21). PR 3 `events`, `iter_ready_groups`. PR 4 `wait_for_quorum`. PR 5 `release_member`, `release_group`, `release_not_ready`. PR 6 `acquire`, `size=0` groups, the next-ordinal annotation, a per-pool single-flight precheck (`_check_pool`, shared outcome). Transport: `connect()` seeds the pod IP, `pool_size`, `pool >= max_in_flight + 2`. Reaper: ownerReference on claims, `python -m k8s_agent_sandbox.batch_reaper` deletes only stale Leases. PR 7 (7a): warm-aware create and acquire pacing (warm credit from the precheck GET's `status.readyReplicas`).

## Approved design decisions
Brian's words are quoted where recorded. Everything else here was approved by him or settled on his delegation.
- Features in the proposal stay until Brian approves removal. Cut with his approval ("so i do agree with the list of stuff it recommended to cut"): `replace`, `wait_for_quorum`'s `timeout`, the `LEASE_DEGRADED` event, `adopt_expired`. `adopt_expired` stays cut; the docs PR gets a `detach(grace=...)` preemption line. Kept: `release_not_ready()`, lazy `size=0` groups, `BatchClaimer`, `fleet.run(wave=True)`.
- Brian decided on 2026-10-19 to make the stack linear (the cloud memory records this; the verbatim wording was not found).
- **Group order (approved 2026-10-21 18:28):** the fill creates claims group by group in caller order, `min_ready` claims first, in PR 2. This replaces the earlier round-robin/"interleaved" order (#11/#12). Each group's ordinals stay contiguous.
- No Lease `batch-size` annotation (Brian chose "Per-claim", 2026-10-13). `release()` gives up after three delete rounds with no progress ("#46 is approved").
- PR 5: caller-called `release_group(group)`; group label = SHA-256 first 16 hex of the pool name; `deletecollection`; `release_not_ready()` touches fill claims only; skipped creates count as released; no deletes for LOST/CreateFailed members. No automatic failed-group delete.
- PR 6: `TerminalMemberError(BatchError)` with `member`; `acquire` never retries with a new claim; the Lease stores caller labels and `create_rps`; `acquire` raises `BatchError` chained from `err()`; "Free all" (acquired claims carry the group label so `release_group` frees them); an all-size-0 `wait_for_quorum`/`iter_ready_groups` returns `[]` on both handles (matches shipped code, Brian 2026-10-21). Precheck single-flight per pool with a shared outcome (2026-10-21).
- `members()` drops released members (#27). `Member.state` enum approved. #41 pool >= `max_in_flight` + 2. #43 dropped ("i also agree with not doing #43"). #42 (no Sandbox GET at connect fan-out) met.
- Defaults `create_rps` 50 and `max_in_flight` 20 stay; PR 7's warm-aware pacing is how they win on warm pools. Brian's goal: "i want them to be good engouh that batch claiming is way better - otherwise, it's alughable we spent so much on this for no return".
- Cap semantics: `max_in_flight` caps in-flight creates only (holding until Ready was rejected).
- Reaper: ownerReference on claims, option B ("sure i approve B"): the reaper deletes only Leases; claims are left to garbage collection. Default `--skew-margin` 60 s. Sync only.
- Naming: the Python distribution is `k8s-agent-sandbox`, the import `k8s_agent_sandbox`, the directory `agentic-sandbox-client`.
- RL (not yours): Design C approved 2026-10-21 09:49, a batch-backed `fleet.acquire/release` behind `FleetConfig(claim_mode="batch")`; RL Lease default 300 s approved 2026-10-21 18:28 (60 s was the alternative). These set the SDK's Lease expectations; do not change Lease semantics without asking.
- Pending approval (proposals, not approved): `create_rps` default 200 (after the rerun, superseded by PR 7's pacing), a p50 check in the RL prototype prompt, type tightening (see `tasks.md`).

## Measured facts you may need
- SDK benchmark (kind, Brian's Mac, maxPods 400): no `create_rps` default won clearly before PR 7. Warm all-Ready floor is about 0.9 s for 160 claims (controller adoption). Cost win: 4.8x fewer requests, 11x fewer watches. Details in `refs/benchmark_rerun_review.md`.
- Kind runs about 400 pods per node, so about 160 sandboxes per step.
- Async native memory: the baseline's async loop grew 93 to 514 MB, the batch's 94 to 282 MB; Python-traced memory stayed flat at 11 MB (existing growth, not batch state).

## Where things are on the notes branch (`batch-primitives-notes`, directory `batch-primitives/`)
`batch_claim_proposal.md` (the design; Brian updates his Google Doc by hand), `review_carryover_notes.md` (rules, approved decisions, roadmap), `reaper_contract.md`, `pr2_implementation_spec.md`, `pr4_implementation_spec.md`, `pr2_design.md`, `skills/test-audit/SKILL.md`, and this `handoff/` folder. `STALE_*` files and `stale_python_sdk_batch_plan.md` are stale: never read or use them. `pr1_revision_prompt.md` and `pr2_pr3_review_response_prompt.md` are history. The cloud-only `work_plan.md` is copied to `refs/work_plan.md` (it is partly out of date; Task 3 updates what matters).

## Open threads in the cloud project (not yours)
RL integration (Design C prototype, limits rerun on `bench/rl-claim-mode`, TL meeting). They may produce SDK change requests; Brian will paste those to you.
