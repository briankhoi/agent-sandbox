# C2: PR 8 fixes (drop commits 4 and 5, fold `del page` and docstring into commits 1 and 2)

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
- The stack conversation owns every `feat/batch-*` branch (except a new `feat/batch-8-*` created by coordinator 2) and every open upstream PR branch, all restacks and syncs to real branches, the `review/*` copies it makes, and the notes files `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit or push any of them.
- Coordinator 1 owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, `handoff/work/parallel_tracker.md`, `feat/rl-batch-claim-mode`, `go-batch-design-notes`, `bench/async-mem-probe`, `fix/python-sdk-release-terminated-handles`, cluster `par-mem`, prompts and results `A*`, `D*`, `E*`, `F*`, `G*`.
- Coordinator 2 owns `handoff/work/type_tightening_plan.md`, `handoff/work/pr8_plan.md`, `handoff/work/coordinator2_tracker.md`, review copies `review/types/*` and `review/pr8/*`, `feat/batch-8-*`, prompts and results `B*`, `C*`.
- Edit only what this prompt assigns to you. Code branches you create get new names only. Never rebase or push a stack branch.
- Notes: edit only in the notes worktree. Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase` right before you commit, and again right before a normal push (never force), because several conversations push to `batch-primitives-notes`.

## Report format
Write the report to `batch-primitives/handoff/work/results/<prompt file name>` on the notes branch (create `results/` if missing), commit it with your work (or alone, prefix `notes:`), pull --rebase, push, and give Brian only the link and a one-line summary. Short. 1) Summary, readable in a minute. 2) Ranked findings, each with a recommendation and reason, facts marked measured/verified/inferred with `file:line`. 3) Self-check result (what the reviewer found, how you resolved it). 4) Decisions for Brian. 5) Links: commit or compare URLs (`https://github.com/briankhoi/agent-sandbox/commit/<sha>` or `.../compare/<a>..<b>`, notes files as `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/<path>`).

## Your task: C2
Prompt file name: `C2-pr8-fixes.md` (report to `batch-primitives/handoff/work/results/C2-pr8-fixes.md`).

Finalize `feat/batch-8-list-memory` after Brian's decisions on C1 (`batch-primitives/handoff/work/results/C1-pr8-impl.md`) and the verifier's small fixes. No kind cluster or benchmark reruns are needed.

### Brian's decisions (verbatim, 2026-10-28 00:05)
- "i see. i approve of your recommendations for 1, 3, 4, which means we drop 2"
  - Decision 1 (optional commits): **keep commit 6** (`e5367671`, drop `managedFields` from listed claims); **drop commits 4 and 5** (`30e6fb20` metadata-only count, `d8ec0178` metadata-only deletecollection).
  - Decision 2 (sync metadata route): dropped with commits 4 and 5.
  - Decision 3 (urllib3 retries on sync deletecollection): separate small PR (out of PR 8).
  - Decision 4 (`del page`, two pages held at once): **fold `del page` into commits 1 and 2** in both shells.

### Pinned state (verified 2026-10-28 00:10)
- Base: PR 7 `91d5ffc4` (`origin/feat/batch-7a-create-path`). If live `origin/feat/batch-7a-create-path` has moved from `91d5ffc4`, still build on `91d5ffc4` and note the new live PR 7 head in the report (do not restack onto a moving PR 7 unless asked).
- Current `origin/feat/batch-8-list-memory` head: `e5367671` (6 commits on `91d5ffc4`):
  1. `76d718e0` `perf(python-sdk): page the claim list`
  2. `765e159f` `perf(python-sdk): count live claims page by page in release rounds`
  3. `fb2a7e52` `perf(python-sdk): stop parsing the deletecollection response`
  4. `30e6fb20` `perf(python-sdk): count live claims from metadata-only pages` (DROP)
  5. `d8ec0178` `perf(python-sdk): ask for metadata only in the deletecollection response` (DROP)
  6. `e5367671` `perf(python-sdk): drop managedFields from listed claims` (KEEP as commit 4)

### Changes to make on `feat/batch-8-list-memory` (resulting in 4 commits on `91d5ffc4`)
SDK paths under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.

1. **In commit 1 (`76d718e0`, `perf(python-sdk): page the claim list`):**
   - Keep `_claim_list_pages`'s signature unchanged.
   - **Free the previous page in `_claim_list_pages` before fetching the next page (`del page`):**
     In both `k8s_helper.py` (`_claim_list_pages`, `:77-116` at `76d718e0`, right after `continue_token = ...` at `:113`) and `async_k8s_helper.py` (`_claim_list_pages`, `:55-94` at `76d718e0`, right after `continue_token = ...` at `:91`):
     ```python
     continue_token = (page.get("metadata") or {}).get("continue") or None
     del page
     if continue_token is None:
         return
     ```
     Why: while the generator awaits/fetches page `N+1`, its frame still holds `page` from iteration `N` unless deleted.
   - **Document per-page `_request_timeout` in `list_sandbox_claim_objects` docstring (both shells):**
     Matches `count_live_sandbox_claims` (added in commit 2 at `k8s_helper.py:634-636`, `async_k8s_helper.py:662-663` at `765e159f`).
     - In `k8s_helper.py` `list_sandbox_claim_objects` (`:604-612` at `76d718e0`), add:
       ```python
               Args:
                   _request_timeout: Optional timeout (seconds, or a ``(connect, read)``
                       pair) forwarded to the underlying urllib3 request of each page.
       ```
     - In `async_k8s_helper.py` `list_sandbox_claim_objects` (`:637-642` at `76d718e0`), add:
       ```python
               Args:
                   _request_timeout: Optional total timeout in seconds for each page, forwarded to kubernetes_asyncio.
       ```

2. **In commit 2 (`765e159f`, `perf(python-sdk): count live claims page by page in release rounds`):**
   - **Free `page` at the end of the loop body in `count_live_sandbox_claims` (`del page`, both shells):**
     In `k8s_helper.py` (`count_live_sandbox_claims`, `:625-644` at `765e159f`, shifted +4 lines by commit 1's docstring) and `async_k8s_helper.py` (`count_live_sandbox_claims`, `:656-674` at `765e159f`, shifted +3 lines by commit 1's docstring):
     ```python
             for page, first in _claim_list_pages(
                 self.custom_objects_api.list_namespaced_custom_object, namespace, label_selector, _request_timeout
             ):
                 if first:
                     live = 0
                 live += sum(1 for c in page.get("items", []) if not (c.get("metadata") or {}).get("deletionTimestamp"))
                 del page
     ```
     (and the `async for` twin in `async_k8s_helper.py`).
     Why: in Python, the `for` loop target variable `page` remains bound in the function frame while the generator advances to fetch page `N+1`, keeping page `N` alive alongside `_claim_list_pages` unless deleted before the next iteration.
   - **Unit test for single-page retention (in both `test_k8s_helper.py` and `test_async_k8s_helper.py`):**
     Update `test_count_live_sandbox_claims_counts_claims_not_being_deleted_across_pages` in both files (`import weakref` at top of both test files) so `side_effect` is a function that pops from a list of `_Page(dict)` objects and asserts `self.assertIsNone(prev_page_ref())` when each subsequent page is requested (do NOT pass a list `side_effect=[...]`, because CPython's `list_iterator` retains every list element until the iterator is exhausted, and asserting after `count_live_sandbox_claims` returns would pass even without `del page`):
     ```python
     class _Page(dict):
         pass

     pages: list[Any] = [
         # A restart after an expired continue token drops the earlier pages from the count.
         _Page({"items": [live, live], "metadata": {"continue": "t0"}}),
         client.ApiException(status=410),
         _Page({"items": [live, deleting], "metadata": {"continue": "t1"}}),
         _Page({"items": [deleting, live, live], "metadata": {}}),
     ]
     prev_page_ref: weakref.ref[_Page] | None = None

     def next_page(**_):
         nonlocal prev_page_ref
         if prev_page_ref is not None:
             self.assertIsNone(prev_page_ref())
         item = pages.pop(0)
         if isinstance(item, Exception):
             raise item
         prev_page_ref = weakref.ref(item)
         return item
     ```
     Set `mock_api.list_namespaced_custom_object.side_effect = next_page` (sync) and `self.helper.custom_objects_api.list_namespaced_custom_object = AsyncMock(side_effect=next_page)` (async).

3. **Keep commit 3 (`fb2a7e52`, `perf(python-sdk): stop parsing the deletecollection response`) unchanged** (rebased onto the updated commit 2).

4. **Drop commits 4 (`30e6fb20`) and 5 (`d8ec0178`).**

5. **Cherry-pick commit 6 (`e5367671`, `perf(python-sdk): drop managedFields from listed claims`) as commit 4** on top of commit 3. Resolve the small context conflict in `test_k8s_helper.py` and `test_async_k8s_helper.py` (where commit 6 was added adjacent to commit 4/5 tests).

6. **Minor report cleanup in `batch-primitives/handoff/work/results/C1-pr8-impl.md`:**
   - In section 1 bullet 6 (`C1-pr8-impl.md:14`) and F6 (`:95-96`), reword the "one 90 s timeout can stretch to about 6 minutes" / "about 360 s" claim so it is accurate: at the default 90 s client timeout, the apiserver's own 60 s request timeout returns a `504` first (which urllib3 does not retry); the 4x stretch happens when a client read timeout fires before the server responds (as in the 5 s forced-timeout run, 5 s -> 20 s) or on `429`/`503` with `Retry-After`.
   - In F5 table (`:67`), mark the 3-claim wire-byte numbers (`9,492 B` to `8,031 B`) as measured ad-hoc via `kubectl proxy` + `curl` (not from a committed JSON file, whereas the 5,000-claim `0.844` ratio is in the JSON files).

### Steps (resumable)
1. Setup (always pass `-C`, never `cd` into the main repo worktree `/usr/local/google/home/brianknguyen/agent-sandbox`):
   ```sh
   R=/usr/local/google/home/brianknguyen/agent-sandbox
   W=/usr/local/google/home/brianknguyen/agent-sandbox-par-pr8
   G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"
   $G -C $R fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'
   git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase
   git -C $R rev-parse origin/feat/batch-8-list-memory
   ```
   If `origin/feat/batch-8-list-memory` is still `e5367671`, proceed. Create or reuse `$W` (`git -C $R worktree add --detach $W 91d5ffc4` if missing, then `git -C $W checkout -B feat/batch-8-list-memory e5367671`). Always run commands with `-C $W` (or `cd $W`).
2. Build the 4-commit branch on `91d5ffc4`:
   - Edit commit 1 (`76d718e0`) to add `del page` in `_claim_list_pages` (both shells) and the `Args: _request_timeout:` docstring block in `list_sandbox_claim_objects` (both shells).
   - Edit commit 2 (`765e159f`) to add `del page` in `count_live_sandbox_claims` (both shells) and the `_Page(dict)` weakref check in `test_count_live_sandbox_claims_counts_claims_not_being_deleted_across_pages` (both shells) verifying that earlier pages are freed before the next page is fetched.
   - Rebase commit 3 (`fb2a7e52`) on top.
   - Cherry-pick commit 6 (`e5367671`) on top as commit 4 (dropping `30e6fb20` and `d8ec0178`).
3. Verify every commit (all 4 commits in `91d5ffc4..HEAD`):
   - Author & trailers: `git -C $W log --format='%h %an <%ae>%n%B' 91d5ffc4..HEAD` — every commit `Brian Nguyen <brianknguyen@google.com>`, no trailers.
   - On each of the 4 commits:
     - `mypy` (`python -m mypy k8s_agent_sandbox`, cwd `$W/clients/python/agentic-sandbox-client`) in both `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (36.1.0) and `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33` (33.3.0).
     - Targeted tests (`test_k8s_helper.py`, `test_async_k8s_helper.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`) in both venvs.
   - Mutation-check the new `del page` weakref assertion in both shells: comment out `del page` in `_claim_list_pages` -> test fails; restore; comment out `del page` in `count_live_sandbox_claims` -> test fails; restore.
   - `pycodestyle --max-line-length=120` on all changed lines across `91d5ffc4..HEAD`.
   - Docs check: confirm `docs/python_sdk_reference.md` has no diff when regenerated.
   - Compare against `e5367671`: `git -C $W diff e5367671 HEAD` should show only the removal of commits 4 and 5, the 4 `del page` lines, the 2 docstring `Args:` blocks, and the weakref check in the 2 count tests.
4. Self-check: spawn one fresh `inherit` subagent given `91d5ffc4`, the new `HEAD`, this task section, and the rules. Resolve any findings.
5. Push `feat/batch-8-list-memory` with lease on `e5367671`, plus a synthetic `review-diff/pr8-c2` branch (parent `e5367671`, tree `HEAD^{tree}`) so the C1-to-C2 delta is 1-click viewable on GitHub:
   ```sh
   $G -C $W push --force-with-lease=feat/batch-8-list-memory:e5367671 https://github.com/briankhoi/agent-sandbox.git HEAD:refs/heads/feat/batch-8-list-memory
   D=$(GIT_AUTHOR_NAME="Brian Nguyen" GIT_AUTHOR_EMAIL=brianknguyen@google.com GIT_COMMITTER_NAME="Brian Nguyen" GIT_COMMITTER_EMAIL=brianknguyen@google.com git -C $W commit-tree HEAD^{tree} -p e5367671 -m "review-diff: PR 8 C2 fixes (e5367671 -> $(git -C $W rev-parse --short HEAD))")
   $G -C $W push https://github.com/briankhoi/agent-sandbox.git $D:refs/heads/review-diff/pr8-c2
   ```
6. Update `batch-primitives/handoff/work/results/C1-pr8-impl.md` (the two wording fixes in item 6 above) and write `batch-primitives/handoff/work/results/C2-pr8-fixes.md` in the notes worktree, pull --rebase, commit, and push to `batch-primitives-notes`.
````
