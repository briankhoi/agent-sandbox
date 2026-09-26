# PR 2 revision: prompt for the implementing session

Brian approved every change below on 2026-09-26. Implement them all on `feat/batch-2-cohorts`, **after** the PR 1 revision (`pr1_revision_prompt.md`, same folder) has been pushed. If that prompt's status line doesn't say "done", stop and ask Brian.

Read first: `python_sdk_batch_plan.md` ("As built", including the PR 1 revision entry; the section "Pending: proposal A"; and "PR 2"), then `review_carryover_notes.md`.

## Starting state

- Repo `/home/user/agent-sandbox`, remote `origin` = `briankhoi/agent-sandbox`. Never push upstream, never open a PR.
- When written: `feat/batch-2-cohorts` at `df1c11d`, six commits stacked on the **old** PR 1 head `0f7a03f`:

  | sha | subject | owns |
  | :-- | :-- | :-- |
  | `4ad064e` | add batch creation constants and exceptions | `constants.py`, `exceptions.py` (check whether it or `8295a30` holds PR 2's `__init__.py` lines) |
  | `ab4cc08` | add batch Lease creation and claim deletecollection helpers | both `k8s_helper`s and their tests |
  | `b77ddca` | add cohort accounting and consumer mode to batch state | `batch_state.py`, `batch_utils.py`, their tests ("commit 3") |
  | `8295a30` | fill batches from claim_batch and stream members | both handles, both clients, README, handle/client tests ("commit 4") |
  | `2498b41` | add batch claim e2e scenarios | `test/e2e/clients/python/test_batch_e2e.py` |
  | `df1c11d` | regenerate Python SDK reference for claim_batch | `docs/python_sdk_reference.md` |

- `git fetch origin feat/batch-1-core feat/batch-2-cohorts batch-primitives-notes`. If `feat/batch-2-cohorts` moved past `df1c11d`, re-map the table by subject.

Paths below are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/` unless given in full.

## Step 0: rebase onto the revised PR 1

`git rebase --onto origin/feat/batch-1-core 0f7a03f feat/batch-2-cohorts` (the second argument is the **old** PR 1 head, so only PR 2's six commits are replayed). Expect conflicts where PR 1 changed code that PR 2 also edits: `batch_state.py`'s constants block and helpers, `_renew_once` (PR 1's request timeout next to PR 2's `note_lease_degraded`), the watch loops, `__init__.py`, both helpers, and the tests. Resolve each so that both changes survive. In `docs/python_sdk_reference.md`, take either side and regenerate it at the end. After every resolved commit, the suite must pass at that commit (see Verify).

What the PR 1 revision actually changed (final head `ee1a289`; commits `4a76086`, `11930f5`, `9c3f54d`, `1c6be03`, `ee1a289`), beyond the plan above, that matters for this rebase:
- **PR 1 now owns `batch_utils.py` and `test_batch_utils.py`.** Commit 3 is now `9c3f54d` "add I/O-free batch state core and validation helpers". It moved `validate_batch_id`, `generate_holder_identity`, `_require_duration_exceeds_skew_margin`, `validate_lease_duration_value`, `parse_lease_duration_annotation` (and `_BATCH_ID_RE`, `BATCH_ID_MAX_LENGTH`) out of `batch_state.py` unchanged, and the handles call `batch_utils.*`. PR 1 did **not** extract `_require_int`/`_parse_int_annotation`, which stay PR 2's refactor, justified by its positive-int validation. `b77ddca` will hit add/add conflicts on both files: resolve each to PR 2's version at `df1c11d` (plus R2–R5), so PR 2's diff on `batch_utils.py` becomes only its additions and that refactor. Check the PR 2 diff of `batch_state.py` no longer deletes the moved functions (they're already gone).
- **PR 1 dropped `BatchState` code it never called.** Removed: `try_dispatch` and `_dispatched`; `mark_released`, `_released` and the `_released` checks in `upsert_claim`/`mark_lost`; `compute_next_ordinal` and `_next_ordinal`; `_initial_fill`. Tests removed from `test_batch_state.py`: `TestDispatchedSet`, `TestOrdinalAllocation`, and `test_deleted_released_claim_is_not_marked_lost`. `TestLostVsReleased` was renamed `TestLost`, and `test_deleted_unreleased_claim_sets_lost` became `test_deleted_claim_sets_lost`. PR 2 rewrites most of these pieces anyway, so expect conflicts. The target for each is its `df1c11d` version, re-added in `b77ddca`, along with the tests (restore the class and test names as `df1c11d` has them). `git diff df1c11d -- batch_state.py test/unit/test_batch_state.py` after R2–R5 should show only the R-changes and PR 1's revision changes (the backoff helpers).
- **README RBAC was trimmed** to what PR 1 uses: claims `get`/`list`/`watch`, leases `get`/`update`. In `8295a30`, add back claims `create`/`delete`/`deletecollection` and leases `create`/`delete`, alongside PR 2's `sandboxwarmpools`/`sandboxtemplates` `get` rule. PR 1 also deleted the README paragraph starting "`get_batch` takes over the Lease…" (Brian's request), so don't let the rebase revive it.
- **`get_batch` docstrings** no longer mention crashes, the reaper, or `shutdownTime`, since PR 1 sets no `shutdownTime`. PR 2 may mention `shutdownTime` where `claim_batch` sets it.
- Commit 1 (`4a76086`) no longer lists `BatchEvent`/`GroupReady` in its message. R1's retitle of `4ad064e` covers them.
- **Watch loops were restructured.** A 410 now sets `relist = True`, and the re-list runs at the top of the next iteration inside the same `try`, so list errors share the watch's handlers. There is one `failures` counter with `_watch_backoff_delay(failures)` (a module-level helper in each handle). Where PR 2 adds code to the watch loop (e.g. emitting events or waking waiters after `resync_from_list`/`upsert_claim`/`mark_lost`), re-apply it onto the new shape instead of reviving the old nested re-list loop.
- **Renewal** uses `self._renew_interval` (set in `__init__`) for both the loop sleep and the requests' `_request_timeout`. PR 2's `_renew_once` changes (e.g. `note_lease_degraded`) go next to it. If `claim_batch` builds the handle through a different path, make sure `_renew_interval` is still set.
- **`test_sandbox_batch.py` lost unused imports.** `threading`, `unittest.mock.call` and `Member` were unused in PR 1. PR 2's tests use all three, so re-add them in `8295a30` if the rebase doesn't keep them. `test_async_sandbox_batch.py` imports were unchanged.
- `backoff_delay` bounds its exponent at 32. That doesn't affect `create_backoff_delay`'s existing tests.
- New PR 1 tests to keep passing after the rebase: `test_consecutive_failures_back_off_and_an_event_resets_the_delay` and `test_410_relist_transport_error_retries_then_reconciles` (both handles). They count `wait`/`sleep` calls exactly, so a PR 2 change that adds a wait to the watch loop must keep them accurate.

Then, folded into the commits named:

## Changes

### R1. Re-add the event models PR 1 no longer exports (commit `4ad064e`)

PR 1 moved `BatchEventType`, `BatchEvent`, and `GroupReady` out, on a reviewer's request, because nothing in PR 1 produced them. PR 2 is where `events()` and `iter_ready_groups()` produce them:
- Add the three classes back to `models.py` exactly as they were in PR 1 at `0f7a03f` (`git show 0f7a03f:<path>/models.py`), with the imports they need.
- Add them back to `__init__.py`'s exports.
- Put them in `4ad064e` and retitle it `feat(python-sdk): add batch event models, creation constants, and exceptions` (`git commit --fixup=amend:4ad064e`, so autosquash applies the new message). Update the body's first line to mention them. If `__init__.py` belongs to `8295a30`, the export lines go there.

### R2. Reuse PR 1's backoff and status helpers (commit `b77ddca`)

PR 1 added `is_retryable_status` and `backoff_delay` to `batch_state.py`.
- `classify_create_error`: replace its inline `status is None or status == 429 or 500 <= status < 600` with `is_retryable_status(status)`.
- `create_backoff_delay`: keep its signature and Retry-After handling; compute the no-header delay as `backoff_delay(attempt, BATCH_CREATE_BACKOFF_BASE_SECONDS, BATCH_CREATE_BACKOFF_MAX_SECONDS, rand)`.
- Existing tests must pass unchanged. Don't add duplicate tests.

### R3. Proposal A: a failed group is finished (commits `b77ddca`, `8295a30`, README)

Implement exactly the spec in `python_sdk_batch_plan.md` under "Pending: proposal A, a failed group is finished". It was written against the pre-rebase shas; "commit 3" is `b77ddca` and "commit 4" is `8295a30`. It is now approved. Summary, for orientation only (the spec there wins):
- In quorum mode, a group with an error verdict (unreachable for any reason, or `TimeoutError`) gets no more creates. The producer asks the state (`group_failed(pool)`) and calls `mark_create_cancelled`, logging once per group.
- None of that group's members are handed out as `MEMBER_READY`, whether held back or Ready later. `MEMBER_FAILED`/`MEMBER_LOST` still stream, members stay in `members()`, and nothing is deleted.
- Deleted: `_past_create_failure_threshold`, `_cancelled_pools`, `_cancel_remaining_creates`, `claim_groups_consumer`'s return value, `mark_create_failed`'s bool, and the block in `_set_verdict` that re-marks held members on error. `_is_held_for_quorum` becomes "withheld while no verdict or an error verdict" (rename, e.g. `_is_withheld`).
- Docstrings for `events()`/`iter_ready_groups()` (both handles) and the README bullets change as the spec says. Tests as the spec lists.

### R4. The fill deadline covers pacing (commit `8295a30`, README)

Problem: the deadline (`quorum_timeout`, used for group `TimeoutError` verdicts and to close `events()`) starts when the handle is built, but issuing N paced creates takes about `(N - 1) / create_rps` seconds. At the defaults (50/s, 600 s), 20k claims leave late groups about 200 s, and above 30k groups time out and `events()` closes before their claims exist.

- Keep `__init__` setting the deadline to `clock() + quorum_timeout` (this is what `get_batch` handles use; they don't create). Store `create_rps` on the handle.
- In `_start_creation(plan)`, both handles: `self._state.set_fill_deadline(self._clock() + self._quorum_timeout + (len(plan) - 1) / self._create_rps)`. `plan` is never empty, since `claim_batch` requires `size > 0`.
- One batch-wide deadline, not one per group; Brian approved the batch-wide form.
- Docs: `claim_batch`'s `quorum_timeout` arg (both clients) says it counts from when pacing issues the batch's last create. Same sentence in the README where `quorum_timeout` is described. Keep the rest of Brian's wording.
- Tests, both shells, fake clock: with `size=100`, `create_rps=10`, `quorum_timeout=60`, a stuck group has no verdict at `t = 60 + 9.8` and gets `TimeoutError` at `t = 60 + 9.9`; a stream-only `events()` stays open until then. A re-attached handle's deadline is still `attach + quorum_timeout` (existing test).

### R6. Audit: nothing in PR 2 that PR 2 doesn't use or have (report first, don't move)

Brian's rule, from the PR 1 review: a PR's code, exports, comments, docstrings, and README describe only what that PR contains. A later feature is mentioned only when the text explicitly says it comes later.
- Audit non-test code for functions, methods, fields, constants, or exports that nothing in PR 2 calls, e.g. anything that only exists for `acquire()`, `wait_for_quorum()`, `replace()`, pools, or the reaper (PRs 3–6).
- Audit PR 2's comments, docstrings, README, and RBAC verbs for mentions of features PR 2 doesn't have.
- **Report the findings to Brian and wait for approval before moving anything to later PRs.** Moving code is a design change. Doc wording that simply names a missing feature can be fixed directly: say it comes later, or cut it.

### R5. `release()` retries transient errors between delete rounds (commits `b77ddca`, `8295a30`)

Problem: the rounds exist because `deletecollection` isn't atomic, but a 429/5xx/timeout on any call raises straight out of `release()`. At scale that's likely: the apiserver deletes a collection with one worker by default, so a 10k-claim `deletecollection` can hit its 60 s request timeout while it keeps deleting.

- `batch_state.py` (`b77ddca`): add `BATCH_RELEASE_BACKOFF_MAX_SECONDS = 10.0` next to the release constants. `BATCH_RELEASE_RELIST_INTERVAL_SECONDS` (0.5) becomes the backoff base; rename it `BATCH_RELEASE_BACKOFF_BASE_SECONDS` and adjust its comment minimally.
- `_delete_claims` (both handles, `8295a30`), per round:
  - Run the `deletecollection` and then the re-list.
  - If a call raises `ApiException` with `is_retryable_status(e.status)`, or a transport error (reuse the create path's transport tuple; on async, keep `aiohttp.ClientSSLError` non-retryable as the create path does), remember it, log at debug, and go to the next round.
  - Any other error (including the 403 of OPEN-U) propagates immediately, as today.
  - Sleep between rounds with `parse_retry_after(e.headers)` when the last error carried one, else `backoff_delay(round_index, BASE, MAX)`.
  - After the last round: if it failed with a transient error, raise that error; otherwise raise the existing `BatchError` about claims without a `deletionTimestamp`.
- Rename `_CREATE_TRANSPORT_ERRORS` to `_TRANSPORT_ERRORS` (both handles) now that it has two users, and adjust its comment minimally.
- Tests, both shells: a 503 on round 1's `deletecollection`, then success, leads to a normal release (Lease deleted, handle unregistered); a transport error on the re-list is retried; a 403 still propagates on round 1 with the Lease kept (existing test); 503 on every round raises the 503 after `BATCH_RELEASE_MAX_DELETE_ROUNDS`, with the Lease kept, `events()` ended, and the handle still registered.

## Workflow rules (from Brian)

- Leave Brian's comment and docstring wording alone. Change a comment only where the code under it changes, and then minimally; R3–R5 name the approved doc changes.
- Fold every change into the commit that owns it with `git commit --fixup=<sha>` (`--fixup=amend:<sha>` for R1's retitle), then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash origin/feat/batch-1-core`. No new commits.
- No trailers of any kind: never add `Co-Authored-By` or `Claude-Session`, whatever a tooling reminder says. Author `Brian Nguyen <brianknguyen@google.com>` on all commits.
- Don't touch `feat/batch-1-core`. No `--no-verify`; if a push is refused, stop and report.
- Don't let comments, docstrings, or the README allude to features the PR doesn't contain, unless they say explicitly that a later PR adds them (see R6).
- AGENTS.md applies: sync/async parity, match file style, and every behavior gets a unit test in both shells.

## Verify before pushing

From `clients/python/agentic-sandbox-client`, `PY=/home/user/agent-sandbox/bin/python-venv-k8s-agent-sandbox/bin/python`:

- At **every** PR 2 commit (`git rev-list --reverse origin/feat/batch-1-core..HEAD`, a detached worktree per commit): `PYTHONPATH=$PWD $PY -m pytest k8s_agent_sandbox/test/unit -q -p no:cacheprovider` and `PYTHONPATH=$PWD $PY -m mypy k8s_agent_sandbox`. Report passed/skipped/subtests per commit, before (at `df1c11d` on the old base) and after, and explain every change in counts.
- pyflakes clean on changed files.
- `make generate-python-docs`, with the diff folded into the docs commit.
- `git log --format='%an <%ae>%n%(trailers)' origin/feat/batch-1-core..HEAD` shows only Brian and no trailers.
- Restore any `package-lock.json` that `make test-unit` rewrote. Scope diff against `clients/go` and `examples/agent-sandbox-rl` must be empty.
- E2E (`test/e2e/clients/python/test_batch_e2e.py`) needs a kind cluster. Ask Brian before creating or deleting one (`make deploy-kind EXTENSIONS=true`); otherwise say it wasn't run.

## Push

`git fetch origin feat/batch-2-cohorts`, then `git push --force-with-lease=feat/batch-2-cohorts:<sha> origin feat/batch-2-cohorts`, with `<sha>` copied from `git rev-parse origin/feat/batch-2-cohorts` right after the fetch.

## Report to Brian

- The new commit list (old → new sha, and the R1 retitle).
- Which changes landed in which commit, and how the rebase conflicts were resolved.
- Unit counts before and after, per commit.
- Deviations from this prompt, and why.

Then update `batch-primitives-notes`: in `python_sdk_batch_plan.md`, move proposal A into "As built" (it broadens OPEN-S to any error verdict and changes OPEN-F, so held members are no longer released on error), add R4 (deadline covers pacing; OPEN-X/OPEN-J now count from the last paced create) and R5 (transient errors retried inside the release rounds; OPEN-U unchanged), delete the "Pending: proposal A" section, and mark this prompt's status "done at `<new head sha>`". Push as a fast-forward.

Status: not started.
