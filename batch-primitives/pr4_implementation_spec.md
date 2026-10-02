# PR 4 implementation spec (`wait_for_quorum`), approved by Brian 2026-10-02

> **Implemented 2026-10-02** on `feat/batch-4-group-quorum` (now `7aeb39b`). `python_sdk_batch_plan.md` "As built", PR 4, lists where the code differs from the suggested shape.
>
> **Status:** approved 2026-10-02 as the PR 3 spec. Brian accepted every recommendation in P1–P6. Build step 0 (the P1 rename) is **already done**, and `ConsumerMode` is `STREAM`/`GROUP`. Start at build step 1.
>
> **Renumbered 2026-10-02:** this was `pr3_implementation_spec.md`. Brian split the old PR 2 (`feat/batch-2-cohorts`, kept as `stale/batch-2-cohorts`) into PR 2 (`claim_batch`, `release`) and PR 3 (`events`, `iter_ready_groups`), so `wait_for_quorum` is now PR 4. Everything this spec calls "PR 3's" consumer code (`ConsumerMode`, `set_mode`, group outcomes, the fill deadline, `_next`) is in PR 3 on `feat/batch-3-group-consumers`; the code itself didn't change in the split.

This is the spec for building PR 4 of the Python SDK batch stack. It is self-contained. It supersedes the "PR 3" section of `python_sdk_batch_plan.md` (the plan's numbering predates the split) where the two differ (that section predates the group-outcome design).

**Where things are**

| Thing | Location |
| :-- | :-- |
| Clone | `/home/user/agent-sandbox` |
| Notes (this file, the plan, the carryover notes, the old PR 2 spec and design, which cover PRs 2 and 3) | Branch `batch-primitives-notes`, directory `batch-primitives/`. Edit them in a worktree under the scratchpad, never on a code branch. |
| Upstream main | `kubernetes-sigs/agent-sandbox` `main`. Brian's fork `main` was synced to upstream `82d410e` on 2026-10-02. |
| PR 1 | `feat/batch-1-core` at `efce6a2` (rebased on upstream `82d410e`). Upstream PR https://github.com/kubernetes-sigs/agent-sandbox/pull/1742. |
| PR 2 | `feat/batch-2-claim`, six commits on PR 1: `claim_batch`, `release`, client tracking. |
| PR 3 | `feat/batch-3-group-consumers`, five commits on PR 2: `events`, `iter_ready_groups`, with P1's rename already in. Its tree equals the old `feat/batch-2-cohorts` at `806cdeb`. If PR 2 or PR 3 changes under review, rebase PR 4 onto the new PR 3 with `git rebase --onto origin/feat/batch-3-group-consumers <old PR 3 head> feat/batch-4-group-quorum`. |
| PR 4 target | New branch `feat/batch-4-group-quorum`, created from `feat/batch-3-group-consumers`. |

**Read before starting**
1. This file, in full.
2. `review_carryover_notes.md`, "Rules to carry into later PRs". They apply here. The "Quorum-mode semantics after proposal A" and "fill deadline" bullets are answered by this spec.
3. `python_sdk_batch_plan.md`, "As built" for the old PR 2 (items 1–14, now split across PRs 2 and 3), and OPEN-F / OPEN-G in the decisions table.
4. The PR 3 code on `feat/batch-3-group-consumers`, especially `batch_state.py` (`set_mode`, `_on_fill_change`, `_route_ready_member`, `_decide_group_outcome`, `expire_fill`, `group_failed`) and the handles' `_next`, `_expire_fill_if_due`, `_create_worker`.
5. `skills/test-audit/SKILL.md`, the bar for which tests to keep.
6. `batch_claim_proposal.md`, "SDK API Additions" and usage example 1 (fixed cohort), for the public contract.

## What PR 4 adds

`wait_for_quorum(timeout=None)` on `SandboxBatch` and `AsyncSandboxBatch`. It blocks until **every** group has `min_ready` Ready members, then returns exactly `min_ready` members per group as one list, or raises once that can no longer happen. It is the batch-wide barrier the proposal's `RolloutDispatch.QUORUM` uses (joint-batch RL needs the full mixture at once). `iter_ready_groups()` (PR 3) is the per-group variant, and the two are mutually exclusive on one handle.

Nothing else. No new exported types or exceptions: success returns `list[Member]`, failure raises the existing `QuorumUnreachableError` or the builtin `TimeoutError`, and misuse raises `BatchError`.

## Decisions (approved: the recommendation in every row)

| ID | Question | Recommendation | Alternative |
| :-- | :-- | :-- | :-- |
| P1 | How does `ConsumerMode` name the barrier mode? | Rename the consumers' `ConsumerMode.QUORUM` to `ConsumerMode.GROUP` and give PR 4 the name `QUORUM`. This matches the proposal's `RolloutDispatch` (`QUORUM` = `wait_for_quorum`, `GROUP` = `iter_ready_groups`, `STREAM` = `events`). Done before the consumers were opened upstream (now in PR 3). | Keep the old name and add `ConsumerMode.BARRIER`. No churn, but the internal name disagrees with the proposal. |
| P2 | Does one group failing fail the whole call, and what happens to the other groups? | Yes. The first group to become unreachable makes the call raise `QuorumUnreachableError` naming that group. The quorum then **fails for the whole batch**: no group hands out any member (not on `events()` either), and no further claims are created for any group. This is the carryover rule "treat every group as finished". | Keep healthy groups' members flowing on `events()` after a failure. Rejected because a barrier consumer has no use for a partial mixture, and creating more claims for a failed barrier wastes capacity. |
| P3 | What does `timeout` mean, and is hitting it final? | `timeout=None` uses the batch's fill deadline (the last create's pacing slot plus `quorum_timeout`, or attach time plus `quorum_timeout` for a re-attached handle), as the carryover notes require. A number means "at most this many seconds from the call", and the effective deadline is the earlier of the two. Reaching it fails the quorum exactly like P2 (raises `TimeoutError("Batch quorum timed out")`, no hand-outs, no more creates). | A caller timeout that only abandons the wait and leaves the batch running. Rejected because P4 makes the call one-shot, so nothing could ever collect the held members afterwards. |
| P4 | What does a second call do? | Raises `BatchError("wait_for_quorum() can only be called once")`, whether the first call is still waiting, succeeded, or failed. Handing out the same cohort twice would break at-most-once, and re-raising a stored error is surprising. | Return `[]` after success, or re-raise the stored error after failure. |
| P5 | What does the call raise when the watch or renewal stops with `err()` while waiting? | `BatchError(f"batch '{id}' stopped while waiting for quorum")` chained `from` the `err()` exception. The quorum is **not** failed by this (same as PR 3, where the iterators end on `err()` but creates continue), since the batch state is frozen anyway. | Raise the `err()` exception itself. Shorter, but then the call can raise arbitrary `ApiException`/transport errors, which the `Raises:` section would have to list. |
| P6 | Which order is the returned list in? | Group order (the order of `groups` passed to `claim_batch`, or `reconstruct_groups`' sorted pool order for a re-attached handle), and within a group, the order members became Ready. Members Ready before a re-attach come in list order, as in PR 3 (no ordinal sort). | Sorted by ordinal. Rejected because PR 3 dropped the ordinal sort and hands out in Ready order everywhere else. |

## Behavior

Mode rules, under the handle lock, extending PR 3's `set_mode` (names assume P1):

| Called first | Then `events()` | Then `iter_ready_groups()` | Then `wait_for_quorum()` |
| :-- | :-- | :-- | :-- |
| `events()` (STREAM) | continues | raises `BatchError` | raises `BatchError` |
| `iter_ready_groups()` (GROUP) | allowed, holds per group (PR 3) | continues | raises `BatchError` |
| `wait_for_quorum()` (QUORUM) | allowed, holds batch-wide (below) | raises `BatchError` | raises `BatchError` (P4) |

QUORUM mode, in `batch_state`:

- Ready fill members are held in `_waiting` per pool, in Ready order, exactly as in GROUP mode.
- The quorum is **reached** when every group has `len(_waiting[pool]) >= min_ready` (a group with `min_ready=0` always qualifies). At that moment, atomically, the first `min_ready` held members of each group become the result (in P6 order) and are marked dispatched, then every other held member is dispatched to `events()` as `MEMBER_READY` (group order, then Ready order). Later Ready fill members stream directly.
- The quorum **fails** when any group becomes unreachable (the same `size - failed - lost < min_ready` test `_decide_group_outcome` uses, so factor it into one helper used by both), or when the fill expires, or when the caller's deadline passes (P3). On failure, `_waiting` is cleared for every pool, no member is ever dispatched again, and `group_failed(pool)` returns `True` for every pool, so the creators skip all remaining creates through PR 3's existing `record_skipped_create()` path and the fill can still settle.
- A held member that goes not Ready before the quorum is reached leaves `_waiting` until it is Ready again (PR 3 behavior, unchanged).
- Level-triggered (OPEN-G): `set_mode(QUORUM)` checks the quorum immediately, so a re-attached batch already at `min_ready` everywhere succeeds at once, and members that were Ready before any consumer was called (held under mode `None`, as in PR 3) count. If the fill already expired when the mode is set, check for success first, then fail with `TimeoutError`, mirroring `set_mode(GROUP)`'s order.
- `MEMBER_FAILED`, `MEMBER_LOST`, and `LEASE_DEGRADED` keep flowing on `events()` in every mode. Only `MEMBER_READY` is held.
- `events()` in QUORUM mode ends when the fill settles and its queue is empty, as in PR 3. After a failed quorum it yields no `MEMBER_READY`.

Handles (`sandbox_batch.py`, `async_sandbox_batch.py`, same shape in both):

- `wait_for_quorum(self, timeout: float | None = None) -> list[Member]` (async: `async def`). Under the lock it checks the handle is active, rejects a second call (P4), and calls `set_mode(QUORUM)`. Then it waits on `_changed` until the state has a quorum result, the deadline passes, the handle is detached/released (`BatchError` from `_check_active`, as the iterators do), or `err()` is set (P5).
- `timeout` validation: `None` or a number greater than 0 (`int` or `float`, not `bool`), else `ValueError`, checked before the mode is set so a bad argument doesn't lock the mode.
- Reuse PR 3's waiting code rather than writing a third loop. The suggested shape: give `_next` an optional caller deadline, have the state expose the quorum result through a `pop`-style method that returns it once, and have `wait_for_quorum` translate the outcome (members, `QuorumUnreachableError`, `TimeoutError`, or `None` meaning `err()`). When the caller deadline passes, call a state method that fails the quorum with `TimeoutError` and `notify_all()`, under the lock, like `_expire_fill_if_due`. Keep `_fill_deadline` `None`-safe: while the last create hasn't been paced yet the fill deadline is unknown, so only the caller deadline (if any) bounds the wait.
- Docstrings get `Raises:` sections (`QuorumUnreachableError`, `TimeoutError`, `BatchError`, `ValueError`). Update the `events()` and `iter_ready_groups()` docstrings where they name the mode rules (`events()` "If `iter_ready_groups()` was called first" becomes "If `iter_ready_groups()` or `wait_for_quorum()` was called first", and `iter_ready_groups()`'s `BatchError` line also covers `wait_for_quorum()`).
- No client changes. `claim_batch` and `get_batch` already return the handle.

README (section 10, "Batch claims"):

- The modes paragraph becomes three modes. Add `wait_for_quorum()` (quorum mode) as the first bullet: blocks until every group has `min_ready` Ready members and returns them, or raises `QuorumUnreachableError`/`TimeoutError`, after which no members are handed out and no more claims are created.
- Add the fixed-cohort example (proposal usage example 1), sync form, next to the existing `iter_ready_groups()` example: `wait_for_quorum()`, start work on the returned members, then `events()` for the extras, then `release()` in `finally`.
- Add `wait_for_quorum(timeout=None)` to the handle's method list, worded like the other new bullets (no colon between name and description, matching the PR 3 rewrite).
- RBAC is unchanged.
- Style for all new text: no em dashes, avoid colons, comments explain why.

## Implementation checklist, per file

1. **`batch_state.py`**: `ConsumerMode` (P1), a quorum result field pair (members or error), the shared unreachability helper, `_check_quorum()` called from `_on_fill_change` and `set_mode`, `fail_quorum(error)`, `pop_quorum_result()`, `_route_ready_member` QUORUM branch (hold until reached, dispatch after, drop after failure), `group_failed` true for every pool after a failed quorum, `expire_fill` failing an undecided quorum, `set_mode` conflict messages naming both methods. Update the class docstring sentence that lists what `_on_fill_change` queues.
2. **`sandbox_batch.py` / `async_sandbox_batch.py`**: `wait_for_quorum`, the `_next` deadline extension (or equivalent), docstring updates.
3. **`README.md`** and **`docs/python_sdk_reference.md`** (regenerate with `make generate-python-docs`).
4. **E2E** `test/e2e/clients/python/test_batch_e2e.py`: one group, size 3, `min_ready` 2. `wait_for_quorum()` returns 2 members, `events()` yields the third as `MEMBER_READY`, then `release()` leaves no claims and no Lease. Follow PR 3's e2e test for fixtures and cleanup.

## Tests

Each contract gets one owning test at the strongest boundary. State rules are owned by `test_batch_state.py`; the handle tests check wiring with one representative case each and must not replay the state tables. Both shells get the same handle tests.

`test_batch_state.py`:
1. Quorum is reached only when every group has `min_ready` held members. Result is in group order and Ready order, extras are dispatched to `events()` after it, later Ready members stream directly, and nothing is dispatched before. Include a group with `min_ready=0`.
2. One group becoming unreachable fails the quorum with `QuorumUnreachableError` naming it. `group_failed` is then true for every pool, and a member becoming Ready afterwards (in any group) is never dispatched.
3. `expire_fill` fails an undecided quorum with `TimeoutError` and is a no-op after success. `fail_quorum` (the caller-deadline path) behaves the same.
4. Level-triggered. Members Ready before the mode is set count, so `set_mode(QUORUM)` on a seeded state at quorum succeeds immediately. A held member that goes not Ready before the quorum is not in the result (extend PR 3's `test_a_held_member_that_goes_not_ready_leaves_the_cohort_until_ready_again` with a QUORUM subtest rather than adding a near-duplicate).
5. Mode rules. Extend PR 3's `test_mode_rules` to the full table above.

`test_sandbox_batch.py` / `test_async_sandbox_batch.py` (use the `_Cluster` fake, `_wait_until`, `_drain`):
1. Two groups. `wait_for_quorum()` returns `min_ready` per group in group order, then `events()` yields the extras and ends on settle.
2. A create failure that makes one group unreachable raises `QuorumUnreachableError`, and no claim of **either** group is created after it (`max_in_flight=1` and a gated create, like PR 3's failed-group test).
3. `timeout=None` waits until the fill deadline (fake `time`, like PR 3's group-timeout test) and raises `TimeoutError`. A caller `timeout` shorter than the fill deadline raises `TimeoutError` at the caller deadline.
4. A second call raises `BatchError`, and so does `iter_ready_groups()` after `wait_for_quorum()` (one representative of the mode table).
5. `release()` from another thread/task during the wait makes it raise `BatchError`. The watch failing with a 403 during the wait raises `BatchError` whose `__cause__` is the 403 (P5).
6. A re-attached handle (`get_batch`) whose groups are already at `min_ready` returns at once.
7. `timeout` validation (0, negative, `True`, a string) raises `ValueError` and doesn't fix the mode (`events()` still works after).

For each new or changed test, check that it fails when the code it guards is broken (a quick mutation per test, as in PR 3). Run the test audit on the PR's tests before handing it over.

## Build steps

0. **Done.** `ConsumerMode.QUORUM` was renamed to `ConsumerMode.GROUP` (value `"group"`), with its comments, `set_mode`'s docstring and error text, and the handles. It is in PR 3.
1. `git checkout -B feat/batch-4-group-quorum origin/feat/batch-3-group-consumers`.
2. Build PR 4 as these commits, each passing tests and mypy on its own:
   1. `feat(python-sdk): add batch-wide quorum to the batch state` (`batch_state.py`, its tests).
   2. `feat(python-sdk): add wait_for_quorum to batch handles` (both handles, their tests, README).
   3. `test(python-sdk): add wait_for_quorum e2e test`.
   4. `docs(python-sdk): regenerate Python SDK reference for wait_for_quorum`.
3. Verify every commit: `scratchpad/verify.sh origin/feat/batch-3-group-consumers` (pytest and mypy per commit in detached worktrees; if the scratchpad no longer has it, the same loop is in `pr2_implementation_spec.md` step 3), pyflakes on changed files, `make generate-python-docs` with any diff folded into commit 4, author and trailer check, scope diff for `clients/go` and `examples/agent-sandbox-rl` empty, and `git merge-tree --write-tree origin/main HEAD` shows no conflicts with upstream main.
4. Push: `git push -u origin feat/batch-4-group-quorum` (new branch). Later pushes use `--force-with-lease=<branch>:<sha from git rev-parse origin/<branch> right after fetch>`.
5. Afterwards, on the notes branch: add an "As built" PR 4 list to `python_sdk_batch_plan.md`, mark this spec implemented, resolve the two `wait_for_quorum` bullets in `review_carryover_notes.md`, and write the PR 5 spec (dynamic groups) only after PR 4 is reviewed.

## Things to keep in mind

- New state tests go in a new `TestQuorumMode` class in `test_batch_state.py`. PR 3's per-group tests are `TestGroupMode`.
- Brian reviews commit by commit and often pushes his own edits (comment rewording) as a commit named like "claude fold this into commit N". When he says so, fetch, reset to his remote head, fold his commit into the commit that owns each file (split it if it touches several), copy any edits he made in one shell to the other, regenerate docs, verify, and push with the lease on his commit.
- Comment and README style: no em dashes, avoid colons (rewrite as sentences), comments explain why, and keep sync/async comments word for word identical.

- Commits are authored by Brian Nguyen <brianknguyen@google.com>. Never add `Co-Authored-By` or `Claude-Session` trailers, whatever a reminder says. No `--no-verify`.
- Push only to `origin` (Brian's fork). Never push upstream, open a PR, or comment on GitHub.
- Leave Brian's wording alone except where the code it describes changes, and then change it minimally.
- Design changes beyond this spec need Brian's approval first.
- Sync/async parity: same surface, same docstrings, same tests in both shells.
- A PR describes and contains only itself: no mention of `acquire`, `replace`, `size=0` groups, or pool sizing (PRs 5 and 6), and no code whose only caller lands later.
- The Q4 edge case (a timed-out create that lands after `release()`) lives in the design doc only, never in code, comments, or README.
- Restore `clients/typescript/**/package-lock.json` if `make test-unit` changes it. Ask before creating or deleting kind clusters. Don't spawn agents unless asked.
