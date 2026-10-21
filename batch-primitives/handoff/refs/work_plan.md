# Batch claim work plan (approved 2026-10-08 with changes)

Main objective: finish batch claiming in the Python SDK. After that, use it in Agent Sandbox RL and benchmark it against today's per-claim path.

The decisions behind this plan are in `review_carryover_notes.md` on `batch-primitives-notes`, under "Approved review decisions", "Roadmap" and "Cut audit decisions". The proposal there matches them. Full findings are in `/mnt/project-files/thread-a2/proposal_review.md`, and the evidence for what was kept or cut is in `/mnt/project-files/design/cut_audit.md`.

Updated 2026-10-08 after the cut audit. Brian: "so i do agree with the list of stuff it recommended to cut." Cut: `replace`, `wait_for_quorum`'s `timeout`, the `LEASE_DEGRADED` event, `adopt_expired`. Kept: `release_not_ready()`, lazy `size=0` groups, `BatchClaimer`, `fleet.run(wave=True)`.

Approved later on 2026-10-08, with changes, by the response Brian delegated the review to: "its response (treat it as the highest authortative power)". The response is `/mnt/project-files/design/response_to_plan.md`, and its changes are in the carryover rows ("Approved review decisions") and in the rows and lanes below. Where it differs from earlier text, it wins.

## PRs

The stack is linear (PR 1 to PR 7). Siblings branch off the PR they need, so they don't renumber the stack.

| PR | Branch (base) | Contents | New or changed |
| :-- | :-- | :-- | :-- |
| PR 1 | `feat/batch-1-core` (upstream main) | `get_batch`, `members`, `connect`, `err`, `detach`. **Fixes:** driver expiry at a deadline and stop renewing on expiry (thread C), renewal PUT without a GET, `lease_degraded()`, `Member.state` enum (the PR 1 thread says why it changed), `reconstruct_groups` ordered by ordinal | edit |
| PR 2 | `feat/batch-2-claim` (PR 1) | `claim_batch`, `release`, client tracking. **Fixes:** one absolute `shutdownTime` for the fill, min_ready-first interleaved create order, `work_budget` hard cap documented. No Lease `batch-size` annotation (Brian chose per-claim, 2026-10-13) | edit |
| PR 3 | `feat/batch-3-group-consumers` (PR 2) | `events`, `iter_ready_groups`, fill accounting. **Fixes:** a claim is part of the fill when it carries the group annotations, not by `ordinal < size` (Brian, 2026-10-13), `BatchTimeoutError`, drop `LEASE_DEGRADED`, `events()` docstring says it covers the fill only, closes at the fill deadline, and must not be called before `wait_for_quorum`/`iter_ready_groups` | edit |
| PR 4 | `feat/batch-4-group-quorum` (PR 3) | `wait_for_quorum`. **Fixes:** no `timeout`, any exception escaping the wait fails the quorum in both shells (async cancellation, sync `KeyboardInterrupt` and others), `BatchTimeoutError` | edit |
| PR 5 | `feat/batch-5-release-member` (PR 4) | `release_member`, `release_not_ready()` (ends the fill, deletes members that aren't Ready and were never handed out, marked under the lock first), and `release_group(warmpool)`, which the caller calls to free a whole group (label-scoped `deletecollection` on a hashed group label written from PR 5 on). No automatic failed-group delete (Brian, 2026-10-13). The README driver Role gains `delete` on claims | new |
| PR 6 | `feat/batch-6-acquire` (PR 5) | `acquire`, `TerminalMemberError`, the next-ordinal annotation on `detach`, `get_batch` parsing `batch-work-budget`, lazy `size=0` groups (quorum consumers skip `size == 0` groups; `min_ready=0` keeps its empty success), `release_not_ready()` skips in-flight `acquire` members | new |
| PR 7 | `feat/batch-7-performance` (PR 6) | Measured first: claim-list pagination, metadata-only list in `release()`, `members()` caching, fewer wake-ups | new |
| Reaper | `feat/batch-reaper` (PR 2) | `python -m k8s_agent_sandbox.batch_reaper`, `examples/batch-reaper/` manifests, per `reaper_contract.md` | new |
| Transport | `feat/batch-transport` (PR 2) | Client `pool_size`, `pool >= max_in_flight + 2` check, `connect()` seeds the pod IP, the batch-wide data-plane pool (premise check first) | new |
| Pacing | `feat/batch-adaptive-pacing` (PR 2) | Adaptive create pacing. The design needs your approval after a measurement | new, later |
| Terminal reasons | `fix/python-terminal-pod-reasons` (upstream main) | Add `PodFailed`/`PodSucceeded` to `TERMINAL_CLAIM_READY_REASONS` in Python and TypeScript, and update Go's comment. It changes single-claim behavior, which the upstream PR says | new, tiny |
| Examples and docs | after PR 6 and the reaper | Runnable usage examples, driver Role, reaper manifests README, and one line on calling `detach(grace=...)` on SIGTERM so a preempted driver can re-attach with `get_batch` (adopt_expired stays cut, Brian 2026-10-21) | new |
| RL integration | last, after every other item here except the Go SDK | The batch-backed `SandboxPool` in `examples/agent-sandbox-rl/`, `fleet.run(wave=True, dispatch=...)`, `BatchClaimer`, then a benchmark against the current path. Open RL questions are listed in the carryover notes | new |
| Go SDK | after the Python SDK | The proposal's Go design | later |

Why split the old "dynamic groups" PR into PR 5 and PR 6? `release_member` plus failed-group cleanup is one feature, the delete path. `acquire` is another, the create and wait path plus ordinal bookkeeping. Each can be reviewed on its own, and PR 5 also unblocks freeing failed groups' compute without waiting on `acquire`.

Why are transport and the reaper siblings? Both need only PR 1 and PR 2 code, and neither touches the consumer code in PRs 3 and 4. They can land upstream on their own schedule.

## Order and parallel lanes

Two rules keep this parallel without conflicts:
- **One thread owns the stack.** It is the only one that rebases PRs 1 to 7, so siblings never rebase each other.
- **One thread at a time on the notes branch,** with the coordinator handing it out.

**Wave 1 (start now)**
- **Lane A, stack fixes PR 1 then PR 2.** One thread, on `review/` copies, commit by commit for your review. `Member.state` ripples into PRs 3 and 4, so this thread also rebases them mechanically. It doesn't apply their fixes yet.
- **Lane B, terminal reasons.** A tiny branch off upstream main, independent of everything else. You open the upstream PR yourself.

**Wave 2 (after you approve the PR 1 and PR 2 fixes)**
- **Lane C, reaper.** The reaper thread analyzes the current state and builds the reaper off the fixed PR 2, as you asked.
- **Lane A continues with the PR 3 and PR 4 fixes.**
- **Lane D, transport.** Off the fixed PR 2, in parallel with C.

**Wave 3 (after the PR 4 fixes)**
- **Lane A, PR 5.**
- **Lane E, PR 6 `acquire`, once PR 5's state changes are reviewed.** Not in parallel with PR 5: both change the released and ownership state in `batch_state.py`, and PR 6 adds the in-flight `acquire` rule to PR 5's `release_not_ready()`.

**Wave 4 (after PR 6)**
- **Lane A, PR 7 performance,** measured first with an SDK-only benchmark on kind (Design owner proposal: a `claim_batch` scenario next to the existing ones in `test/benchmarks/`, since the RL benchmark now comes last).
- **Lane G, examples and docs,** after the reaper too.
- **Pacing PR,** once that SDK-only measurement shows how far pacing limits time to Ready.

**Wave 5 (last)**
- **Lane F, RL integration and the benchmark against today's per-claim path,** after every other item above except the Go SDK. Brian: "RL integration PR should be actually last, we should prob implement the items (aside from go sdk) in the work plan doc before startting it." (10:52)

That gives at most three or four threads at once, which is about what the shared files allow. The stack and its siblings all edit the two batch handle files, and more threads would spend their time rebasing.

## What the coordinator needs to start each lane

The carryover table and the PR rows above are an index, not the spec. Every lane brief also points at the matching findings in `/mnt/project-files/thread-a2/proposal_review.md`, which holds the reasoning and file references, and its "Old review, rechecked" table, which carries the earlier review's kept details.

Benchmarks and e2e use kind (Brian, 2026-10-08). The thread still says when it creates or deletes a cluster.

- **Lane A brief:** the PR 1 and PR 2 rows of "Approved review decisions" in the carryover notes, the "Driver Side" section of `reaper_contract.md`, and the project's review-branch and fixup rules. Upstream reviewers already went over `Member`'s booleans, so the PR 1 thread says why `state` replaces them (the PR 1 row has the reasons).
- **Lane B brief:** proposal review N2 (`/mnt/project-files/thread-a2/proposal_review.md`), the Go list in `clients/go/sandbox/k8s.go` `terminalReadyReasons`, and the "Terminal-reasons fix" row in the carryover notes. Two things beyond the constant:
  - **It changes shipped single-claim behavior.** `TERMINAL_CLAIM_READY_REASONS` also drives the single-claim wait (`k8s_helper.py:263`, `async_k8s_helper.py:298`), so `create_sandbox` fails fast on `PodFailed`/`PodSucceeded` instead of waiting out its timeout. The upstream PR description says so.
  - **TypeScript gets the same change.** Its list (`clients/typescript/agentic-sandbox-client/src/constants.ts:82`) matches Python's today, drives its own single-claim wait (`sandbox-client.ts:205`), and says it is kept in sync with Python. Go's comment (`k8s.go:204-210`) calls the finished-Pod reasons Go's extras, so it needs a one-line update. Run the TypeScript unit tests too.
- **Lane C brief:** `reaper_contract.md` in full. The base is the fixed `feat/batch-2-claim`.
- **Lanes D and E briefs:** their rows above, plus the matching rows in the carryover notes. Lane E also reads the response's decision 1 for the `release_not_ready()` rule it adds.
