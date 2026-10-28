# B5: 21/N Types PR (`feat/batch-9-types`)

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

## Your task: B5
Prompt file name: `B5-types-pr.md` (report to `batch-primitives/handoff/work/results/B5-types-pr.md`).

Build the 21/N Types PR (`refactor(python-sdk): replace Any with exact helper and Lease types in batch modules (21/N)`) as a new branch `feat/batch-9-types` off `origin/feat/batch-8-list-memory` (`219985c0`).

### Brian's decisions (verbatim, 2026-10-23 to 10-28)
- Separate types PR after PR 8: "and then for b, the stuff that needs types after PR 7 mainly reaper we just fold into their respective PRs right? if so, i approve B" and "for the separate types PR, i support your recommendation".
- Lease `metadata`/`spec` narrowing (`lease_parts(lease)` raising plain `BatchError`) and `spec.holder_identity = cast(Any, None)` in `async_sandbox_batch.py` (`detach()`): "approve both".

### Pinned state (verified at `origin/feat/batch-8-list-memory` `219985c0`)
SDK paths under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.
1. Already folded into 17/N (transport) and 18/N (reaper) in B2/B3 (present in `219985c0`):
   - `async_k8s_helper.py:149`: `cast(client.ApiClient, self._api_client)` in `api_pool_size()`.
   - `batch_utils.py:177-199`: `lease_owner_reference(lease: "V1Lease | AsyncV1Lease") -> dict[str, str]` raising `BatchError`.
   - `k8s_helper.py:167` and `async_k8s_helper.py:162`: `owner_references: list[dict[str, str]] | None = None`.
   - `batch_reaper.py`: 0 `Any` remain (`_continue=` passed directly).
2. What remains for 21/N (18 `Any` annotations across 4 files, plus `lease_parts` in `batch_utils.py` and its call sites; all other `Any` in `batch_state.py`, `batch_utils.py`, `k8s_helper.py`, `async_k8s_helper.py`, `models.py`, and `SandboxClient[Any]` are true JSON/pydantic/generic boundaries and stay `Any`):
   - **Typed `helper` parameters in `sandbox_batch.py` and `async_sandbox_batch.py` (6 annotations):**
     - `sandbox_batch.py`: add `from .k8s_helper import K8sHelper` under `if TYPE_CHECKING:` (`:57-59`). Change `helper: Any` to `helper: "K8sHelper"` in:
       - `_precheck_pools` (`:1497`)
       - `_delete_batch_objects` (`:1530`; called from `sandbox_client.py` and from `async_sandbox_client.py:812` `_atexit_cleanup` with a sync `K8sHelper`)
       - `_delete_claims_in_rounds` (`:1553`)
     - `async_sandbox_batch.py`: add `from .async_k8s_helper import AsyncK8sHelper` under `if TYPE_CHECKING:` (`:62-64`). Change `helper: Any` to `helper: "AsyncK8sHelper"` in:
       - `_precheck_pools` (`:1522`)
       - `_delete_batch_objects` (`:1555`)
       - `_delete_claims_in_rounds` (`:1576`)
   - **Typed `V1Lease` methods and `lease_parts(lease)` (12 annotations + narrowing):**
     - `k8s_helper.py`:
       - `read_batch_lease` (`:686-688`): return `-> client.V1Lease | None:` (was `-> Any:`)
       - `replace_batch_lease` (`:704-706`): `body: client.V1Lease`, return `-> client.V1Lease:` (was `body: Any`, `-> Any:`; wrap the signature across lines to stay `<= 120` chars for `pycodestyle`)
       - `create_batch_lease` (`:719-721`): `body: client.V1Lease`, return `-> client.V1Lease:` (was `body: Any`, `-> Any:`)
     - `async_k8s_helper.py`:
       - `read_batch_lease` (`:719`): return `-> client.V1Lease | None:` (was `-> Any:`)
       - `replace_batch_lease` (`:736-738`): `body: client.V1Lease`, return `-> client.V1Lease:` (was `body: Any`, `-> Any:`)
       - `create_batch_lease` (`:752`): `body: client.V1Lease`, return `-> client.V1Lease:` (was `body: Any`, `-> Any:`)
     - `sandbox_batch.py`:
       - `_write_renewal` (`:1316`): `def _write_renewal(self, lease: V1Lease) -> tuple[float, V1Lease]:` (was `lease: Any`)
     - `async_sandbox_batch.py`:
       - `_write_renewal` (`:1326`): `async def _write_renewal(self, lease: V1Lease) -> tuple[float, V1Lease]:` (was `lease: Any`)
     - `batch_utils.py`:
       - Import `overload` from `typing` (`:32`), and under `if TYPE_CHECKING:` (`:48-51`) import `V1Lease, V1LeaseSpec, V1ObjectMeta` from `kubernetes.client` and `V1Lease as AsyncV1Lease, V1LeaseSpec as AsyncV1LeaseSpec, V1ObjectMeta as AsyncV1ObjectMeta` from `kubernetes_asyncio.client`.
       - Right after `lease_owner_reference` (`:177-199`), add `lease_parts`:
         ```python
         @overload
         def lease_parts(lease: "V1Lease") -> "tuple[V1ObjectMeta, V1LeaseSpec]": ...
         @overload
         def lease_parts(lease: "AsyncV1Lease") -> "tuple[AsyncV1ObjectMeta, AsyncV1LeaseSpec]": ...
         def lease_parts(
             lease: "V1Lease | AsyncV1Lease",
         ) -> "tuple[V1ObjectMeta | AsyncV1ObjectMeta, V1LeaseSpec | AsyncV1LeaseSpec]":
             """Returns a batch Lease's ``(metadata, spec)``.

             Raises:
                 BatchError: If the Lease has no ``metadata`` or ``spec``.
             """
             if lease.metadata is None or lease.spec is None:
                 raise BatchError("batch Lease has no metadata or spec")
             return lease.metadata, lease.spec
         ```
     - Call sites of `lease_parts` in `sandbox_batch.py` and `async_sandbox_batch.py` (keep sync and async identical except `await` and the one `cast(Any, None)` in async `detach()`):
       1. `_attach` (`sandbox_batch.py:346-385`, `async_sandbox_batch.py:346-385`):
          - Unpack `meta, spec = batch_utils.lease_parts(lease) if lease is not None else (None, None)`.
          - Use `meta` and `spec` for `labels`/`annotations`, `lease_duration_seconds`, `renew_time`, `holder_identity`, `acquire_time`, and `lease_transitions`.
          - In the `BatchLeaseExpiredError` check (`:360-364`), add `or spec is None` right after `lease is None` (`if lease is None or spec is None or renew_time is None or spec_duration is None or ...:`) so `mypy` narrows both `lease` (for `replace_batch_lease` at `:386-388`) and `spec` (for `spec.holder_identity` at `:369,380`). When `lease is None`, `lease_parts` is skipped and `_attach` still raises `BatchLeaseExpiredError`.
       2. `detach` (`sandbox_batch.py:1086-1123`, `async_sandbox_batch.py:1106-1143`):
          - Under `if lease is not None:`, unpack `meta, spec = batch_utils.lease_parts(lease)` before checking `spec.holder_identity != self._holder_identity`.
          - Inside the `except ApiException` `409` handler after re-reading `lease`, unpack `meta, spec = batch_utils.lease_parts(lease)` again after `if lease is None: break`:
            ```python
            if lease is None:
                break
            meta, spec = batch_utils.lease_parts(lease)
            if spec.holder_identity != self._holder_identity:
                break
            ```
            so `attempt == 1` mutates the re-read `lease`'s `meta` and `spec`.
          - In `async_sandbox_batch.py` (`detach()`), add `cast` to `from typing import TYPE_CHECKING, Any, TypeVar, cast` (`:31`) and write:
            ```python
            # kubernetes_asyncio 36's stub types holder_identity as str, though None clears it.
            spec.holder_identity = cast(Any, None)
            ```
            In `sandbox_batch.py` (`detach()`), write `spec.holder_identity = None` with no cast and no stub comment (the comment in `async_sandbox_batch.py` explains the `kubernetes_asyncio` `cast(Any, None)` that only exists there, matching `async_k8s_helper.py:148` and `:812`).
       3. `_renew_once` (`sandbox_batch.py:1290-1297`, `async_sandbox_batch.py:1298-1305`):
          `spec = batch_utils.lease_parts(lease)[1]` before checking `spec.holder_identity != self._holder_identity`.
       4. `_write_renewal` (`sandbox_batch.py:1316-1328`, `async_sandbox_batch.py:1326-1338`):
          `spec = batch_utils.lease_parts(lease)[1]` before setting `spec.holder_identity`, `spec.renew_time`, `spec.lease_duration_seconds`.
     - Unit test in `test_batch_utils.py`:
       Update `from kubernetes.client import V1Lease, V1ObjectMeta` (`test_batch_utils.py:19`) to also import `V1LeaseSpec`, and add `TestLeaseParts` right after `TestLeaseOwnerReference` (`test_batch_utils.py:109-116`) testing:
       1. `lease_parts` returns `(lease.metadata, lease.spec)` when both are set.
       2. `lease_parts` raises `BatchError` when `metadata` is `None` or `spec` is `None` (mutation-check both conditions).

### Steps (resumable)
1. Setup (always pass `-C`, never `cd` into `/usr/local/google/home/brianknguyen/agent-sandbox`):
   ```sh
   R=/usr/local/google/home/brianknguyen/agent-sandbox
   W=/usr/local/google/home/brianknguyen/agent-sandbox-par-types21
   G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"
   $G -C $R fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'
   git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase
   git -C $R rev-parse origin/feat/batch-8-list-memory
   ```
   Confirm `origin/feat/batch-8-list-memory` is `219985c0`. Create or reuse `$W` (`git -C $R worktree add -B feat/batch-9-types $W 219985c0`).
2. Implement the changes on `feat/batch-9-types` (1 commit or 2 clean commits: e.g. commit 1 typed `helper` parameters in `sandbox_batch.py` / `async_sandbox_batch.py`, commit 2 typed `V1Lease` methods and `lease_parts` with `TestLeaseParts` — or 1 combined commit; 2 commits is very clean to review):
   - Author `Brian Nguyen <brianknguyen@google.com>`, no trailers.
3. Verify every commit in `219985c0..HEAD`:
   - `mypy` (`python -m mypy k8s_agent_sandbox`, cwd `$W/clients/python/agentic-sandbox-client`) in both `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (36.1.0) and `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33` (33.3.0): 0 errors.
   - Targeted pytest (`test_batch_utils.py`, `test_k8s_helper.py`, `test_async_k8s_helper.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`) in both venvs.
   - Mutation-check `TestLeaseParts` (remove `lease.metadata is None` check -> test fails; restore; remove `lease.spec is None` check -> test fails; restore).
   - `pycodestyle --max-line-length=120` on all changed lines: 0 hits.
   - Docs check: confirm `docs/python_sdk_reference.md` has no diff when regenerated.
4. Self-check: spawn one fresh `inherit` subagent given `219985c0`, `HEAD`, this task section, and the rules. Resolve any findings. Apply `skills/test-audit/SKILL.md` (read-only) to `TestLeaseParts`.
5. Push `feat/batch-9-types` to `origin`:
   ```sh
   $G -C $W push https://github.com/briankhoi/agent-sandbox.git HEAD:refs/heads/feat/batch-9-types
   ```
   Remove `$W`.
6. Write the report to `batch-primitives/handoff/work/results/B5-types-pr.md` (including draft PR 21/N title and description covering only this PR), pull --rebase, commit, and push to `batch-primitives-notes`.
````
