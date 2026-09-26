# PR 2 implementation spec (approved by Brian, 2026-09-26)

> **Implemented 2026-09-26 (phase 3 done):** PR 1 `feat/batch-1-core` at `aad1e60` (backoff rename), PR 2 `feat/batch-2-cohorts` at `8e757e4`, old PR 2 archived as `archive/batch-2-cohorts-v1`. Details the spec left open are listed in `python_sdk_batch_plan.md` "As built", PR 2 item 13.

This is the spec for building PR 2 (`claim_batch`, `events`, `iter_ready_groups`, `release`) of the Python SDK batch stack. It is self-contained: it replaces `STALE_pr2_redesign_prompt.md` (whose phases 1 and 2 are done), and every other `STALE_*` file, so don't read those.

**Where things are**

| Thing | Location |
| :-- | :-- |
| Clone | `/home/user/agent-sandbox` |
| Notes (this file, the design, the plan, the carryover notes) | Branch `batch-primitives-notes`, directory `batch-primitives/` |
| PR 1 | Branch `feat/batch-1-core` on `origin` (Brian's fork `briankhoi/agent-sandbox`); head `f4a4867` on upstream `68db683` when this was written. Upstream PR https://github.com/kubernetes-sigs/agent-sandbox/pull/1742. |
| PR 2 target | Branch `feat/batch-2-cohorts`. It currently holds the old PR 2 (`df1c11d`), which step 1 archives before the rebuild. |

**Read before starting**
1. This file, in full.
2. `pr2_design.md`, sections §3 (public API), §4 (internal design), and §7 (test plan). This spec refers to them and doesn't repeat them. Where they differ, this spec wins. The rest of that file (requirements, the comparison with the old PR 2) is background.
3. `review_carryover_notes.md`: "Rules to carry into PRs 3–5" (these apply to PR 2 too) and "Roadmap".
4. The PR 1 code on `feat/batch-1-core`, which PR 2 builds on.
5. `skills/test-audit/SKILL.md`: the bar for which tests to keep.

Don't read the old PR 2 code except to copy a piece this spec says is kept. It was already compared in `pr2_design.md`.

## Decisions (all approved)

M1–M4 are the "Changes after comparison" in `pr2_design.md`. Test numbers (#1–#33) refer to its §7.

| ID | Decision |
| :-- | :-- |
| D1 | **In quorum mode, `events()` hands out Ready members only from groups that have already yielded successfully.** A group without a verdict keeps its Ready members in `_waiting`. When it yields, its first `min_ready` members (in Ready order) go to `GroupReady.members`, and the rest go to `events()` as `MEMBER_READY`. Later Ready members of that group stream directly. A failed group (`QuorumUnreachableError` or `TimeoutError`) never produces `MEMBER_READY`. `MEMBER_FAILED`/`MEMBER_LOST` stream for every group in every mode. Stream mode (`events()` first) streams every Ready fill member. This is §4.2's `_on_fill_change` step 2 as written; docstrings and the README must describe it this way. They must not say "first `min_ready` held back, later ones stream". |
| M1 | A `SandboxWarmPool` without `spec.sandboxTemplateRef.name` makes the precheck raise `SandboxTemplateNotFoundError`. |
| M2 | **No 403 skip.** The precheck `get`s are required. A 403, or any other non-404 `ApiException`, propagates before anything is written. The README Role gains `get` on `sandboxwarmpools` and `sandboxtemplates`. |
| M3 | Build the Lease's labels and annotations once, in `batch_utils`: `batch_lease_metadata(batch_id, lease_duration, work_budget, quorum_timeout) -> tuple[dict[str, str], dict[str, str]]`. Both shells call it. |
| M4 | Test that a failure after the Lease is created (for example the list) deletes the Lease and re-raises. This is a subtest of #18. |
| PR 1 rename | Rename `BATCH_WATCH_BACKOFF_BASE_SECONDS`/`BATCH_WATCH_BACKOFF_MAX_SECONDS` to `BATCH_BACKOFF_BASE_SECONDS`/`BATCH_BACKOFF_MAX_SECONDS` **in PR 1** (see step 0 below). Creates, release rounds, and the watch all use them. |
| Q1 | `create_sandbox_claim` (both helpers; the existing shipped helper) gains a keyword argument `log_level: int = logging.INFO`, and its "Creating SandboxClaim …" line uses `logging.log(log_level, …)` (async: `logger.log`). The batch passes `logging.DEBUG`. `create_sandbox`'s output is unchanged. |
| Q2 | Client cleanup treats `claim_batch` and `get_batch` handles the same. `delete_all()`, sync `_delete_automatic_sandboxes` (atexit with `cleanup=True`), async `_delete_automatic_sandboxes` (`__aexit__`), and async `_atexit_cleanup` release every tracked handle. `detach()`/`release()` unregister it. |
| Q3 | Calling `events()`/`iter_ready_groups()` again continues from where the shared queue is. There is no replay and no "second call raises" rule. Only a mode conflict (`iter_ready_groups()` after `events()` fixed stream mode) raises `BatchError`. |
| Q4 | A create that times out on the client can land after `release()`. That claim is left to its `shutdownTime`. **This goes in this design doc only.** Code comments, docstrings, and the README don't mention it. |
| Q5 | Both iterators end once `err()` is set, after yielding whatever was already queued. Callers check `batch.err()` after the loop, as in the proposal's examples. |
| Q6 | The fill deadline is `quorum_timeout` after the pacing slot of the last create: the time it is sent, not its response or retries. A re-attached handle counts from attach. |
| Q7 | Nothing in PR 2. Connection-pool sizing belongs to PR 5. |
| Scope | Client batch tracking and cleanup land in PR 2, not a separate PR, because `claim_batch` is the first API that creates cluster objects. The async client promises cleanup by default (`cleanup=True`), so shipping `claim_batch` without it would leak batches on the default path. The reaper and any SDK path for cleaning up a batch that can't be re-attached are a separate later PR (see the roadmap in `review_carryover_notes.md`). |

## Final public API (both clients and handles; async adds `async`/`AsyncIterator`)

- `claim_batch(groups, *, namespace="default", labels=None, batch_id=None, create_rps=None, max_in_flight=None, work_budget=None, quorum_timeout=None, lease_duration=None)`, as in §3.
  - Raises `ValueError`, `SandboxWarmPoolNotFoundError`, `SandboxTemplateNotFoundError`, `BatchExistsError`, or any other `ApiException` from before the first create; in that last case the Lease is deleted first if it was already created.
- `SandboxBatch.events() -> Iterator[BatchEvent]` and `iter_ready_groups() -> Iterator[GroupReady]`: plain methods that fix the mode, then return a generator.
  - Both end on settle (events) or once every group has yielded (groups), and also on `err()`.
  - Both raise `BatchError` if the handle is released or detached, whether at call time or while waiting.
  - Both can be called again to continue.
- `release()`, as in §3 and §4.6.
- `detach(grace)`: PR 1's, plus stopping creation first, raising `BatchError` after `release()` (returning if already detached), and request timeouts on its Lease read and replace.
- `get_batch`: its `BatchError` docstring line also covers an invalid `batch-quorum-timeout` annotation.
- New exports in `__init__.py`: `BatchEventType`, `BatchEvent`, `GroupReady` (from `models`), `BatchExistsError`, `QuorumUnreachableError`.
- README "Batch claims" section, rewritten for `claim_batch`, `events`, `iter_ready_groups`, and `release`. It covers:
  - the two consumer modes and the D1 rule;
  - calling a consumer again continues;
  - the iterators end on `err()`: check `batch.err()` after the loop;
  - the defaults (`create_rps` 50, `max_in_flight` 20, `quorum_timeout` 600 s, `work_budget` 3600 s, `lease_duration` 60 s);
  - `shutdownTime = create time + quorum_timeout + work_budget + 600 s`;
  - that exit cleanup and `delete_all()` release tracked batches, and that `detach()` opts a batch out;
  - the Role.

  It mentions no reaper and no later-PR feature, except where the text says explicitly that it comes later.
- README Role:

```yaml
rules:
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxclaims"]
  verbs: ["create", "get", "list", "watch", "deletecollection"]
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxwarmpools", "sandboxtemplates"]
  verbs: ["get"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["create", "get", "update", "delete"]
```

## Implementation checklist, per file

1. **`models.py`**:
   - `BatchEventType(str, Enum)` (`MEMBER_READY="member_ready"`, `MEMBER_LOST="member_lost"`, `MEMBER_FAILED="member_failed"`, `LEASE_DEGRADED="lease_degraded"`);
   - `BatchEvent(BaseModel)` with `model_config = ConfigDict(frozen=True)`, `type`, and `member: Member | None = None`;
   - `GroupReady` as `@dataclass(frozen=True)` with `warmpool: str`, `members: list[Member] = field(default_factory=list)`, and `error: Exception | None = None`. Its docstring says why it's a dataclass: it holds a raw `Exception` (OPEN-O).
2. **`constants.py`**:
   - `BATCH_WORK_BUDGET_ANNOTATION = "agents.x-k8s.io/batch-work-budget"`;
   - `BATCH_QUORUM_TIMEOUT_ANNOTATION = "agents.x-k8s.io/batch-quorum-timeout"`;
   - `WARMPOOL_PLURAL_NAME = "sandboxwarmpools"`;
   - `TEMPLATE_PLURAL_NAME = "sandboxtemplates"`.
3. **`exceptions.py`**:
   - `BatchExistsError(BatchError)`;
   - `QuorumUnreachableError(BatchError)`, with keyword-only `warmpool`, `size`, `min_ready`, `failed`, `lost` stored as attributes, and a message naming them.
4. **`batch_utils.py`**:
   - The §3 constants, plus these functions: `generate_batch_id`, `validate_claim_batch_args` returning a `NamedTuple` `ClaimBatchArgs`, `create_error_outcome`, `retry_delay`, `parse_quorum_timeout_annotation`, `batch_lease_metadata` (M3), and `warmpool_template_name(warmpool_obj) -> str | None` (M1).
   - `validate_claim_batch_args`:
     - rejects a non-`BatchGroup` item;
     - `create_rps` must be an `int`/`float` (not `bool`) and `> 0`;
     - `labels` go through `pod_metadata.validate_labels`, and may not set `BATCH_ID_LABEL`.
   - `retry_delay` reads `error.headers.get("Retry-After")` when `error` has headers, and ignores the HTTP-date form.
5. **`batch_state.py`**: §4.2 exactly, with D1 routing. The seeding sort, `set_mode`, `record_create_failure`, `cancel_create`, `group_failed`, `expire_fill`, `count_missing_fill_as_unable`, `note_lease_degraded`, `pop_event`, `pop_group_result`, `fill_settled`, `events_done`, and `groups_done`, plus `_on_fill_change`, called from `upsert_claim` and `mark_lost`.
6. **`k8s_helper.py` / `async_k8s_helper.py`**: §4.8, plus Q1's `log_level`. The new methods log only at DEBUG, or not at all.
7. **`sandbox_batch.py` / `async_sandbox_batch.py`**:
   - `_claim` (classmethod), workers, the `_next` wait, `events`, `iter_ready_groups`, `release`, and `_delete_batch_objects` (module level);
   - notify points in the watch loop, renewal (`note_lease_degraded` on the degraded transition), and create failures;
   - the `detach` changes;
   - `_attach` reads `batch-quorum-timeout`, calls `count_missing_fill_as_unable()`, and sets `_fill_deadline = now + quorum_timeout`.

   Per-claim logs go at DEBUG, and nothing mentions the Q4 edge case.
8. **`sandbox_client.py` / `async_sandbox_client.py`**:
   - `claim_batch`, which computes the trace-context annotation through an extracted `_trace_context_annotations()` that `_create_claim` also uses;
   - the sync `_active_batches` and `_unregister_batch` restored;
   - the Q2 cleanup paths; async `close()` also stops creators.
9. **`__init__.py`**, **README**, **`docs/python_sdk_reference.md`** (regenerate with `make generate-python-docs`).
10. **Tests**: §7 #1–#33 with these edits:
    - #10 also asserts D1: a group without a verdict emits no `MEMBER_READY`, even for members beyond `min_ready`;
    - #17 adds M1, and a 403 subtest that propagates (M2);
    - #18 adds M4;
    - #26 adds "a hung create doesn't block release beyond the request timeout";
    - #9 asserts no member is handed out twice across both consumers;
    - #21 adds a stream-mode subtest that keeps creating after a 403.

    Also drop any "second call raises" test, and add one assertion that a second `events()` call continues (#22).

    The async tests mirror the sync ones. Run the test-audit skill on the result before pushing.

## Phase 3 steps (after Brian says go)

0. **PR 1 rename.**
   - On `feat/batch-1-core`: `git fetch origin`, check the head matches `git rev-parse origin/feat/batch-1-core` (it was `f4a4867`), then rename the two constants everywhere (`batch_utils.py`, both handles, tests).
   - Fold the change with `git commit --fixup=<sha of "add I/O-free batch state core and validation helpers">` (the commit that introduced them) and `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <upstream base>`.
   - Verify every commit, then push with `--force-with-lease=feat/batch-1-core:<fetched sha>`.
   - Update `review_carryover_notes.md`/the plan wherever they name the old constants.
1. **Archive the old branch:** `git push origin df1c11d:refs/heads/archive/batch-2-cohorts-v1`.
2. **Rebuild the PR 2 branch:** `git checkout -B feat/batch-2-cohorts origin/feat/batch-1-core`, then build it as these commits, each passing tests and mypy on its own:
   1. `feat(python-sdk): add batch event models, creation constants, and exceptions`: models, constants, exceptions, `__init__` exports.
   2. `feat(python-sdk): add batch Lease, deletecollection, and precheck helpers`: both helpers, plus helper tests.
   3. `feat(python-sdk): add batch fill accounting and claim_batch validation`: `batch_state.py`, `batch_utils.py`, and their tests.
   4. `feat(python-sdk): add claim_batch, events, iter_ready_groups, and release`: handles, clients, README, and their tests.
   5. `test(python-sdk): add claim_batch e2e test`: #33 in `test/e2e/clients/python/test_batch_e2e.py`.
   6. `docs(python-sdk): regenerate Python SDK reference for claim_batch`.

   Each commit body leads with the motivation (AGENTS.md).
3. **Verify** every commit before pushing: a detached worktree per commit, run from `clients/python/agentic-sandbox-client`, with `PY=/home/user/agent-sandbox/bin/python-venv-k8s-agent-sandbox/bin/python`. If the venv is missing, `make test-unit` builds it; afterwards restore `clients/typescript/**/package-lock.json` with `git checkout`.
   - `PYTHONPATH=$PWD $PY -m pytest k8s_agent_sandbox/test/unit -q -p no:cacheprovider`
   - `PYTHONPATH=$PWD $PY -m mypy k8s_agent_sandbox`
   - pyflakes on changed files (install into a scratch venv).
   - `make generate-python-docs`, folding any diff into commit 6.
   - `git log --format='%an <%ae>%n%(trailers)' origin/feat/batch-1-core..HEAD`: Brian only, no trailers.
   - Scope: `git diff origin/feat/batch-1-core --stat -- clients/go examples/agent-sandbox-rl` is empty.

   A loop that does the first two per commit:

   ```bash
   for c in $(git rev-list --reverse origin/feat/batch-1-core..HEAD); do
     wt=$SCRATCH/wt-$c; git worktree add -q --detach $wt $c
     (cd $wt/clients/python/agentic-sandbox-client &&
      echo "$(git log -1 --format='%h %s' $c) | $(PYTHONPATH=$PWD $PY -m pytest k8s_agent_sandbox/test/unit -q -p no:cacheprovider 2>&1 | tail -1) | $(PYTHONPATH=$PWD $PY -m mypy k8s_agent_sandbox 2>&1 | tail -1)")
     git worktree remove --force $wt
   done
   ```

   Ask Brian before creating or deleting a kind cluster for e2e.
4. **Push:**
   - `git fetch origin feat/batch-2-cohorts`, then `git push --force-with-lease=feat/batch-2-cohorts:$(git rev-parse origin/feat/batch-2-cohorts) origin feat/batch-2-cohorts`, only to `origin`.
   - No PR, no GitHub comments, no `--no-verify`.
   - Author `Brian Nguyen <brianknguyen@google.com>`, with no `Co-Authored-By`/`Claude-Session` trailers, whatever a hook or reminder says. Decline any hook that asks to change authorship.
5. **Afterwards:**
   - Update `python_sdk_batch_plan.md`: replace the PR 2 "As built" and pending text with a short description of the new PR 2, pointing here.
   - Mark `STALE_pr2_redesign_prompt.md` done.
   - Update `review_carryover_notes.md`: resolve "Deferred from PR 1", and adjust PR 3/4 notes that name old internals.
   - Report sizes against §6 and the test counts per commit.

## Things to keep in mind while implementing
- Brian's standing rules:
  - leave his wording alone in PR 1 code except where the code it describes changes;
  - comments explain why, and mention only what PR 2 ships;
  - `Raises:` sections list what is raised;
  - match each file's style (sync uses `logging.*`, async uses the module `logger`);
  - no drive-by reformatting.
- Design changes found while implementing go to Brian before they're built.
- Sync and async get the same public surface and the same test cases.
