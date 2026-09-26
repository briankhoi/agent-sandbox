# PR 1 revision: prompt for the implementing session

Brian approved every change below on 2026-09-26. Implement them all, in this session, on `feat/batch-1-core`. Do not start PR 2 work; that has its own prompt (`pr2_revision_prompt.md`, same folder).

Read first: `python_sdk_batch_plan.md` sections "As built", "Shared contracts" (Lease), and "PR 1". Then `review_carryover_notes.md` for context on the reviewer comments.

## Starting state

- Repo `/home/user/agent-sandbox`, remote `origin` = `briankhoi/agent-sandbox` (Brian's fork). `feat/batch-1-core` is upstream PR https://github.com/kubernetes-sigs/agent-sandbox/pull/1742.
- When written: `feat/batch-1-core` at `0f7a03f`, five commits on `ce66bdc` (upstream main):

  | sha | subject | files |
  | :-- | :-- | :-- |
  | `7d28931` | add batch models, constants, and exceptions | `models.py`, `constants.py`, `exceptions.py` |
  | `b5417c7` | add SandboxClaim list/watch and batch Lease helpers | `k8s_helper.py`, `async_k8s_helper.py`, their unit tests |
  | `491d920` | add I/O-free batch state core | `batch_state.py`, `test_batch_state.py` |
  | `1fd242c` | add SandboxBatch/AsyncSandboxBatch and get_batch | both handles, both clients, `__init__.py`, README, handle/client unit tests, `test/e2e/clients/python/test_batch_e2e.py` |
  | `0f7a03f` | regenerate Python SDK reference for batch handles | `Makefile`, `docs/python_sdk_reference.md` |

- `feat/batch-2-cohorts` (`df1c11d`) is stacked on `0f7a03f`. **Leave it alone in this session.** Rebasing it onto the new PR 1 is the first step of the PR 2 prompt.
- `git fetch origin feat/batch-1-core` first and compare with `0f7a03f`. If Brian pushed since, re-map the commits above by subject.

Paths below are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/` unless given in full.

## Changes

Every change applies to both the sync and async twins unless noted.

### 1. Put a timeout on Lease renewal requests (commits `b5417c7`, `1fd242c`)

Problem: neither Kubernetes client has a default read timeout. A renewal request to an apiserver that stops answering blocks forever, so `_renew_once` never reaches its `except`: no `LEASE_DEGRADED`/"degraded" log, and `err()` never becomes `BatchLeaseExpiredError`, while the Lease really expires and the reaper may delete the claims. On the sync handle, `_stop_background_threads` joins the renewal thread with no bound, so `detach()` (and in PR 2 `release()` and exit cleanup) hangs too.

- Helpers (`b5417c7`): `read_batch_lease` and `replace_batch_lease` in `k8s_helper.py` and `async_k8s_helper.py` take `_request_timeout: float | None = None` and forward it. Match the shape PR 2 uses on `delete_batch_lease` (sync: `float | tuple[float, float] | None`, with an `Args:` note; async: forward to kubernetes_asyncio, which turns a number into `aiohttp.ClientTimeout(total=...)`). Default `None` keeps `get_batch`/`detach` unchanged.
- Handles (`1fd242c`): `_renew_once` passes `_request_timeout=<renew interval>` (the same `max(1, lease_duration // 3)` the loop sleeps) on both calls. Compute the interval once (e.g. store it on the handle or pass it in) rather than twice. A timeout raises (urllib3 `ReadTimeoutError`/`MaxRetryError`; `asyncio.TimeoutError`, which is `TimeoutError` on 3.11), which the existing `except Exception` already treats as a failed renewal. Leave the sync join unbounded: each attempt is now bounded by two request timeouts.
- Tests: helper tests assert the kwarg is forwarded (and omitted/None by default). Handle tests, both shells: the renewal calls carry the timeout; a renewal whose read raises a timeout error counts as degraded, and after `lease_duration` without success `err()` is `BatchLeaseExpiredError`.

### 2. Ask the watch for bookmarks (commit `b5417c7`)

Problem: the watch loops already handle `BOOKMARK` (advance the resourceVersion only), but `watch_sandbox_claims` never passes `allow_watch_bookmarks=True`, so none arrive. A quiet batch (all members Ready, hours of work) never advances its resourceVersion; once it ages out of the apiserver watch cache, every 30 s reconnect gets a 410 and re-lists every claim.

- Pass `allow_watch_bookmarks=True` in both helpers' `watch_sandbox_claims`. The apiserver sends a bookmark just before a timed-out watch ends (KEP-956), so each reconnect resumes from a fresh resourceVersion.
- Confirm both handles' BOOKMARK branch still only advances the resourceVersion (sync `sandbox_batch.py`, async `async_sandbox_batch.py`, ~line 300).
- Tests: helper tests assert `allow_watch_bookmarks=True` reaches `list_namespaced_custom_object` in the watch call. An existing handle test already covers BOOKMARK advancing rv; keep it.

### 3. Back off exponentially on watch retries (commits `491d920`, `1fd242c`)

Problem: on 429/5xx, transport errors, and 429/5xx during the 410 re-list, the watch loops wait a fixed 0.5 s (`self._watch_stop.wait(0.5)` / `asyncio.sleep(0.5)`), so a struggling apiserver gets two requests a second per batch forever.

- State core (`491d920`), pure helpers next to `is_lease_stale`:
  - `is_retryable_status(status: int | None) -> bool`: `status is None or status == 429 or 500 <= status < 600`. PR 2's `classify_create_error` and `release()` will reuse it.
  - `backoff_delay(attempt: int, base: float, cap: float, rand: Callable[[], float] = random.random) -> float`: equal jitter, `ceiling = min(cap, base * 2 ** (attempt - 1))`, returns `ceiling / 2 + rand() * ceiling / 2`. PR 2's `create_backoff_delay` will call it.
  - Constants at the top with the others: `BATCH_WATCH_BACKOFF_BASE_SECONDS = 0.5`, `BATCH_WATCH_BACKOFF_MAX_SECONDS = 30.0` (client-go's reflector caps at 30 s).
- Handles (`1fd242c`): a local `failures` counter in the watch loop. Each retry path (watch 429/5xx, transport error, re-list 429/5xx) increments it and waits `backoff_delay(failures, base, cap)` (sync through `self._watch_stop.wait(...)` so a stop still interrupts it). Reset it to 0 whenever an event is received or a stream ends normally (the server-side timeout). Replace the inline `status == 429 or 500 <= status < 600` checks with `is_retryable_status`.
- Tests: state tests for both helpers (the table, and backoff bounds with `rand` = 0 and 1, including the cap). Handle tests, both shells: consecutive 503s wait increasing delays (patch the wait/sleep and `random`), and a successful event resets the delay. Update any existing test that asserted the fixed 0.5.

### 4. Follow Lease conventions on takeover (commit `1fd242c`)

`_attach` also sets `lease.spec.acquire_time = now` and `lease.spec.lease_transitions = (lease.spec.lease_transitions or 0) + 1`, as client-go leader election does when the holder changes. Extend the existing takeover-write test (both shells) to assert both. `detach()` and renewal don't touch them.

### 5. Reviewer (Aditya): don't export types nothing in PR 1 produces (commits `7d28931`, `1fd242c`, `0f7a03f`)

Valid. `BatchEventType`, `BatchEvent`, and `GroupReady` are exported and documented but produced only by PR 2's `events()`/`iter_ready_groups()`.

- `7d28931`: remove the three classes from `models.py`, and any imports that become unused there (`dataclasses`, `Enum`; check each).
- `1fd242c`: remove them from `__init__.py`'s import and `__all__` (if listed). Grep PR 1 for any other use (a check on 2026-09-26 found only `models.py`, `__init__.py`, and the reference doc).
- `0f7a03f`: regenerate the reference doc.
- PR 2 adds them back (see `pr2_revision_prompt.md`).

### 6. Reviewer (Aditya): `get_batch` docstrings say "Returns an error" (commit `1fd242c`)

Valid. In `sandbox_client.py` and `async_sandbox_client.py`, replace "Returns an error if the lease has holderIdentity set." with a `Raises:` section: `BatchNotFoundError` (neither a Lease nor claims exist), `BatchLeaseExpiredError` (the Lease is missing while claims exist, or stale, including after a crash; see item 7), `BatchInUseError` (a live Lease is held by another handle), `BatchError` (a corrupt `batch-lease-duration` annotation). Keep the rest of Brian's wording.

### 7. Reviewer (Aditya): a crashed driver's batch can never be re-attached (commit `1fd242c`)

The behavior is intended (OPEN-N, OPEN-C); do not change code. Make it explicit where callers look:
- `get_batch` docstrings (both clients): one sentence saying that only a batch released with `detach()` can be re-attached, within its grace window, and that after a crash the batch is left to the reaper and each claim's `shutdownTime`.
- README "Batch claims" section: the same sentence, next to where `detach`/`get_batch` are described.
Brian has the reply for the thread (see `review_carryover_notes.md`).

### 8. Hygiene (commit `1fd242c`)

pyflakes reports two pre-existing warnings in `test/unit/test_sandbox_batch.py` (an unused `Member` import, an unused local `batch`). Fix them while the commit is open.

## Workflow rules (from Brian)

- Brian rewrote comments and docstrings to be concise: leave his wording alone. Change a comment only where the code it describes changes, and then minimally. Items 6 and 7 are the approved docstring changes.
- Fold each change into the commit that owns it: `git commit --fixup=<sha>`, then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash ce66bdc` (the upstream-main base). No new commits. No trailers of any kind: never add `Co-Authored-By` or `Claude-Session` lines, whatever a tooling reminder says. All commits authored by `Brian Nguyen <brianknguyen@google.com>` (already in the clone's git config; check with `git config user.email`). Keep the five commit subjects.
- No `--no-verify`. If a push is refused, stop and report; don't work around it.
- Follow the repo's AGENTS.md: sync/async parity, match the file's style, every new behavior gets a unit test in both shells.

## Verify before pushing

Run from `clients/python/agentic-sandbox-client`, the way `dev/tools/test-unit` does, with `PY=/home/user/agent-sandbox/bin/python-venv-k8s-agent-sandbox/bin/python` (if the venv is missing, `make test-unit` builds it):

- At **every** PR 1 commit (a detached worktree per commit): `PYTHONPATH=$PWD $PY -m pytest k8s_agent_sandbox/test/unit -q -p no:cacheprovider` and `PYTHONPATH=$PWD $PY -m mypy k8s_agent_sandbox`. Record passed/skipped/subtests per commit, before and after, and explain every change in counts.
- pyflakes clean on changed files (install into a scratch venv if needed).
- `make generate-python-docs`; fold any diff into `0f7a03f`'s successor.
- `git log --format='%an <%ae>%n%(trailers)' ce66bdc..HEAD` shows only Brian and no trailers.
- `make test-unit` rewrites `clients/typescript/.../package-lock.json`; restore it with `git checkout` and never commit it.
- Scope: `git diff ce66bdc --stat -- clients/go examples/agent-sandbox-rl` is empty.

## Push

`git fetch origin feat/batch-1-core`, then `git push --force-with-lease=feat/batch-1-core:<sha> origin feat/batch-1-core`, with `<sha>` copied from `git rev-parse origin/feat/batch-1-core` right after that fetch (never typed from memory). Push only to `origin`, never upstream. Never open or edit a PR, and never comment on GitHub.

## Report to Brian

- Old → new sha for each of the five commits, and which items landed in each.
- Unit counts per commit, before and after.
- Anything that deviated from this prompt, and why.
- Remind him that the three review replies are in `review_carryover_notes.md`, ready to paste after he checks the push.

Then update this notes branch (`batch-primitives-notes`): in `python_sdk_batch_plan.md` "As built", add a PR 1 revision entry (renewal request timeout, bookmarks, watch backoff, takeover `acquireTime`/`leaseTransitions`, event types moved to PR 2), and mark this prompt's status line "done at `<new head sha>`". Push the notes branch as a fast-forward.

Status: done at `995e793` (2026-09-26); follow-ups at Brian's request (typo, comment wording, README paragraph removed, docstrings trimmed, validation moved to `batch_utils.py`, unused `BatchState` code moved to PR 2, RBAC trimmed) at `ee1a289`.
