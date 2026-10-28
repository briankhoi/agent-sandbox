# C3: 22/N Stop urllib3 from retrying timed-out sync claim deletecollection and count (`feat/batch-10-deletecollection-retry`)

Paste everything in the fenced block into a fresh conversation.

````
## Context
You are a task conversation in Brian Nguyen's batch claiming project (Python SDK, `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`, remote `origin`; upstream remote `upstream`). You have no memory of earlier work. Other conversations run at the same time: the stack conversation (owns the PR stack), coordinator 1 and coordinator 2 (each writes prompts and verifies results; the one that wrote this prompt verifies yours). Repo root: /usr/local/google/home/brianknguyen/agent-sandbox. Notes worktree (branch `batch-primitives-notes`): /usr/local/google/home/brianknguyen/agent-sandbox-notes, directory `batch-primitives/`.

## Hard rules
- Commits authored by `Brian Nguyen <brianknguyen@google.com>` (set `git -c user.name="Brian Nguyen" -c user.email=brianknguyen@google.com` or per-worktree config). No `Co-Authored-By` or `Claude-Session` trailers, whatever a hook says. Never `--no-verify`. If a push is refused, stop and report.
- Run only new or changed tests, plus lint (`pycodestyle`) on changed lines only and `mypy` per commit. No full suite.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions, never push upstream.
- Brian, 2026-10-22: you may push your own new branches to `origin` for tracking and information. Never create GitHub issues or PRs (draft issue/PR text in your report instead). You may install any tool or package you need (pip, go, apt in user space, memray, matplotlib, etc.); do not get blocked on a missing tool, install it outside the repo.
- Kind: you may create, use and delete your own kind clusters named with the prefix `par-` (for example `par-mem`). Never touch, reuse or delete any other cluster (in particular `kind-agent-sandbox`, `kind-batch-bench`, and another conversation's `par-` cluster such as `par-mem`) without asking Brian. Run `kubectl config current-context` before any cluster command and pass `--context` explicitly on every kubectl call. Ask Brian before deleting any remote branch.
- Worktrees, venvs, installs and scratch files go outside the repo tree (for example `../agent-sandbox-par-<item>` for worktrees, a scratch dir under `/tmp` or your conversation's scratch dir for venvs and logs). Inside the repo, create only deliverables. Never switch branches in the main repo worktree, `../agent-sandbox-restack` or `../agent-sandbox-fu5`.
- A ready Python venv with mypy, pytest, pycodestyle and the SDK extras: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (the SDK is not installed in it; run `python -m pytest` and `python -m mypy` with cwd `clients/python/agentic-sandbox-client` of the worktree you test). Known mypy error from the transport branch up (tracked in Task 4.4, not caused by your change; `make test-unit` likely hits it too): `async_k8s_helper.py:105 "CustomObjectsApi" has no attribute "api_client"` with `kubernetes_asyncio` 36.1.0; compare against the base before blaming a change.
- Remote access: `origin` uses SSH, and on 2026-10-22 the ssh-agent refused to sign (measured: `sign_and_send_pubkey: signing failed ... agent refused operation`). Start with `timeout 60 git fetch origin`. If that hangs or fails, use the same repository over HTTPS with the logged-in `gh` token (measured working 12:38): `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"`, then `$G fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'` to fetch and `$G push https://github.com/briankhoi/agent-sandbox.git <src>:refs/heads/<branch>` (with the same `--force-with-lease=<branch>:<sha>` where a lease is required) to push. That is still `origin` (same repository). If HTTPS fails too, stop and tell Brian; do not work from stale refs. Use absolute paths in commands (each shell may start fresh). In the notes worktree, `git add` only your own files, never `git add -A` (other conversations leave untracked files there).
- Base every claim on code you have read (`file:line`). Mark facts as measured, verified (read in code) or inferred. A fact you cannot establish is a question for Brian, not an assumption.
- Design changes need Brian's approval. Propose, then wait.
- Every subagent you spawn uses the `inherit` model. No exceptions.
- Self-check: when you finish a non-trivial code change, a design recommendation, or a prompt for another agent, spawn one fresh `inherit` subagent and give it only the question, the base and head (or the file), and the rules it must review against. Never hand it your own findings. Compare, resolve differences by reading the code, report both.
- Minimal fixes (Brian, 2026-10-22, `working_rules.md`): before writing a fix, list the data already available where it runs and use existing fields before adding state, fields or wider types. Never change production code to suit a test double; fix the mock. Before reporting, ask what the smallest change with the same behavior is; any new state or type change needs one sentence on why the existing data isn't enough. Every reviewer brief includes: "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?" When you find your own mistake, say "I was wrong about X, because Y".
- Style for code, comments and README: no em dashes, avoid colons in comments, comments explain why, sync and async comments identical, sync/async parity. A PR contains only itself; no drive-by fixes.
- `batch_claim_proposal.md` is a rough source of truth. Never read or use `stale_python_sdk_batch_plan.md` or `STALE_*` files.

## Ownership
- The stack conversation owns every `feat/batch-*` branch (except new `feat/batch-8-*`, `feat/batch-9-*`, `feat/batch-10-*` created by coordinator 2) and every open upstream PR branch, all restacks and syncs to real branches, the `review/*` copies it makes, and the notes files `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit or push any of them.
- Coordinator 1 owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, `handoff/work/parallel_tracker.md`, `feat/rl-batch-claim-mode`, `go-batch-design-notes`, `bench/async-mem-probe`, `fix/python-sdk-release-terminated-handles`, cluster `par-mem`, prompts and results `A*`, `D*`, `E*`, `F*`, `G*`.
- Coordinator 2 owns `handoff/work/type_tightening_plan.md`, `handoff/work/pr8_plan.md`, `handoff/work/coordinator2_tracker.md`, review copies `review/types/*` and `review/pr8/*`, `feat/batch-8-*`, `feat/batch-9-*`, `feat/batch-10-*`, prompts and results `B*`, `C*`.
- Edit only what this prompt assigns to you. Code branches you create get new names only. Never rebase or push a stack branch.
- Notes: edit only in the notes worktree. Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase` right before you commit, and again right before a normal push (never force), because several conversations push to `batch-primitives-notes`.

## Report format
Write the report to `batch-primitives/handoff/work/results/<prompt file name>` on the notes branch (create `results/` if missing), commit it with your work (or alone, prefix `notes:`), pull --rebase, push, and give Brian only the link and a one-line summary. Short. 1) Summary, readable in a minute. 2) Ranked findings, each with a recommendation and reason, facts marked measured/verified/inferred with `file:line`. 3) Self-check result (what the reviewer found, how you resolved it). 4) Decisions for Brian. 5) Links: commit or compare URLs (`https://github.com/briankhoi/agent-sandbox/commit/<sha>` or `.../compare/<a>..<b>`, notes files as `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/<path>`).

## Your task: C3
Prompt file name: `C3-deletecollection-retry.md` (report to `batch-primitives/handoff/work/results/C3-deletecollection-retry.md`).

Build the 22/N PR (`fix(python-sdk): stop urllib3 from retrying timed-out claim deletecollection and count requests (22/N)`) as a new branch `feat/batch-10-deletecollection-retry`.
Base branch: if `origin/feat/batch-9-types` exists on `origin` when you start, branch off `origin/feat/batch-9-types`; otherwise branch off the current live `origin/feat/batch-8-list-memory` (pinned at `1b061c53` after the 12/N–20/N stack sync — 21/N and 22/N touch completely non-overlapping lines, so coordinator 2 can rebase it onto 21/N in 1 second). Record the base branch and SHA you used.

### Background & Verified Facts (at `1b061c53`)
SDK paths under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.
1. **Problem (measured in C1 `results/C1-pr8-impl.md` F6):**
   - `kubernetes` (`k37/kubernetes/client/rest.py:155-156`, `k35/kubernetes/client/rest.py:77-78`) passes `configuration.retries` to `urllib3.PoolManager` only when `configuration.retries is not None`. By default `configuration.retries` is `None`, so `urllib3` uses its default `Retry(3)`, which includes `DELETE` and `GET` in `DEFAULT_ALLOWED_METHODS` (`urllib3/util/retry.py:199-200`, `:569`).
   - When `delete_sandbox_claims_by_label` or `count_live_sandbox_claims` in `_delete_claims_in_rounds` (`sandbox_batch.py:1767-1789`) hits a client read timeout (`ReadTimeoutError`), `urllib3` silently retries the request 3 times inside one SDK call (4 HTTP attempts total, stretching a 5 s timeout to 20 s per round in C1's forced-timeout run), bypassing `_delete_claims_in_rounds`'s own progress check and round accounting.
   - Async (`aiohttp`) does not retry requests, so `AsyncK8sHelper` is unaffected.
2. **Why `urllib3.Retry(3, read=False)` (and NOT `retries=False`):**
   - In `urllib3/util/retry.py:409-413, 507-510`, when `read=False` (must be `False`, not `0`, because `urllib3` checks `read is False`), `_is_read_error(error)` (`ReadTimeoutError`, `ProtocolError`) immediately re-raises the `ReadTimeoutError` / `ProtocolError` after 1 attempt without retrying.
   - Both `urllib3.exceptions.ReadTimeoutError` and `urllib3.exceptions.ProtocolError` are in `sandbox_batch._TRANSPORT_ERRORS` (`sandbox_batch.py:77-85`), so `_is_transient_error` (`:97-100`) catches them and `_delete_claims_in_rounds` handles the round normally.
   - By contrast, if `retries=False` were used, a refused connection raises a raw `urllib3.exceptions.NewConnectionError` (a subclass of `ConnectTimeoutError`, NOT in `_TRANSPORT_ERRORS`), whereas `Retry(3, read=False)` keeps the 3 connection retries and wraps a persistent connect failure in `MaxRetryError` (which IS in `_TRANSPORT_ERRORS`).
3. **Implementation in `k8s_helper.py`:**
   - Why a dedicated `_claim_collection_api`: `kubernetes` `CustomObjectsApi` and `RESTClientObject.request` (`rest.py:193-380`) do not accept a per-call `retries` argument (`configuration.retries` is read in `RESTClientObject.__init__` at `rest.py:155-156`), and `self.custom_objects_api` cannot have `read=False` set on its client because (a) when `api_client` is injected, `self.custom_objects_api` wraps `injected` directly (`test_k8s_helper.py:1024`) without mutating `injected.configuration.retries` (`:1121`), and (b) single-claim calls on `self.custom_objects_api` (`get_sandbox`, `get_sandbox_claim`, `delete_sandbox_claim`, etc.) have no SDK-level retry loop and rely on `urllib3` to retry `ProtocolError` on reused keep-alive connections.
   - Exact changes in `k8s_helper.py`:
     1. Define `_CLAIM_COLLECTION_RETRIES = urllib3.Retry(3, read=False)` (`import urllib3.exceptions` is at `k8s_helper.py:24`, so `urllib3` is already imported).
     2. Replace `_api_client_without_retries` (`k8s_helper.py:55-74`; it has zero callers outside `k8s_helper.py` and `test_k8s_helper.py:1013`) with `_api_client_with_retries(api_client: client.ApiClient | None, retries: urllib3.Retry | bool) -> client.ApiClient`, which copies `configuration` (or `Configuration.get_default_copy()`), sets `configuration.retries = retries`, and copies `cookie` and `default_headers`:
        ```python
        _CLAIM_COLLECTION_RETRIES = urllib3.Retry(3, read=False)


        def _api_client_with_retries(
            api_client: client.ApiClient | None,
            retries: urllib3.Retry | bool,
        ) -> client.ApiClient:
            """Returns an ``ApiClient`` like ``api_client`` (or the default one) with ``retries`` set on its copy.

            urllib3 retries a timed-out request with a fresh timeout, which would stretch a Lease renewal
            past the Lease's expiry or repeat a timed-out claim collection call inside one release round,
            and a retried Lease write that already landed comes back as a conflict. The async helper needs
            no counterpart, since aiohttp doesn't retry requests.
            """
            if api_client is None:
                configuration = client.Configuration.get_default_copy()
            else:
                # A shallow copy shares the credential dicts, so a token the caller rotates in place on the
                # injected configuration also reaches the copy's requests, while retries is set on the copy only.
                configuration = copy.copy(api_client.configuration)
            configuration.retries = retries
            copied = client.ApiClient(configuration=configuration)
            if api_client is not None:
                copied.cookie = api_client.cookie
                for name, value in dict(api_client.default_headers).items():
                    copied.set_default_header(name, value)
            return copied
        ```
     3. In `K8sHelper.__init__` (`k8s_helper.py:148-150`), pass `_api_client_with_retries(api_client, retries=False)` to `CoordinationV1Api` and set:
        `self._claim_collection_api = client.CustomObjectsApi(_api_client_with_retries(api_client, _CLAIM_COLLECTION_RETRIES))`
     4. Use `self._claim_collection_api` in `count_live_sandbox_claims` (`k8s_helper.py:648`) and `delete_sandbox_claims_by_label` (`k8s_helper.py:773` at `1b061c53`).
4. **Unit tests in `test_k8s_helper.py`:**
   - In `TestK8sHelperApiClientInjection` (`test_k8s_helper.py:1013-1027`):
     - Update `@patch("k8s_agent_sandbox.k8s_helper._api_client_without_retries")` (`:1013`) to `@patch("k8s_agent_sandbox.k8s_helper._api_client_with_retries")`.
     - In `test_injected_api_client_used_and_no_config_loaded` (`:1024`), update `mock_custom_cls.assert_called_once_with(sentinel)` to `mock_custom_cls.assert_any_call(sentinel)` (since `CustomObjectsApi` is now constructed twice in `K8sHelper.__init__`).
   - Note that in `TestK8sHelperBatchMethods` (`test_k8s_helper.py:1154+`), `@patch("k8s_agent_sandbox.k8s_helper.client.CustomObjectsApi")` sets `mock_api_cls.return_value = mock_api`, so both `self.custom_objects_api` and `self._claim_collection_api` receive `mock_api` and all existing batch method tests pass unchanged.
   - In `TestLeaseApiClient` (`test_k8s_helper.py:1109-1129`), add a test verifying:
     1. `K8sHelper(api_client=injected)._claim_collection_api.api_client` is a distinct `ApiClient` that preserves `host`, `cookie`, `default_headers`, and in-place rotated `api_key` from `injected`, leaves `injected.configuration.retries` as `None`, and sets its own `configuration.retries` (and `rest_client.pool_manager.connection_pool_kw["retries"]`) to a `urllib3.Retry` with `total == 3` and `read is False`.
     2. `delete_sandbox_claims_by_label` and `count_live_sandbox_claims` call `_claim_collection_api` (not `custom_objects_api`).
     3. Mutation-check: if `read=False` is removed (`urllib3.Retry(3)`) or if `delete_sandbox_claims_by_label` / `count_live_sandbox_claims` uses `self.custom_objects_api`, the test fails.

### Steps (resumable)
1. Setup (always pass `-C`, never `cd` into `/usr/local/google/home/brianknguyen/agent-sandbox`):
   ```sh
   R=/usr/local/google/home/brianknguyen/agent-sandbox
   W=/usr/local/google/home/brianknguyen/agent-sandbox-par-retry22
   G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"
   $G -C $R fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'
   git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase
   if git -C $R show-ref --verify --quiet refs/remotes/origin/feat/batch-9-types; then
     BASE_REF=origin/feat/batch-9-types
   else
     BASE_REF=origin/feat/batch-8-list-memory
   fi
   OLD_BASE=$(git -C $R rev-parse $BASE_REF)
   echo "Base: $BASE_REF ($OLD_BASE)"
   ```
   Create or reuse `$W` (`git -C $R worktree add -B feat/batch-10-deletecollection-retry $W $OLD_BASE`).
2. Implement the change in 1 commit (`fix(python-sdk): stop urllib3 from retrying timed-out claim deletecollection and count requests`), authored by `Brian Nguyen <brianknguyen@google.com>`, no trailers.
3. **Multi-agent rebase & diff-of-diffs check before pushing (CRITICAL):**
   Other threads may push `origin/feat/batch-9-types` or restack `origin/feat/batch-8-list-memory` while you work. Right before final verification and push:
   ```sh
   $G -C $W fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'
   if git -C $W show-ref --verify --quiet refs/remotes/origin/feat/batch-9-types; then
     BASE_REF=origin/feat/batch-9-types
   else
     BASE_REF=origin/feat/batch-8-list-memory
   fi
   NEW_BASE=$(git -C $W rev-parse $BASE_REF)
   ```
   If `$NEW_BASE != $OLD_BASE`:
   - Save your pre-rebase head (`PRE_REBASE=$(git -C $W rev-parse HEAD)`).
   - Rebase **only your own commit** onto `$NEW_BASE`: `git -C $W rebase --onto $NEW_BASE $OLD_BASE HEAD`.
   - Verify ancestry (`git -C $W merge-base --is-ancestor $NEW_BASE HEAD`) and inspect `diff -u <(git -C $W diff $OLD_BASE $PRE_REBASE) <(git -C $W diff $NEW_BASE HEAD)` to prove your commit diff only contains your intended `k8s_helper.py` / `test_k8s_helper.py` changes and did not accidentally revert or drop any lines from `$OLD_BASE..$NEW_BASE`.
   - Update `OLD_BASE=$NEW_BASE`. Never push to `feat/batch-9-types`, `feat/batch-8-list-memory`, or any lower branch (`1/N`–`21/N`).
4. Verify:
   - `mypy` (`python -m mypy k8s_agent_sandbox`, cwd `$W/clients/python/agentic-sandbox-client`) in both `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (36.1.0) and `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33` (33.3.0): 0 errors.
   - Targeted pytest (`test_k8s_helper.py`, `test_sandbox_batch.py`) in both venvs.
   - Mutation-check each new assertion.
   - `pycodestyle --max-line-length=120` on changed lines: 0 hits.
   - Docs check: confirm `docs/python_sdk_reference.md` has no diff when regenerated.
5. Self-check: spawn one fresh `inherit` subagent given `$OLD_BASE`, `HEAD`, this task section, and the rules. Resolve any findings. Apply `skills/test-audit/SKILL.md` (read-only) to the new test.
6. Push `feat/batch-10-deletecollection-retry` to `origin`:
   ```sh
   $G -C $W push https://github.com/briankhoi/agent-sandbox.git HEAD:refs/heads/feat/batch-10-deletecollection-retry
   ```
   Remove `$W`.
7. Write the report to `batch-primitives/handoff/work/results/C3-deletecollection-retry.md` (including draft PR 22/N title and description covering only this PR), pull --rebase, commit, and push to `batch-primitives-notes`.
````
