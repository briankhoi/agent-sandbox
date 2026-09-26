# Review carry-over notes (read before PRs 3–5)

These are lessons from the review of PR 1 (https://github.com/kubernetes-sigs/agent-sandbox/pull/1742) and from the design pass of 2026-09-26, recorded so that later PRs don't repeat the same mistakes. `python_sdk_batch_plan.md` is still the spec, and its "As built" section wins where the two differ.

## Reviewer comments on PR 1 and how they were handled

### Aditya Shantanu

1. **"A crashed holder's stale Lease can never be re-attached; is that intended?"** Yes, by design (OPEN-N, OPEN-C). The code is unchanged. The `get_batch` docstrings now say only a detached batch can be re-attached; at Brian's request, the README and docstrings don't mention the reaper or `shutdownTime`, which PR 1 doesn't have. Reply to paste:

   > Intended. A crashed driver's batch is meant to be cleaned up rather than resumed. Once its Lease is stale, a reaper (planned as a follow-up PR) may already be deleting it, so adopting it in `get_batch` could hand the caller a batch that's being deleted underneath it. That's why the stale check runs before the holder check. Re-attaching is for planned handoff: `detach(grace)` clears `holderIdentity` and keeps the Lease live for `grace` seconds, and `get_batch` adopts it within that window. A crash while the Lease is still live gives `BatchInUseError`, since every handle has its own holder identity. The `get_batch` docstrings now say only a detached batch can be re-attached.

2. **"`BatchEvent`, `BatchEventType`, `GroupReady` are exported but nothing produces them."** Valid. They move to PR 2, where `events()`/`iter_ready_groups()` land (PR 1 item 5, PR 2 item R1). Reply:

   > Agreed. Moved them out of this PR; they're added with `events()`/`iter_ready_groups()` in the next PR of the stack.

3. **"'Returns an error' is inaccurate in `get_batch`."** Valid. Both clients now have a `Raises:` section (PR 1 item 6). Reply:

   > Fixed in both clients; the docstrings now list the exceptions `get_batch` raises.

### CodeRabbit

Checked on 2026-09-26: every CodeRabbit finding is already handled in `0f7a03f` except two, which are declined:
- **Make `GroupReady` a frozen pydantic model with `arbitrary_types_allowed`.** Declined (OPEN-O). It holds a raw `Exception`, which pydantic can only carry by switching off validation for that field, so a frozen dataclass is the simpler fit; the class docstring says why. The class moves to PR 2 anyway. Reply, if the thread is still open:

  > Keeping it a frozen dataclass: `error` holds a raw `Exception` (traceback and cause intact), which pydantic can only hold with `arbitrary_types_allowed`, i.e. without validating it, so a model would add nothing. The docstring notes the reason.

- **Remove `deletecollection` from the driver Role.** Agreed (2026-09-26). The README Role now lists only PR 1's verbs (claims `get`/`list`/`watch`, leases `get`/`update`), and PR 2 adds the rest with `claim_batch`/`release()`. Reply, if the thread is still open:

  > Agreed. Trimmed the Role to the verbs this PR uses; the next PR adds `create`/`delete`/`deletecollection` along with `claim_batch()` and `release()`.

## Rules to carry into PRs 3–5

- **Tests follow `skills/test-audit/SKILL.md`.** Each contract gets one owning test at the strongest boundary. Don't repeat a `batch_utils` or `batch_state` table at the handle level; one representative value there checks the wiring. Don't add a test that exercises the same code path as an existing one with a different exception type or status. Run the audit on each PR before handing it over.
- **A PR describes and contains only itself.** Comments, docstrings, README text, and RBAC verbs mention only what the PR has; a later feature appears only when the text says explicitly that it comes later. Likewise, no code (functions, fields, constants) whose only caller lands in a later PR: move it to the PR that calls it. Brian's rule, after the PR 1 review.
- **Export only what the PR produces.** A public type, exception, or export lands in the PR whose code first returns or raises it: `TerminalMemberError` in PR 4, and nothing new for PR 3 unless `wait_for_quorum` needs it. Public SDK surface is hard to walk back.
- **Docstrings say "Raises", and list what is raised.** Never "returns an error". Every public method that raises SDK exceptions gets a `Raises:` section in both clients/handles.
- **Every request made from a background loop, or on a path the caller can't interrupt, gets a request timeout.** Neither Kubernetes client has a default read timeout. PR 1 added one to renewal. In PR 4, `acquire()`'s create and wait paths and `release_member`/`release_not_ready` deletes need a bound; `acquire` already has its own `timeout`, so derive the request timeout from it or bound it with `BATCH_STOP_CREATION_TIMEOUT_SECONDS`.
- **Transient-error policy is shared.** Use `batch_state.is_retryable_status` and `backoff_delay` (PR 1) for any new retry loop, e.g. `acquire`'s create through the shared `_create_with_retry`, and `release_not_ready`'s per-claim deletes (404 is success there). Honor `Retry-After` through `parse_retry_after`.
- **Anything that scales with N must hold up at the proposal's stated scale (tens of thousands of claims).** Check each new timing against pacing time (`N / create_rps`), deletecollection time, and watch-cache aging, as R4/R5 and the bookmark change did.
- **Quorum-mode semantics after proposal A.** A group with an error verdict is finished: no creates, no hand-outs. PR 3's `wait_for_quorum()` failing with `QuorumUnreachableError`/`TimeoutError` should treat **every** group as finished the same way (no further creates, no `MEMBER_READY`). Decide whether that follows from the same `group_failed` check, or needs a batch-level "failed" flag, when PR 3 is planned. It is not specified yet.
- **The fill deadline counts from the last paced create** (PR 2 R4). `wait_for_quorum(timeout=None)` in PR 3 should default to the same deadline, not to `now + quorum_timeout`.
- **PR 4 must parse `batch-work-budget` back in `get_batch`** ("As built" item 7), and `acquire` uses `is_retryable_status`/`backoff_delay` like the fill.
- **PR 5 pool validation** (OPEN-I): the watch and renewal each hold a connection, so validate `pool >= max_in_flight + 2`.

## Ideas raised but not planned

- **An SDK path to clean up a batch that can't be re-attached.** After a crash, `get_batch` raises, so the SDK has no way to delete a stale batch before the reaper or `shutdownTime` does; today that takes `kubectl delete sandboxclaims -l agents.x-k8s.io/batch-id=<id>` plus deleting the Lease. A `client.delete_batch(batch_id, namespace)` doing exactly the reaper's steps (label `deletecollection`, bounded re-list, then Lease delete, never adopting the Lease) wouldn't race the reaper, since both only delete. Worth raising with Brian when PR 6 (reaper) is planned, since the two would share code.
- **A per-group fill deadline** (each group's clock starting at its own last paced create) instead of R4's batch-wide one. Declined for now as extra state for a small gain; revisit if groups are very unequal in size.
