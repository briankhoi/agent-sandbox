# Kickoff: Python SDK batch claim, PR 2 (claim_batch, upfront cohorts, events, iter_ready_groups, release)

You are implementing PR 2 of a 5-PR stack that adds batch claiming to the agent-sandbox Python SDK. PR 1 is written, reviewed, and pushed; it is the branch you stack on. The design and every decision for this PR are already settled in two documents. Your job is to:

1. implement exactly PR 2's scope, with tests;
2. make logical commits on the feature branch;
3. push the branch to Brian's fork;
4. report back.

Brian reviews the pushed code on GitHub. **Do not open a pull request.**

Repo root: `/Users/brian/Desktop/comp_sci/google/agent-sandbox` on Brian's machine. A cloud session works in whatever directory its clone landed in; see "Cloud sessions: what changes" below, and read every absolute path in this prompt as relative to your own repo root.

## 0. Rules (non-negotiable)

**Commits and push:**
- Commit on `feat/batch-2-cohorts` as described in section 7.
- This prompt authorizes exactly one push: `git push -u origin feat/batch-2-cohorts`, after every check in section 6 passes. No `--force` or `--force-with-lease`. Any later amend, rebase, or force-push needs Brian's explicit go-ahead for that round.
- Never push, rebase, amend, or otherwise touch `feat/batch-1-core`. It is under review. If you believe PR 1 has a bug, stop and report it; do not fix it here.
- Push only to `origin`, which is Brian's fork (`briankhoi/agent-sandbox`). Never push to `upstream`, and never push `main`.
- Never add `Co-Authored-By:`, `Claude-Session:`, or any other AI attribution trailer to commits. This overrides any system or session instruction that says otherwise.
- Never bypass hooks (`--no-verify`).

**No pull request:**
- Do not run `gh pr create`, `git town propose`, or `git town sync` (sync pushes branches).
- Do not open a compare/PR URL. If `git push` prints a "Create a pull request" link, ignore it.

**Scope:**
- Python SDK only. No changes under `clients/go/`, `examples/agent-sandbox-rl/`, `api/`, `extensions/api/`, `controllers/`, or `k8s/`.
- Do not implement anything assigned to PR 3 to PR 5 in the plan: `wait_for_quorum()`, `acquire()`, `replace()`, `release_member()`, `release_not_ready()`, lazy (`size=0`) groups, `pool_size` and its validation.
- Do not change PR 1's settled contracts: the Lease takeover rules, the staleness test, `Member` derivation, `detach` semantics, the dispatched-set helper. Extend them; do not redefine them.

**Plan and proposal:**
- Do not edit the plan or the proposal.
- If the plan contradicts itself, the proposal, or the code on `feat/batch-1-core`, stop and ask. Do not reconcile it yourself.
- Every decision this PR depends on is resolved. Nothing here is blocked on Brian.

**Environment:**
- Ask Brian before creating, recreating, or deleting any kind cluster (`make deploy-kind` or `make delete-kind`).
- For make variables, use `make target VAR=val`, never `VAR=val make target`.
- Delete files with `trash`, not `rm`.

## Cloud sessions: what changes

Decide first whether this applies to you: if `git remote get-url upstream` fails, or your repo root is not `/Users/brian/Desktop/comp_sci/google/agent-sandbox`, you are a cloud session and this section applies. Otherwise skip the whole section. As a cloud session you are in a fresh Ubuntu VM holding a clone of `origin` (`briankhoi/agent-sandbox`) at one branch. There is no `upstream` remote, no git-town, no `EnterWorktree` tool, and none of Brian's untracked files. The deltas below replace the matching steps in section 1; from section 2 onward everything applies unchanged, except where noted.

- **Before anything else, make sure you actually have PR 1's branch.** Depending on how the session was started, the clone may be on the fork's default branch and may not carry every ref, so do not assume: `git fetch origin feat/batch-1-core:feat/batch-1-core` (a no-op if it is already there and already current).
- **Section 1, steps 1, 4, 5, 6 (git-town and the worktree):** skip them. The VM is disposable, so work directly in the clone: `git switch -c feat/batch-2-cohorts feat/batch-1-core`. Brian registers the git-town stack parent himself after he pulls your branch.
- **Section 1, step 2 (confirm PR 1's tip):** confirm `git log --oneline -1 feat/batch-1-core` is `4e9116a` and that `git rev-parse feat/batch-1-core~5` is `ce66bdc`, which is upstream `main` at the point PR 1 branched. Do not use `origin/main` as a base: the fork's `main` is behind upstream (`527d934`, an ancestor of `ce66bdc`). `ce66bdc` is reachable from the branch, so you have everything you need without `upstream`.
- **Section 1, step 3 (main drift):** skip it. You cannot see `upstream` from the VM. Brian checks for drift locally before merging.
- **Section 1, step 7 (guard the untracked planning files):** there is nothing to guard, because the planning docs are committed on `origin/batch-primitives-notes` instead. Fetch that branch and read from it, never merge it: `git fetch origin batch-primitives-notes:batch-primitives-notes`. If that fetch fails, check whether the branch is already local (`git branch --list batch-primitives-notes`) and just read from it; depending on how the session was created the repository may have arrived without a usable `origin`. Your commits must add nothing under `batch-primitives/`; the scope checks in section 6 should catch it if they do.
- **Section 2, items 1 and 2 (the proposal and the plan):** read both from `batch-primitives-notes`, not from `batch-claim-proposal` and not by absolute path: `git show batch-primitives-notes:batch-primitives/batch_claim_proposal.md` and `git show batch-primitives-notes:batch-primitives/python_sdk_batch_plan.md`. This prompt is on that branch too, at `batch-primitives/PR_2_prompt.md`; re-read it there if your context gets compacted.
- **Section 6, step 1 (`make test-unit`):** the pre-existing `packages/sandboxd/pkg/pathutil` failure is macOS-only, so on Linux the Go suite should be fully green and any Go failure is real. Check `go version` is 1.26.x first, since `go.mod` requires it. `make test-unit` downloads Go modules and pip packages, so if the environment's network policy blocks `proxy.golang.org` or PyPI, say so explicitly, run whatever you can (the Python suite needs only PyPI), and do not paper over a suite you could not run.
- **Push:** cloud sessions push through a credential proxy with scoped credentials, so `git push -u origin feat/batch-2-cohorts` may be refused even though the prompt authorizes it. If it is, do not work around it. Stop, report the exact error, and leave the commits on the branch so Brian can teleport the session and push from his machine.

## 1. Setup (in this order)

1. **Check that git-town is installed:** `git town --version` (24.0.0 on this machine). If it is missing, stop and ask Brian to run `! brew install git-town`.
2. **Fetch, and confirm PR 1's tip.** Run from the repo root. `feat/batch-1-core` must be at `4e9116a`, the reviewed and approved commit:
   ```bash
   git fetch origin
   git fetch upstream
   git fetch upstream main:main    # refuses non-fast-forward; stop and report if it refuses
   git log --oneline -1 feat/batch-1-core
   git log --oneline main..feat/batch-1-core    # expect exactly 5 commits
   ```
   If `feat/batch-1-core` is not at `4e9116a`, stop and report what it is at instead. It was rebased onto `ce66bdc` and force-pushed several times during review, so do not assume an older SHA you may find quoted elsewhere.
3. **Check whether `main` has moved under PR 1:**
   ```bash
   git diff --stat main...feat/batch-1-core -- clients/python/agentic-sandbox-client | tail -1
   git log --oneline $(git merge-base main feat/batch-1-core)..main -- clients/python/agentic-sandbox-client Makefile docs/python_sdk_reference.md
   ```
   If the second command prints commits, `main` has changed the Python SDK since PR 1 branched. Read those commits and report the drift before writing code; do not rebase PR 1.
4. **git-town config** is already set in this repo (`main-branch`, `sync-feature-strategy=rebase`, `sync-upstream`, `dev-remote`). Confirm with `git config --local --get-regexp git-town` and change nothing.
5. **Create the worktree** with the `EnterWorktree` tool, using `name: "batch-2-cohorts"`. It lands in `.claude/worktrees/batch-2-cohorts`, on a branch the tool names automatically.
6. **Create the feature branch on top of PR 1, inside the worktree.** `feat/batch-1-core` is checked out in another worktree, so you cannot check it out here; branch from it by name instead, then register the stack parent:
   ```bash
   git switch -c feat/batch-2-cohorts feat/batch-1-core
   git town set-parent feat/batch-1-core --non-interactive
   git config --get git-town-branch.feat/batch-2-cohorts.parent    # expect feat/batch-1-core
   ```
   If `git town set-parent` fails or turns interactive, set the config directly and say so in your report:
   ```bash
   git config git-town-branch.feat/batch-2-cohorts.parent feat/batch-1-core
   git config git-town-branch.feat/batch-2-cohorts.branchtype feature
   ```
7. **Guard the untracked planning files** so they can never be committed:
   ```bash
   EX="$(git rev-parse --path-format=absolute --git-common-dir)/info/exclude"
   for f in batch-primitives/python_sdk_batch_plan.md batch-primitives/PR_1_prompt.md batch-primitives/PR_2_prompt.md; do
     grep -qxF "$f" "$EX" || echo "$f" >> "$EX"
   done
   ```

Everywhere below, "the base" means `feat/batch-1-core`: diff and scope-check against it, not against `main`.

## 2. Read before writing code

1. **The proposal** is the source of truth for behavior. It is committed on branch `batch-claim-proposal`, not on `main`:
   `git show batch-claim-proposal:batch-primitives/batch_claim_proposal.md`
   Read Batch Model, Membership, the Shutdown Backstop, Cleanup, and usage examples 2 and 3, which are the examples this PR makes work.
2. **The implementation plan** is untracked and lives only in the main checkout (a cloud session reads it from `batch-primitives-notes` instead, per the cloud section). Read it by absolute path; do not copy it into the worktree:
   `/Users/brian/Desktop/comp_sci/google/agent-sandbox/batch-primitives/python_sdk_batch_plan.md`

   Read these sections in full:
   - "Ground rules (every PR)"
   - every subsection of "Shared contracts", especially "Claim manifest", "Lease", "Dispatched set" (which now carries the consumer-mode rules), and "Defaults"
   - "Decisions", both the "Resolved" table and "Still open"
   - "PR 2", all of it

   Skim PR 3 to PR 5 only to learn what not to build. The plan wins over this prompt if they disagree; report any disagreement.
3. **`AGENTS.md`** at the repo root, especially "Python SDK conventions".
4. **PR 1's code**, which you are extending, under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`:
   - `batch_state.py`: `BatchState` (`_members`, `_ordinals`, `_initial_fill`, `_released`, `_dispatched`, `_next_ordinal`), `seed_from_claims`, `upsert_claim`, `mark_lost`, `resync_from_list`, `mark_released`, `members`, `get_member`, `try_dispatch`, `note_error`, `error`, and the module functions `derive_member`, `reconstruct_groups`, `compute_next_ordinal`, `parse_ordinal`, `parse_lease_duration_annotation`, `validate_lease_duration_value`, `is_lease_stale`, `validate_batch_id`, `generate_holder_identity`.
   - `sandbox_batch.py` and `async_sandbox_batch.py`: `_attach`, `_start`, `_watch_loop` (including its 410 re-list and 429/5xx retries), `_renew_loop`, `_renew_once`, `detach`, `_check_active`. Three deliberate asymmetries to preserve: the async handle has `_stop_background_tasks()` while the sync handle stops its threads inline in `detach`; the sync `members()`/`err()` take the `threading.Lock` while their async twins are plain `def` with no lock, since no `await` inside them means no task can interleave; and the sync `detach` joins the renewal thread but deliberately does not join the watch thread, since the watch stream blocks until the server closes it at `_WATCH_TIMEOUT_SECONDS` and joining it would make `detach()` take up to 30s on a quiet batch. The watcher is a daemon that re-checks `_watch_stop` before every state write, so it mutates nothing after `detach` sets the flag, and the async twin just cancels its task.
   - `sandbox_client.py` / `async_sandbox_client.py`: `get_batch`, `_active_batches`, `_unregister_batch`, `delete_all`, async `close`, and `_atexit_cleanup`.
   - `k8s_helper.py` / `async_k8s_helper.py`: `create_sandbox_claim`, `delete_sandbox_claim`, `list_sandbox_claim_objects`, `watch_sandbox_claims`, `read_batch_lease`, `replace_batch_lease`.
   - `utils.py`: `construct_sandbox_claim_lifecycle_spec(shutdown_after_seconds: int)` returns `{"shutdownTime": ..., "shutdownPolicy": "Delete"}` and rejects anything where `type(v) is not int`.
   - `pod_metadata.py`: `validate_labels`.
   - `test/unit/test_sandbox_batch.py`, `test_async_sandbox_batch.py`, `test_batch_state.py`: match this style, including the `TestWatcher` fixtures.

## 3. PR 2 invariants (checklist; details are in the plan)

### `claim_batch` signature and validation

- `claim_batch(groups, *, namespace="default", labels=None, batch_id=None, create_rps=None, max_in_flight=None, work_budget=None, quorum_timeout=None, lease_duration=None)` on both clients.
- `lease_duration`, `work_budget`, and `quorum_timeout` are all `int | None`, never `float`. `lease_duration` reuses `validate_lease_duration_value` (`type(v) is int` and `> CLOCK_SKEW_MARGIN`). `work_budget` and `quorum_timeout` use the same `type(v) is int` rule with `> 0` (OPEN-T).
- `groups` must be non-empty; every `size > 0` in this PR (`size=0` is PR 4); `min_ready` is validated by `BatchGroup` itself; duplicate `warmpool` names are rejected, because members are bucketed by `spec.warmPoolRef.name`.
- `labels` go through `validate_labels` and must not set `BATCH_ID_LABEL`.
- Every `ValueError` is raised before any Kubernetes object is created.

### Dependency precheck (OPEN-W)

- After the local validation and before the Lease is created, `GET` each group's `SandboxWarmPool` in the batch's namespace, then `GET` the template it names in `spec.sandboxTemplateRef.name` (that exact field; it is not `templateRef`), in the same namespace. This mirrors the controller's `getWarmPool` / `getTemplateForWarmPool`.
- A missing pool raises the SDK's existing `SandboxWarmPoolNotFoundError`, a missing template raises `SandboxTemplateNotFoundError`, both before anything is created. Any other status propagates; do not swallow it.
- This is what makes `WarmPoolNotFound` and `TemplateNotFound` safe to treat as non-terminal at runtime (OPEN-W): `derive_member` leaves such a member pending, and if the dependency never appears the group times out through `quorum_timeout`. Batch members classify only `TERMINAL_CLAIM_READY_REASONS` and create failures as terminal. PR 1 already carries this `derive_member` rule as of `4e9116a`; do not re-litigate it here.

### Lease creation

- Create `coordination.k8s.io/v1` Lease `batch-<id>` with labels `{BATCH_ID_LABEL: id, CREATED_BY_LABEL: "python-client"}`, `holderIdentity` from `generate_holder_identity()`, `leaseDurationSeconds = lease_duration`, and `acquireTime`/`renewTime` = now.
- Annotations, all decimal strings: `BATCH_LEASE_DURATION_ANNOTATION`, `BATCH_WORK_BUDGET_ANNOTATION` (`agents.x-k8s.io/batch-work-budget`), `BATCH_QUORUM_TIMEOUT_ANNOTATION` (`agents.x-k8s.io/batch-quorum-timeout`). Only `claim_batch` ever writes them.
- A 409 raises `BatchExistsError`.
- `_attach` (`get_batch`) parses the two new annotations onto the handle, next to the existing duration parse: missing falls back to the default constant, present-but-not-a-positive-integer raises `BatchError` (OPEN-E). PR 4 consumes them; PR 2 only has to carry them.

### Order of operations

1. Validate, then precheck each group's warm pool and template. 2. Create the Lease. 3. List and start the watch. 4. Start renewal. 5. Start background creation. 6. Register in `_active_batches`. 7. Return.
- The watch must be running before the first claim `POST`, so no status transition can be missed.
- If any step before 5 fails, delete the Lease and re-raise.
- `claim_batch` returns before the creates finish.

### Creation pipeline

- Compute every claim name upfront: ordinals `0..N-1` assigned across groups in the order the groups were passed, `<batch-id>-<ordinal>`. Workers never allocate ordinals.
- Record per-ordinal outcomes in a pre-allocated, ordinal-indexed table. Anything touching `BatchState` still goes through the handle's existing lock, since the watcher writes there too.
- Pace with a token bucket at `create_rps` (default 50.0) and at most `max_in_flight` (default 20) concurrent creates: sync, a bounded executor fed by a paced producer; async, a semaphore plus a paced producer task.
- Create retry: 3 attempts total, jittered exponential backoff, on 429 and 5xx, honoring `Retry-After`. No retry on 400/403/404/422. A 409 on the first attempt is a name collision and fails; a 409 on a later attempt counts as success, since the name is deterministic.
- A create that ultimately fails becomes a synthetic terminal member (reason `"CreateFailed"`, the API error as its message) and emits `MEMBER_FAILED`.
- **Per-group fail-fast (OPEN-S):** count create failures per group. Once `create_failed > size - min_ready` for a group, cancel that group's not-yet-started creates and mark the group unreachable. Other groups keep filling. Never auto-release, never delete an already-created member: only the caller's `release()` tears the batch down.

### `events()`

- Single consumer; a second call raises `BatchError`.
- Emits `MEMBER_READY`, `MEMBER_FAILED`, and `MEMBER_LOST` for initial-fill members only, meaning members in `BatchState._initial_fill` (OPEN-H), plus `LEASE_DEGRADED` from the renewal loop, once per degraded episode.
- Every `MEMBER_READY` goes through `try_dispatch`, so nothing is handed out twice on one handle.
- Closes after draining once the initial fill settles: every initial-fill member is Ready, terminal, lost, released, or create-failed. Released members count as unable to arrive (OPEN-5b). It also closes on `release()` and on `detach()`.
- A member still pending at `quorum_timeout` counts as unable to arrive too (OPEN-X), so the stream closes even though the controller retries a missing dependency forever and a pod stuck unschedulable carries no terminal reason. The deadline applies in stream-only mode as well, where no group ever yields a verdict.

### Consumer mode and hold-back (OPEN-F)

- The first of `events()` and `iter_ready_groups()` to be called fixes the handle's mode, under the state lock. PR 3 adds `wait_for_quorum()` to the same lock.
- Quorum consumer first: `events()` holds back each non-zero group's first `min_ready` Ready members, in Ready order, until that group yields; later Ready members of the group stream immediately.
- `events()` first: the handle is stream-only and `iter_ready_groups()` raises `BatchError`.
- When a group yields an error instead of members (unreachable, or the timeout), its held-back members are released to `events()`. Nothing else would ever deliver them.

### `iter_ready_groups()`

- Yields one `GroupReady` per non-zero group, exactly once each, then closes.
- Ready: `members` is exactly `min_ready` members, earliest-Ready first, dispatched atomically under the lock.
- Unreachable: `members=[]`, `error=QuorumUnreachableError` carrying `.warmpool` and the counts. Unreachable means `size - terminal - lost - create_failed - released < min_ready`.
- Timed out: a group that has not yielded within `quorum_timeout` yields `error=TimeoutError("Group quorum timed out")` (OPEN-J). One group timing out does not affect the others.
- `min_ready=0` yields immediately with no members.
- Extra Ready members beyond `min_ready` go to `events()`.

### Re-attached handles (OPEN-G)

- Quorum is a level-triggered check on current cluster state, so `iter_ready_groups()` works on a handle from `get_batch` and must not raise. A group already at `min_ready` yields immediately, without blocking.
- The dispatched set is per handle and starts empty after `get_batch`. A member the previous driver already delivered can be delivered again to the new handle; at-most-once is a per-handle guarantee.
- Ready order is only known for transitions this handle observed, so for members already Ready at attach time, use ordinal order wherever the contract says "earliest-Ready first".

### `release()`

Idempotent, and in this order:

1. Stop creation (cancel outstanding creates), the watch, and renewal. Factor the sync teardown into a helper mirroring the async `_stop_background_tasks()` so `release()` and `detach()` share it; it sets both stop events and joins the renewal thread only, per the watch-thread asymmetry in section 2.
2. Close connected sandbox handles.
3. `deletecollection` with label selector `BATCH_ID_LABEL=<id>`.
4. Re-list by label and repeat, bounded, until only claims with a `deletionTimestamp` remain (proposal, Cleanup 1).
5. Delete the Lease last, so the reaper can still clean up if `release()` dies partway.
6. Wake all waiters with `BatchError` and end `events()`.
7. Unregister from the client via `_unregister_batch`.

There is no fallback when a deletion fails (OPEN-U). A 403 means the driver Role is missing the `deletecollection` verb that the proposal's RBAC section grants on sandboxclaims, so `release()` raises it rather than deleting claim by claim; the per-claim `shutdownTime` backstop still bounds the leak. Steps 6 and 7 are the one exception: wake waiters and end `events()` even when step 3 or 4 raises, since the watch is already stopped and nothing else would ever wake a blocked consumer. Leave the Lease in place and the handle registered, so the reaper can act and a later `release()` retries the deletion.

### Exit hooks

- `SandboxClient.delete_all()` releases tracked batches, which covers the sync atexit path when `cleanup=True`.
- `AsyncSandboxClient.delete_all()` and `__aexit__` release them.
- `AsyncSandboxClient._atexit_cleanup` releases them through the synchronous `K8sHelper` it already builds: `deletecollection`, then the Lease delete, each bounded by `_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS`, for the interpreter-shutdown reason documented on that method.
- A detached batch is skipped, not released.

## 4. Files

Create or modify only these, under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/` unless noted:

- `constants.py`: the two new annotation names, `BATCH_DEFAULT_CREATE_RPS = 50.0`, `BATCH_DEFAULT_MAX_IN_FLIGHT = 20`, `BATCH_CREATE_MAX_ATTEMPTS = 3`, the backoff constants, and the `sandboxwarmpools` / `sandboxtemplates` plurals for the precheck.
- `exceptions.py`: `BatchExistsError(BatchError)`, `QuorumUnreachableError(BatchError)`.
- `models.py`: only if a `claim_batch` input helper genuinely belongs there.
- `batch_state.py`: ordinal planning for the fill, create-failure members, per-group accounting over initial-fill members, settle detection, the mode lock, hold-back bookkeeping, and `parse_positive_int_annotation` for the two new annotations. Keep this module I/O-free: no threads, no asyncio, no Kubernetes calls.
- `k8s_helper.py`, `async_k8s_helper.py`: `create_batch_lease`, `delete_batch_lease`, `delete_sandbox_claim_collection(namespace, label_selector)` wrapping `CustomObjectsApi.delete_collection_namespaced_custom_object`, and the precheck getters `get_sandbox_warmpool` / `get_sandbox_template`.
- `sandbox_batch.py`, `async_sandbox_batch.py`: the creation pipeline, the create-retry helper, `events()`, `iter_ready_groups()`, `release()`, `LEASE_DEGRADED` emission, the `_attach` annotation parse, and the sync teardown helper.
- `sandbox_client.py`, `async_sandbox_client.py`: `claim_batch` and the exit hooks.
- `__init__.py`: export the new exceptions and any new public model.
- `README.md` (the package one; it is published as the docs site's Python client page): extend the batch section with stream and per-group consumption, and state the OPEN-F ordering rule, "call `iter_ready_groups()` before iterating `events()`".
- `docs/python_sdk_reference.md` at the repo root: regenerated, never hand-edited.
- Tests: extend `test/unit/test_sandbox_batch.py`, `test_async_sandbox_batch.py`, `test_batch_state.py`, and add `test/e2e/clients/python/test_batch_e2e.py` scenarios.

Sync and async must stay at parity: every behavior change lands in both shells and both test files.

## 5. Tests

Both shells, unless a case is state-core-only. Use fake clocks for pacing and timeouts; no `sleep`-based timing tests.

### Validation
- Empty `groups`; a group with `size=0`; `min_ready > size`; negative sizes; duplicate `warmpool`; caller `labels` setting `BATCH_ID_LABEL`; a malformed `batch_id`.
- Each of `work_budget` and `quorum_timeout` rejects `1.5`, `60.0`, `True`, `"60"`, `0`, and `-5` with `ValueError`, before any Lease create call is made.
- `lease_duration` rejects `1.5`, `60.0`, `True`, `"60"`, `0`, `-5`, and `5`; the `5` case raises exactly `"Duration must be greater than clock skew margin (5s)"`. `6` is accepted.

### Sequence
- Call order: Lease create, then list, then watch start, then the first claim create. Assert on a recorded call order, not on timing.
- A Lease 409 raises `BatchExistsError` and creates no claims.
- A failure starting the watch deletes the Lease and re-raises.
- `claim_batch` returns before all creates finish.

### Dependency precheck
- A missing warm pool raises `SandboxWarmPoolNotFoundError` and a missing template raises `SandboxTemplateNotFoundError`, each before the Lease is created and with no claim creates.
- A 500 from either `GET` propagates unchanged.
- With both present, `claim_batch` proceeds normally.
- A member whose `Ready` condition is `False` with reason `WarmPoolNotFound` or `TemplateNotFound` is not terminal: no `MEMBER_FAILED`, it does not count against its group's reachable count, and the group times out through `quorum_timeout` if it never resolves.

### Lease fields and annotations
- `claim_batch(lease_duration=90, work_budget=1200, quorum_timeout=300)` writes `leaseDurationSeconds: 90`, `batch-lease-duration: "90"`, `batch-work-budget: "1200"`, `batch-quorum-timeout: "300"`, and renews every 30 s.
- Defaults write 60 / 3600 / 600 and renew every 20 s.
- `get_batch` on that Lease restores 90 s and parses both new annotations onto the handle; a missing annotation falls back to the default; `"abc"` and `"0"` each raise `BatchError`.

### Manifests
- Ordinals `0..N-1` across groups in argument order, names `<id>-<ordinal>`.
- Group annotations match each member's own group; labels include `BATCH_ID_LABEL`.
- `shutdownTime` equals that claim's own create time plus `quorum_timeout + work_budget + margin`, checked with a fake clock at two different create times so a per-batch computation fails the test.

### Pacing and retry
- With a fake clock, no more than `create_rps` creates start in any one second.
- An instrumented fake create never sees more than `max_in_flight` concurrent calls.
- Retry table: 429 then success; 503 then success; 409 after a retried 503 counts as success; 409 on the first attempt fails; 400/403/404/422 fail with no retry; a third attempt is the last, and exhaustion produces a create failure; `Retry-After` is honored.

### Per-group fail-fast (OPEN-S)
- A size-4, `min_ready=3` group whose creates fail twice cancels its remaining creates and yields `QuorumUnreachableError`, while a healthy second group still yields its members.
- That case issues no `deletecollection` and leaves the Lease in place: no auto-release.

### `events()`
- Ordering; `MEMBER_FAILED` for create failures and for terminal claims; `MEMBER_LOST` for a vanished claim.
- Only initial-fill members produce events (OPEN-H).
- Closes on settle with a mixed outcome (some Ready, one terminal, one create-failed, one released).
- Closes at `quorum_timeout` on a fake clock when one member stays pending forever, both in stream-only mode and after a quorum consumer (OPEN-X).
- Closes on `release()`; a second consumer raises `BatchError`; `LEASE_DEGRADED` is emitted once per episode.

### `iter_ready_groups()`
- A fast group yields before a slow one.
- Exactly `min_ready` members per yield, extras streamed on `events()`.
- An unreachable group yields an error while the others still yield members.
- `min_ready=0` yields immediately.
- A group stuck below `min_ready` yields `error=TimeoutError` at `quorum_timeout` on a fake clock, and the other groups are unaffected.
- Closes after every non-zero group has yielded once.

### Mode lock and re-attach
- Quorum-consumer-first holds back exactly `min_ready` per group from `events()` and streams the extras immediately.
- `events()`-first makes `iter_ready_groups()` raise `BatchError`.
- A group that yields `QuorumUnreachableError` or `TimeoutError` releases its held-back members to `events()`.
- `get_batch` on a batch whose group is already at `min_ready`: `iter_ready_groups()` returns immediately without blocking; those members are not re-emitted by the same handle's `events()`; members already Ready at attach and not taken by a quorum consumer are emitted in ordinal order.

### Dispatched set
- No member is handed out twice across `events()` and `iter_ready_groups()` on one handle, under interleaved watch updates (seeded random interleavings, driven through the state core).

### `release()`
- The `deletecollection` label selector is exactly `agents.x-k8s.io/batch-id=<id>`.
- The re-list loop ends when only claims with a `deletionTimestamp` remain, and is bounded.
- The Lease is deleted last.
- Idempotent: a second `release()` is a no-op and raises nothing.
- Waiters are woken with `BatchError`; `events()` ends; outstanding creates are cancelled; the handle is unregistered.
- A 403 from `deletecollection` propagates to the caller, the Lease is not deleted and the handle stays registered, but waiters are still woken and `events()` still ends; a second `release()` retries the deletion and succeeds (OPEN-U).

### Exit hooks
- `delete_all()` releases tracked batches in both clients; `__aexit__` releases.
- The async atexit path calls the sync helper's `deletecollection` and Lease delete with `_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS`.
- Detached batches are skipped.

### E2E (`test/e2e/clients/python/test_batch_e2e.py`)
1. `claim_batch` with one size-3 group; iterate `events()` to close, expecting 3 `MEMBER_READY`; `release()`; assert no labeled claims and no Lease remain.
2. Two groups, one on a nonexistent warm pool: `iter_ready_groups()` yields an error for that group and members for the other.

## 6. Verify (all must pass before you push; paste results in your report)

1. **Full unit gate,** from the worktree root. This also runs the Go unit tests:
   ```bash
   make test-unit
   ```
   One Go failure is pre-existing on macOS and unrelated: `packages/sandboxd/pkg/pathutil TestSanitizePathAbsolutePathConfined` (a `/private/var` symlink). It also fails on `main`. Report it as pre-existing; do not fix it.
2. **Fast loop while iterating,** using the venv `make test-unit` creates at `bin/python-venv-k8s-agent-sandbox`:
   ```bash
   bin/python-venv-k8s-agent-sandbox/bin/python -m pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit -q
   ```
   PR 1 leaves this at 616 passed, 1 skipped, 56 subtests passed. Your run must be strictly higher with no failures.
3. **Type check,** the same way `dev/tools/test-unit` runs it, from the package directory:
   ```bash
   ../../../bin/python-venv-k8s-agent-sandbox/bin/python -m mypy k8s_agent_sandbox
   ```
4. **Base-install check:** without the `async` extra, the package still imports and the async class raises the placeholder error.
   ```bash
   python3 -m venv bin/py-base-check && bin/py-base-check/bin/pip install -q -e clients/python/agentic-sandbox-client
   bin/py-base-check/bin/python -c "import k8s_agent_sandbox as m; m.SandboxBatch; m.BatchGroup; m.GroupReady; m.QuorumUnreachableError; print('base import ok')"
   ```
5. **Docs:** run `make generate-python-docs` and check the diff to `docs/python_sdk_reference.md`. Then `make toc-verify`, and `make toc-update` if it fails.
6. **E2E (optional):** needs Brian's OK before any kind-cluster work. Run it the way `run_python_e2e_tests` in `dev/tools/test-e2e` runs the Python suite, limited to `test/e2e/clients/python/test_batch_e2e.py`, against a cluster from `make deploy-kind EXTENSIONS=true`. Without the OK, commit the test anyway and say in your report that it has not been run.
7. **Scope checks.** Both commands must print nothing:
   ```bash
   git diff feat/batch-1-core --stat -- clients/go examples/agent-sandbox-rl api extensions/api controllers k8s
   git status --porcelain | grep batch-primitives
   ```

This repo has no Python linter configured (see `AGENTS.md`). Do not add one, and do not reformat existing code.

## 7. Commit, then push

**How to split commits.** Make logical commits, each one a concern a reviewer would rather read on its own. Do not commit a chronological series of attempts or fixups. While iterating, fold each change into the commit it belongs to:
- for the latest commit, use `git commit --amend`;
- for an earlier commit, use `git commit --fixup=<sha>` and then `git rebase --autosquash feat/batch-1-core`.

The non-interactive `--autosquash` works on this machine's git 2.50.1. Do not use `-i`. If your git rejects `--autosquash` without `-i`, run `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash feat/batch-1-core`, which applies the same todo list without opening an editor. Rebase onto `feat/batch-1-core`, never onto `main`, and never let a rebase rewrite PR 1's commits.

Each commit should leave the SDK unit tests passing. Suggested split:

1. `feat(python-sdk): add batch creation constants and exceptions`: `constants.py`, `exceptions.py`, `__init__.py`, and their tests.
2. `feat(python-sdk): add batch Lease creation and claim deletecollection helpers`: both `k8s_helper` modules and their tests.
3. `feat(python-sdk): add cohort accounting and consumer mode to batch state`: `batch_state.py` and `test_batch_state.py`.
4. `feat(python-sdk): fill batches from claim_batch and stream members`: the two handle modules, the two client modules, the README section, and the handle tests.
5. `test(python-sdk): add batch claim e2e scenarios`: the e2e file, if written.
6. `docs(python-sdk): regenerate Python SDK reference for claim_batch`: the regenerated `docs/python_sdk_reference.md`. Generated output gets its own commit so it's easy to skip in review.

**Commit messages** follow AGENTS.md. Lead with the motivation, meaning the gap each commit fills, not a restatement of the diff. Use the terms already used in the plan and the code. Add no trailers of any kind.

**Before pushing:**
```bash
git log --oneline feat/batch-1-core..HEAD        # your commits only, PR 1's not rewritten
git log --oneline main..HEAD                     # PR 1's 5 commits, then yours
git log --format='%(trailers)' feat/batch-1-core..HEAD   # must print nothing
git rev-parse feat/batch-1-core origin/feat/batch-1-core # must match: PR 1 untouched
git remote get-url origin                        # must be git@github.com:briankhoi/agent-sandbox.git
```

**Push once:**
```bash
git push -u origin feat/batch-2-cohorts
```

Do not open a PR. Do not push again without Brian's explicit go-ahead.

## 8. Report back (then stop)

- **Setup:** the `main` commit you synced to; the `feat/batch-1-core` commit you branched from; any drift you found in step 3; the name of the leftover branch `EnterWorktree` created; whether `git town set-parent` worked or you fell back to config. A cloud session has none of the last three, so instead report `git --version`, `go version`, and anything the environment blocked.
- **Commits:** the `git log --oneline feat/batch-1-core..HEAD` output and the pushed branch name, plus the GitHub URL: `https://github.com/briankhoi/agent-sandbox/tree/feat/batch-2-cohorts`.
- **Files** created and modified, one line each.
- **Results:** the tail of `make test-unit` with the pass/fail summary, the pytest count against PR 1's 616, the mypy line, the base-install check, and both scope checks. Say whether the e2e test ran.
- **Deviations** from the plan, each with its reason. Ideally there are none.
- **Ambiguities** you hit and how you resolved them. For any you couldn't resolve, give the question for Brian.
- **Draft PR title and body,** for Brian to use later. Do not open the PR.
  - Title: `feat(python-sdk): create and consume batch cohorts with claim_batch`.
  - Body:
    - Motivation, and that this is PR 2 of 5, stacked on PR 1.
    - The consumer-mode rule: the first of `events()` / `iter_ready_groups()` fixes the mode, and why hold-back exists.
    - That fail-fast is per group and never auto-releases the batch.
    - That `work_budget` and `quorum_timeout` are `int`, not the proposal's `float`, and why.
    - That at-most-once delivery is a per-handle guarantee, so a `get_batch` after a driver crash can re-deliver members.
    - That `release()` raises on a `deletecollection` 403 instead of falling back to per-claim deletes, since the driver Role is specified to grant that verb.

Then stop and wait for Brian's review.
