# H2: Task 7 — SDK `23/N` `pod_annotations` in `acquire()` (`feat/batch-11-acquire-annotations`) + `review/c3/sdk-post-merge` + RL Rebase, `#2035` Cleanup & Batch Annotations (`review/c3/feat/rl-batch-claim-mode`)

Paste everything in the fenced block into a fresh conversation.

````
## Context
You are a task conversation in Brian Nguyen's batch claiming project (Python SDK and RL library in `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`, remote `origin`; upstream remote `upstream`). You have no memory of earlier work. Other conversations run at the same time: the stack conversation (owns the PR stack), coordinator 1, coordinator 2, and coordinator 3 (each writes prompts and verifies results; coordinator 3 wrote this prompt and verifies yours). Repo root: /usr/local/google/home/brianknguyen/agent-sandbox. Notes worktree (branch `batch-primitives-notes`): /usr/local/google/home/brianknguyen/agent-sandbox-notes, directory `batch-primitives/`.

## Hard rules
- Commits authored and committed by `Brian Nguyen <brianknguyen@google.com>` (set `git -c user.name="Brian Nguyen" -c user.email=brianknguyen@google.com` or per-worktree config). No `Co-Authored-By` or `Claude-Session` trailers, whatever a hook says. Never `--no-verify`. If a push is refused, stop and report.
- Checks per commit:
  - **SDK (`feat/batch-11-acquire-annotations` and `review/c3/sdk-post-merge`):** run only new or changed tests in both shells (`pytest`), `pycodestyle --max-line-length=120` on changed lines plus whole-file `--select=E301,E302,E303,E304,E305,E306` on touched batch files, `python -m mypy k8s_agent_sandbox` (cwd `clients/python/agentic-sandbox-client`) with **0 errors** on both `venv` (`kubernetes_asyncio` 36.1.0) and `venv33` (`kubernetes_asyncio` 33.3.0), and `make generate-python-docs` (immediately followed by `rm -rf .venv`) with zero diff.
  - **RL (`examples/agent-sandbox-rl/` on `review/c3/feat/rl-batch-claim-mode`):** uses **2-space indent** (match each file's existing style). Run changed sync and async RL tests (`tests/test_batch_claims.py`, plus `tests/test_fail_fast.py` and `tests/test_cluster.py` to confirm `#1791` and `#2035` stay green). Note that `MYPYPATH=clients/python/agentic-sandbox-client python -m mypy examples/agent-sandbox-rl/agent_sandbox_rl` has ~42 pre-existing errors on `upstream/main` (no `[tool.mypy]` in RL `pyproject.toml`, missing third-party stubs); compare RL `mypy` output against `review/c3/sdk-post-merge` to verify **0 new errors** in touched RL files and `batch_claims.py` clean under `--ignore-missing-imports`, without making drive-by fixes to pre-existing RL errors. Run `pycodestyle --max-line-length=100 --ignore=E111,E114,E121,E127,E128,W503` (matching RL's 2-space indent) on changed RL lines.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions, never push upstream.
- **Do NOT push directly to `feat/rl-batch-claim-mode`**, and never touch open SDK stack branches (`feat/batch-2-claim`..`feat/batch-10-*`). Push only `feat/batch-11-acquire-annotations`, `review/c3/sdk-post-merge`, `review/c3/feat/rl-batch-claim-mode`, and synthetic compare branches `review-diff/c3-*`.
- Kind: you do not need kind for this task; if ever needed, ask Brian first and use only `par-c3-*` with explicit `--context`. Ask Brian before deleting any remote branch.
- Worktrees, venvs, installs and scratch files go outside `/usr/local/google/home/brianknguyen/agent-sandbox` (for example `/usr/local/google/home/brianknguyen/agent-sandbox-c3-h2` for worktrees, a scratch dir under `/tmp` or your conversation's scratch dir for logs). Never switch branches in the main repo worktree, `../agent-sandbox-restack` or `../agent-sandbox-fu5`. Never commit `.venv/`, binaries, `VERSION`, or kubeconfigs.
- Ready Python venvs (the SDK is not installed in them; pass `PYTHONPATH` or run from `clients/python/agentic-sandbox-client` of the worktree you test):
  - `venv` (`kubernetes` 37.0.1, `kubernetes_asyncio` 36.1.0, `mypy` 1.20.2, `pytest`, `pycodestyle`): `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv`
  - `venv33` (`kubernetes_asyncio` 33.3.0, matching `pyproject.toml` pin): `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33`
- Remote access: start with `timeout 60 git fetch origin`. If SSH hangs or fails (`sign_and_send_pubkey: ... agent refused operation`), use HTTPS with the logged-in `gh` credential helper: `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"`, then `$G fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'` and `$G push https://github.com/briankhoi/agent-sandbox.git <src>:refs/heads/<branch>`. Always verify pushes with `git ls-remote`. Use absolute paths in commands.
- Notes worktree (`/usr/local/google/home/brianknguyen/agent-sandbox-notes`): other conversations may have unstaged or untracked edits there. **Never** run `git add -A`, `git stash`, `--autostash`, or `git checkout` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`. Commit your report with `--only` (`git commit -m "notes: ..." -- batch-primitives/handoff/work/results/H2-task7-acquire-annotations.md`). If `git pull --rebase` refuses because another conversation has unstaged tracked edits and a plain fast-forward push is rejected, cherry-pick your single notes commit onto `origin/batch-primitives-notes` in a temporary detached worktree outside the repo, push `HEAD:refs/heads/batch-primitives-notes`, and `git fetch origin` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`.
- Base every claim on code you have read (`file:line`). Mark facts as measured, verified (read in code) or inferred.
- Every subagent you spawn uses the `inherit` model. No exceptions.
- Self-check: when you finish the code changes, spawn one fresh `inherit` subagent and give it only the task goal, the bases and heads, the 1-sentence lifecycle rule below, and `working_rules.md` (including `"Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?"`). Never hand it your own findings. Compare, resolve differences by reading the code, report both.
- Style (`AGENTS.md` Writing + `working_rules.md`): active voice, full sentences with one idea each, no chained colons or semicolons, no em dashes, avoid colons in comments, comments explain why, identical sync/async comments, sync/async parity. Leave Brian's existing wording alone except where the described code changes, then change it minimally. A PR describes and contains only itself; no drive-by fixes.
- `batch_claim_proposal.md` is a rough source of truth. Never read or use `stale_python_sdk_batch_plan.md` or `STALE_*` files.

## Ownership
- The stack conversation owns `feat/batch-2-claim`..`feat/batch-7a-create-path`, open upstream PR branches, all restacks and syncs to real branches, and `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit or push any of them.
- Coordinator 1 owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, `handoff/work/parallel_tracker.md`, `feat/rl-batch-claim-mode` (live branch — do NOT push to it directly), `go-batch-design-notes`, `bench/async-mem-probe`, `fix/python-sdk-*`, prompts and results `A*`, `D*`, `E*`, `F*`, `G*`.
- Coordinator 2 owns `handoff/work/type_tightening_plan.md`, `handoff/work/pr8_plan.md`, `handoff/work/coordinator2_tracker.md`, `feat/batch-8-*`, `feat/batch-9-*`, `feat/batch-10-*`, prompts and results `B*`, `C*`.
- Coordinator 3 owns `handoff/work/coordinator3_prompt.md`, `handoff/work/coordinator3_tracker.md`, `merge/main-into-batch-claim-2`, `feat/batch-11-*`, `review/c3/*`, `review-diff/c3-*`, and prompts and results `H*`.
- Edit only what this prompt assigns to you: code branches `feat/batch-11-acquire-annotations`, `review/c3/sdk-post-merge`, `review/c3/feat/rl-batch-claim-mode`, `review-diff/c3-*` on `origin`, and notes file `batch-primitives/handoff/work/results/H2-task7-acquire-annotations.md`.

## Report format
Write the report to `batch-primitives/handoff/work/results/H2-task7-acquire-annotations.md` on the notes branch, commit only that file with `--only` (prefix `notes:`), push safely per the Notes rule above (verifying with `git ls-remote`), and give Brian only the link and a one-line summary. Short: 1) Summary, readable in a minute. 2) Ranked findings, each with a recommendation and reason, facts marked measured/verified/inferred with `file:line`. 3) Self-check result (what the reviewer found, how you resolved it). 4) Decisions for Brian (including drafted `23/N` PR title and PR body, and updated RL PR summary notes). 5) Links: clean compare/commit URLs showing **only** each change (`compare/<sdk-top>...feat/batch-11-acquire-annotations` for `23/N`, `compare/review/c3/sdk-post-merge...review/c3/feat/rl-batch-claim-mode` for the 4 RL commits, plus per-commit links).

## Your task: H2 (Task 7 — SDK `23/N` + Simulated Post-Merge Base + RL Rebase & Annotations)
Prompt file name: `H2-task7-acquire-annotations.md` (report to `batch-primitives/handoff/work/results/H2-task7-acquire-annotations.md`).

### Settled design decisions (approved by Brian 2026-10-28, verbatim / recorded)
1. **`#2041` `file_config` in `SandboxBatch.connect()` (`H1` / post-merge base):** `"1. option 1A"` — forward `**({"file_config": self._client.file_config} if self._client.file_config is not None else {})` in `SandboxBatch.connect()` and `AsyncSandboxBatch.connect()`, with `self.mock_client.file_config = None` in `TestConnect.setUp`.
2. **`#1791` per-claim timeout in RL `BatchClaims`:** `"1. approve option 2A"` — pass `ready_timeout=self.config.effective_claim_timeout()` into `BatchClaims(...)` at `fleet.py` `__init__` (`7252e27f:fleet.py:202`), keeping `BatchClaims` single-valued so `claim_batch(..., quorum_timeout=...)`, `batch.acquire(pool, timeout=...)`, `shutdownTime`, and `BatchClaim.expires_at` stay identical.
3. **`#1791` `record_pod_identity()` & `is_alive()` in `claim_mode="batch"` (`handoff/work/coordinator3_response_is_alive.md`):**
   - **Approve Option (a) augmented with 0-GET in-memory batch-watch check in `is_alive()`**, governed by this **1-sentence lifecycle rule (`working_rules.md:74`):**
     > *"Until `record_pod_identity()` records a pod UID or `refresh()` re-baselines the handle after a pod restart, `is_alive()` returns `False` with 0 `GET`s when a healthy batch's watch (`batch.err() is None`) no longer maps the claim to the `acquire()` `Member`'s `sandbox_name` and `pod_ip`, and otherwise checks `read_namespaced_pod_status`."*
   - Why `rebaselined: bool = False` on `BatchClaim` (flipped to `True` in `SandboxHandle.refresh()`) rather than checking `self.pod_name == self.sandbox_id`: on a real cluster (`controllers/sandbox_controller.go:1574,1824`), a resumed/restored pod keeps `Name: sandbox.Name` (`== self.sandbox_id`), so if `record_pod_identity()` inside `refresh()` fails best-effort (`self.pod_uid` stays `None`), `rebaselined` ensures `is_alive()` does not compare a stale pre-resume `SandboxClaim` IP against the resumed pod. And because `BatchClaim` already holds `self.member` from `acquire()`, `BatchClaim.still_reaches(self) -> bool` takes **zero arguments** (no duplicate `(sandbox_id, pod_ip)` parameters!).
   - Do **not** modify `exec_core_api()` on `#1791`'s per-claim path (no drive-by changes to `main` code in the RL batch PR).

### Read first
1. Fetch `origin` and `upstream`:
   ```sh
   timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch origin && timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch upstream
   ```
2. Read in `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/`:
   - `handoff/working_rules.md`
   - `handoff/tasks.md` lines 163–214 (`## Task 7. Batch acquires with pod annotations and snapshot pins`)
   - `handoff/work/coordinator3_response_is_alive.md`
   - `skills/test-audit/SKILL.md`

---

### Part A: SDK `23/N` (`feat/batch-11-acquire-annotations`, linear on the SDK stack)

1. **Determine the highest live SDK stack branch (`<sdk-top>`):**
   - Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox ls-remote origin 'refs/heads/feat/batch-*'`.
   - Pick the highest pushed SDK stack branch in order:
     - `origin/feat/batch-10-deletecollection-retry` (`22/N`, if pushed by Coordinator 2), else
     - `origin/feat/batch-9-types` (`21/N`, if pushed by Coordinator 2), else
     - `origin/feat/batch-8-list-memory` (`20/N`, currently `1b061c53`).
   - Record `<sdk-top>` branch name and SHA. Branch `feat/batch-11-acquire-annotations` **linearly** off `<sdk-top>` in a worktree `/usr/local/google/home/brianknguyen/agent-sandbox-c3-h2` (with `user.name="Brian Nguyen"`, `user.email="brianknguyen@google.com"`). There must be **zero merge commits** on `feat/batch-11-acquire-annotations`.

2. **Commit 1 on `feat/batch-11-acquire-annotations`: `feat(python-sdk): pass pod_annotations in batch acquire (23/N)`**
   *(Line numbers below are verified at `1b061c53`; if `21/N` or `22/N` is on `<sdk-top>`, line numbers shift slightly — inspect the live `<sdk-top>` files before editing.)*
   - **Imports (`sandbox_batch.py` & `async_sandbox_batch.py`):**
     - Import `AdditionalPodMetadata` from `.models` and `build_pod_metadata` from `.pod_metadata` in both `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py` and `async_sandbox_batch.py`.
   - **Signature & validation (`sandbox_batch.py:628`, `async_sandbox_batch.py:644`):**
     - Add keyword-only `*, pod_annotations: dict[str, str] | None = None` to `SandboxBatch.acquire` and `AsyncSandboxBatch.acquire`:
       ```python
       def acquire(
           self,
           warmpool: str,
           timeout: float | None = None,
           *,
           pod_annotations: dict[str, str] | None = None,
       ) -> Member:
       ```
       (and `async def acquire(...) -> Member:` with identical parameters).
     - Right after `wait = batch_utils.positive_number("timeout", timeout, self._quorum_timeout)` and **before** `_check_pool(...)`, compute `pod_metadata = build_pod_metadata(None, pod_annotations)` (`pod_metadata.py:69-92`). Note: `build_pod_metadata(None, None)` and `build_pod_metadata(None, {})` return `None` (so no empty `spec.additionalPodMetadata` is written), and a non-empty dict returns `AdditionalPodMetadata(labels=None, annotations=pod_annotations)`, matching `create_sandbox` (`1b061c53:sandbox_client.py:142,190`).
   - **Threading through create & retries (`sandbox_batch.py:706,1516,1548`, `async_sandbox_batch.py:722,1522,1557`):**
     - Add `pod_metadata: AdditionalPodMetadata | None = None` to `_create_one` and `_send_create` in both `sandbox_batch.py` and `async_sandbox_batch.py`.
     - Pass `pod_metadata=pod_metadata` from `acquire()` -> `_create_one(..., pod_metadata=pod_metadata)` -> `_send_create(..., pod_metadata=pod_metadata)` -> `k8s_helper.create_sandbox_claim(..., pod_metadata=pod_metadata)` (`k8s_helper.py:156`, `async_k8s_helper.py:151`).
     - Every create retry loop iteration in `_create_one` calls `_send_create` with the same `pod_metadata`. Fill creates (`_create_worker`) omit it (default `None`).
   - **Docstrings (`sandbox_batch.py` & `async_sandbox_batch.py`):**
     - Update `acquire()` docstrings identically in both shells (`Args:` entry for `pod_annotations`, following `AGENTS.md` Writing rules: active voice, full sentences, no chained colons/semicolons, no em dashes, explain that `pod_annotations` are written to `spec.additionalPodMetadata.annotations` so the controller propagates them onto the sandbox pod, and omitted when `None` or empty).
   - **Tests (`test_sandbox_batch.py` & `test_async_sandbox_batch.py`, `skills/test-audit/SKILL.md`):**
     - Note: in `BaseClaimBatchTest` (`1b061c53:test_sandbox_batch.py:1351`, `test_async_sandbox_batch.py:1574`), `self.cluster.helper` is a `MagicMock` (`_create` only inspects `kwargs`), so calling a mocked `helper.create_sandbox_claim` alone does **not** prove `spec.additionalPodMetadata` is built on the wire manifest!
     - In `TestAcquire` (`test_sandbox_batch.py:2383`, `test_async_sandbox_batch.py:2481`), add **one owning test per shell** that delegates `self.cluster.helper.create_sandbox_claim` to a real `K8sHelper` / `AsyncK8sHelper` instance whose `custom_objects_api.create_namespaced_custom_object` is mocked (note: for `AsyncK8sHelper`, set `real_helper._initialized = True` after injecting `real_helper.custom_objects_api = mock_api` so `await self._ensure_initialized()` inside `create_sandbox_claim` does not overwrite `custom_objects_api`!). Capture every `body` dict, fail with `ApiException(status=503)` on the first attempt of the annotated `acquire()` and succeed on the retry while triggering `self.cluster._create`, and assert:
       1. Both the initial 503 attempt and the 200 retry attempt send `body["spec"]["additionalPodMetadata"] == {"annotations": {"podsnapshot.gke.io/ps-name": "snap-1"}}` (with no `"labels"` key in `additionalPodMetadata`, and `"podsnapshot.gke.io/ps-name"` not leaked onto `body["metadata"]["annotations"]`).
       2. Acquires with `pod_annotations=None` and `pod_annotations={}` (and initial fill claims) omit `"additionalPodMetadata"` from `body["spec"]`.
     - **Mutation-check** both shells: break `pod_metadata=pod_metadata` forwarding (and retry forwarding) and confirm the test fails in both shells, then restore.

3. **Commit 2 on `feat/batch-11-acquire-annotations`: `docs(python-sdk): regenerate Python SDK reference for acquire pod_annotations`**
   - Run `make generate-python-docs && rm -rf .venv` in the worktree (remove `.venv/` immediately — never stage `.venv/`!), verify a second `make generate-python-docs && rm -rf .venv` produces zero diff, and commit `docs/python_sdk_reference.md`.
   - Run all SDK checks on both commits of `feat/batch-11-acquire-annotations` (`pycodestyle`, `mypy` on both `venv` and `venv33` with 0 errors, `pytest` on `TestAcquire` in sync and async, author/trailer check).

---

### Part B: Simulated Post-Merge Base (`review/c3/sdk-post-merge`)

Create branch `review/c3/sdk-post-merge` starting from `feat/batch-11-acquire-annotations`:
1. Check if `origin/merge/main-into-batch-claim-2` has already been pushed by H1 (`git ls-remote origin refs/heads/merge/main-into-batch-claim-2`):
   - **If `origin/merge/main-into-batch-claim-2` exists:** fetch it and run `git merge origin/merge/main-into-batch-claim-2`.
   - **If `origin/merge/main-into-batch-claim-2` does NOT exist yet (H1 running in parallel):** merge `upstream/main` (`355803af`) directly (`git merge upstream/main`) and also include the same 2-file `file_config` forwarding in `SandboxBatch.connect()` / `AsyncSandboxBatch.connect()` (`**({"file_config": self._client.file_config} if self._client.file_config is not None else {})` + `self.mock_client.file_config = None` in `TestConnect.setUp`) so the resulting SDK tree matches post-H1 either way!
2. **Expected conflicts when merging `355803af` (or `merge/main-into-batch-claim-2`) into `feat/batch-11-acquire-annotations`:**
   - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_client.py` & `async_sandbox_client.py`: `17/N` added `pool_size: int | None = None` while `#2041` added `*, file_config: SandboxFileConfig | None = None`. Keep **both** in order (`api_client: ..., pool_size: int | None = None, *, file_config: SandboxFileConfig | None = None`), keep both `self.file_config = file_config` and `_batches_lock` / batch fields, and in `async_sandbox_client.py` keep both `BatchGroup` and `SandboxFileConfig` imports from `.models`.
   - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py` & `async_sandbox_batch.py` (if merging `merge/main-into-batch-claim-2`): keep both `17/N`'s `(sandbox, pod_ip)` / `_seeded_pod_ip` in `connect()` AND `**({"file_config": self._client.file_config} if self._client.file_config is not None else {})` in `self._client.sandbox_class(...)` (plus `self.mock_client.file_config = None` in `TestConnect.setUp`).
   - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/__init__.py` & `models.py` & `Makefile` (if merging `upstream/main` directly): keep all stack batch exports (`BatchGroup, Member, MemberState, GroupOutcome, BatchEvent, BatchEventType`, `SandboxBatch`, `AsyncSandboxBatch`, all `Batch*Error` / `QuorumUnreachableError` / `TerminalMemberError`) alongside `SandboxResponseTooLargeError`, keep `StrEnum` + `main`'s `models.py` imports, and keep `-m k8s_agent_sandbox.sandbox_batch` + `main`'s 3-expression `sed` in `Makefile`.
   - Run `make generate-python-docs && rm -rf .venv` and include the regenerated `docs/python_sdk_reference.md` so `make generate-python-docs` has zero diff on `review/c3/sdk-post-merge`.
3. Verify `mypy` (`venv` + `venv33`, **0 errors**) and `pytest` (`test_sandbox_batch.py`, `test_async_sandbox_batch.py` `TestConnect` + `TestAcquire`) pass on `review/c3/sdk-post-merge`.

---

### Part C: Rebase RL Commits + `#2035` Cleanup + `#1791` Integration + Batch `pod_annotations` (`review/c3/feat/rl-batch-claim-mode`)

Rebase the 4 commits of `origin/feat/rl-batch-claim-mode` (`5c585caa`, `35390caf`, `7090c57d`, `7252e27f` on base `ff5c7652`) onto `review/c3/sdk-post-merge` as branch `review/c3/feat/rl-batch-claim-mode`, folding each change strictly into the commit that introduces that code (`working_rules.md:24,26,42`). Keep RL's **2-space indentation** in every RL file.

*(Pay careful attention to exact commit boundaries across the 4 RL commits: `FakeBatch` and `_batch_cluster` are introduced in **Commit 2** `35390caf`, NOT Commit 1; `BatchClaim.expires_at` and `SandboxHandle.claim_expires_at` are introduced in **Commit 3** `7090c57d`, NOT Commit 2!)*

#### Commit 1 (`5c585caa` -> `"feat(agent-sandbox-rl): add claim_mode and per-cluster batch claims"`):
1. **Conflict resolutions & `#2035` cleanup (`tasks.md:175-182`):**
   - `examples/agent-sandbox-rl/agent_sandbox_rl/__init__.py`: keep both `BatchClaimConfig` and `main`'s `FailFastPolicy` / `OwnedByAnotherRunError`.
   - `examples/agent-sandbox-rl/agent_sandbox_rl/config.py`: keep `claim_mode` and `batch_claims` (and validators) alongside `main`'s `claim_timeout`, `fail_fast`, `effective_claim_timeout()`, and `prepull_*`.
   - `examples/agent-sandbox-rl/agent_sandbox_rl/cluster.py`:
     - First confirm with a 5-line one-off Python check (do not commit the script) on the merged tree that `Cluster(ClusterConfig(name="c", namespace="ns"), api_client=api).k8s_helper.coordination_v1_api.api_client.configuration` has the injected `api`'s `host` and `retries is False`.
     - Keep `#2035`'s `k8s_helper` and `sandbox_client` (`355803af:cluster.py:105-140`) cleanly: **delete** `_without_retries` (`5c585caa:cluster.py`), `self._leases_injected`, `batch_sandbox_client`, and `import copy` if nothing else in `cluster.py` uses it (`cluster.py` should match `355803af:cluster.py` with zero diff!).
2. **`batch_claims.py` updates (folded into Commit 1):**
   - Change `cluster.batch_sandbox_client().claim_batch(` to `cluster.sandbox_client.claim_batch(`.
   - Update `BatchClaims.acquire`: accept `*, pod_annotations: dict[str, str] | None = None` and pass `pod_annotations=pod_annotations` to `batch.acquire(pool, timeout=self._ready_timeout, pod_annotations=pod_annotations)`.
   - On `BatchClaim` (note: in Commit 1, `BatchClaim` has `batch` and `member`; `expires_at` arrives in Commit 3!), add `rebaselined: bool = False` and zero-arg `still_reaches(self) -> bool` (importing `still_reaches` from `k8s_agent_sandbox.batch_state` and `select_pod_ip` from `k8s_agent_sandbox.utils`):
     ```python
     def still_reaches(self) -> bool:
       if self.rebaselined or self.batch.err() is not None:
         return True
       current = next(
           (m for m in self.batch.members(self.member.warmpool)
            if m.claim_name == self.member.claim_name),
           None)
       return still_reaches(
           current, self.member.sandbox_name, select_pod_ip(self.member.pod_ips))
     ```
3. **`tests/test_batch_claims.py` & commit message in Commit 1:**
   - Commit 1's `tests/test_batch_claims.py` only has `test_claim_mode_defaults_to_per_claim_and_rejects_unknown` and `test_batch_sandbox_client_points_leases_at_the_cluster`: delete `test_batch_sandbox_client_points_leases_at_the_cluster` and the imports it alone used in Commit 1 (`types`, `MagicMock`, `client`, `Cluster`, `ClusterConfig`).
   - Minimally update Commit 1's commit message body to drop the bullet describing `Cluster.batch_sandbox_client` (`working_rules.md:42`).

#### Commit 2 (`35390caf` -> `"feat(agent-sandbox-rl): claim through the batch in fleet.acquire"`):
1. **`handles.py` conflict resolution & batch liveness (`355803af:handles.py` + `35390caf:handles.py`):**
   - Keep `#1960`'s `runtime_container` / `target_container`, `#1791`'s `pod_uid`, `runtime_restarts`, `record_pod_identity()`, `is_alive()`, `_raise_if_lost()`, `forget_claim_in_client()`, AND Commit 2's `_batch_claim` (`claim_expires_at` arrives in Commit 3!).
   - In `SandboxHandle.is_alive()` (`355803af:handles.py:366-393`): right after `if self._cluster is None: return True`, check:
     ```python
     if (self._batch_claim is not None and self.pod_uid is None
         and not self._batch_claim.still_reaches()):
       return False
     ```
   - In `SandboxHandle.refresh()` (`355803af:handles.py:455-495`): right before `self.record_pod_identity()`, set:
     ```python
     if self._batch_claim is not None:
       self._batch_claim.rebaselined = True
     ```
   - Update `record_pod_identity()` and `is_alive()` docstrings in `handles.py` (1 sentence each, active voice, no em dashes/chained colons): note that `acquire()` calls `record_pod_identity()` in `per_claim` mode while `claim_mode="batch"` skips that initial `GET` (using the batch watch for claim loss and pod replacement in `is_alive()`), so callers in batch mode needing container restart-count baselines for in-place OOM restarts can call `handle.record_pod_identity()`.
   - In `SandboxHandle.release()` (`355803af:handles.py:582-596`): when `self._batch_claim is not None`, call `self._batch_claim.release()` and do **not** call `forget_claim_in_client` (only `per_claim` handles register in `SandboxClient._active_connection_sandboxes` / `_explicit_claims`).
2. **`fleet.py` conflict resolution, `#1791` scoping, `effective_claim_timeout()`, and Task 7 Step 3 (`tasks.md:191-199`):**
   - In `SandboxFleet.__init__`: pass `ready_timeout=self.config.effective_claim_timeout()` to `BatchClaims(...)` (Decision 2A), and omit `self._warned_pin_fallback = False`.
   - In `SandboxFleet.setup()`: keep both `if self._batch_claims is not None: self._batch_claims.reopen()` and `self._deleted_namespaces.clear()`.
   - In `SandboxFleet.acquire` docstring: update `within ready_timeout` to reflect `claim_timeout` (or `ready_timeout` when unset), and remove `Acquires with snapshot_uid or pod_annotations keep the per-claim path.`
   - In `SandboxFleet._acquire_reserved`:
     - Delete the `if batch_claims is not None and annotations:` fallback block.
     - Scope `#1791`'s pre-generated `claim_name = f"sandbox-claim-..."` and `ClaimWatchdog` creation strictly to `if batch_claims is None:` (`watchdog = None` on the batch path).
     - On `if batch_claims is not None:`: call `claim = batch_claims.acquire(cluster, pool, pod_annotations=annotations or None)`. (`snapshot_uid` already sets `annotations[constants.PODSNAPSHOT_NAME_ANNOTATION] = snapshot_uid` above, and `AsyncSandboxFleet.acquire` at `async_fleet.py:169-172` already forwards both args.)
     - In `except Exception:`: keep `if claim is not None: claim.release()`, and scope `#1791`'s `claim_name` delete, `forget_claim_in_client(cluster, claim_name)`, and `if verdict is not None:` handling strictly to `batch_claims is None`; keep `finally: if watchdog is not None: watchdog.stop()`.
     - In `SandboxHandle(...)`: keep both `_batch_claim=claim` and `runtime_container=((entry.runtime_container if entry is not None else None) or constants.RUNTIME_CONTAINER)`.
     - Guard `if claim is None: handle.record_pod_identity()` (Decision 3, Option a).
     - In `if torn:`: when `claim is not None`, call `claim.release()` (do **not** call `forget_claim_in_client`); when `claim is None`, call `sandbox.terminate()` and `forget_claim_in_client(cluster, claim_name)`.
   - In `SandboxFleet._teardown` (`355803af:fleet.py:1947-1948`): put `try: self.release_all() finally: if self._batch_claims is not None: self._batch_claims.release_all()` inside `with self._sweep_lock:`.
3. **`tests/test_batch_claims.py` & commit message in Commit 2:**
   - Remove `c.batch_sandbox_client = lambda: c.sandbox_client` in `_batch_cluster` (and drop unused `client` / `Cluster` imports if present).
   - Update `FakeBatch`:
     - Give `acquire(self, warmpool, timeout=None, *, pod_annotations=None)` keyword-only `pod_annotations=None`. Keep `self.acquired.append((warmpool, timeout))` (so existing 2-tuple unpacks in `test_first_acquire_opens_one_batch_from_its_pool` keep working cleanly) and also record `self.acquired_annotations.append(pod_annotations)` (or update the 2-tuple unpacks in Commit 2 — pick whichever is cleaner!).
     - Also update `acquire_during_teardown` in `test_claim_landing_after_teardown_is_released_through_the_batch` (`35390caf:tests/test_batch_claims.py`) to accept `*, pod_annotations=None` and forward `pod_annotations=pod_annotations`!
     - Track live `Member` objects in `self._members: dict[str, Member]` on `FakeBatch` (inserted in `acquire`, popped in `release_member`, cleared in `release`), and add `def members(self, warmpool=None): ...` matching `SandboxBatch.members`.
   - Rewrite `test_pinned_acquire_keeps_the_per_claim_path` -> `test_annotated_and_pinned_acquires_go_through_the_batch`: assert that `f.acquire(t, pod_annotations={"a": "b"}, snapshot_uid="snap-1")` passes `pod_annotations={"a": "b", constants.PODSNAPSHOT_NAME_ANNOTATION: "snap-1"}` to `FakeBatch.acquire`, `c.sandbox_client.create_sandbox` is not called, and `f.release(h)` releases through `FakeBatch.release_member` (`pod_restore_status` catches errors on `MagicMock` `core_api`, setting `h.pending_snapshot_uid = "snap-1"`). Add the matching `pod_annotations` / `snapshot_uid` batch assertion in `test_async_fleet_claims_releases_and_tears_down_through_one_batch` for sync/async parity. *(Note: `h.claim_expires_at is not None` is added in Commit 3 where `claim_expires_at` lands!)*
   - Add concise tests in Commit 2 covering:
     1. `FleetConfig(claim_mode="batch", claim_timeout=120, ready_timeout=900)` passes `quorum_timeout=120` to `claim_batch` and `timeout=120` to `FakeBatch.acquire`.
     2. Batch `acquire()` makes **0** `read_namespaced_pod_status` calls (`record_pod_identity` skipped); `h.is_alive()` returns `False` with 0 `GET`s when the watched member in `FakeBatch` becomes `LOST` or changes `pod_ips`, and falls through to `read_namespaced_pod_status` when the member still matches, when `batch.err()` is set, after explicit `h.record_pod_identity()`, or after `h.refresh()` (`rebaselined = True`).
   - Minimally update Commit 2's commit message body to replace the bullet saying `snapshot_uid` / `pod_annotations` keep the per-claim path (and note the `claim_timeout` + 0-GET `is_alive()` watch check).
   - **Mutation-check** each new/changed RL test.

#### Commit 3 (`7090c57d` -> `"feat(agent-sandbox-rl): rotate recycled batch claims before they expire"`):
- Rebase onto Commit 2 (`batch_claims.py` `expires_at`, `handles.py` `claim_expires_at`, `recycle.py`, and recycle tests in `test_batch_claims.py`), and add `assert h.claim_expires_at is not None` to `test_annotated_and_pinned_acquires_go_through_the_batch` (replacing `7090c57d`'s old `and h.claim_expires_at is None` hunk on `test_pinned_acquire_keeps_the_per_claim_path`).

#### Commit 4 (`7252e27f` -> `"docs(agent-sandbox-rl): document claim_mode"`):
- Update `examples/agent-sandbox-rl/README.md` (`7252e27f:README.md:528-560`, active voice, no em dashes, avoid colons where natural):
  1. Remove/replace the `"Acquires with snapshot_uid or pod_annotations keep the per-claim path..."` bullet (annotated and pinned acquires go through the batch).
  2. Update the `shutdownTime` bullet (`ready_timeout + work_budget + 600`) to say `claim_timeout` (or `ready_timeout` when `claim_timeout` is unset) plus `work_budget + 600`.
  3. Add the 1-sentence note that `claim_mode="batch"` skips the initial `pods/{name}/status` read on `acquire()` (checking the batch watch for claim loss and pod replacement in `is_alive()`), and callers needing container restart-count baselines for in-place OOM detection can call `handle.record_pod_identity()`.
- Leave `k8s-agent-sandbox` version floor in `examples/agent-sandbox-rl/pyproject.toml` unchanged (`tasks.md:196`).

---

### Verification, compare links & push

1. **Verify `git grep -E 'batch_sandbox_client|_without_retries|_leases_injected'` on `review/c3/feat/rl-batch-claim-mode` is completely empty** (`tasks.md:210`).
2. **Per-commit checks (run on EVERY new commit across `feat/batch-11-acquire-annotations`, `review/c3/sdk-post-merge`, and all 4 commits of `review/c3/feat/rl-batch-claim-mode`):**
   - Author & trailers: `Brian Nguyen <brianknguyen@google.com>`, zero trailers.
   - SDK (`feat/batch-11-acquire-annotations` + `review/c3/sdk-post-merge`): `pycodestyle`, `mypy` on `venv` (`36.1.0`) and `venv33` (`33.3.0`) with **0 errors**, `make generate-python-docs && rm -rf .venv` zero diff, `pytest` on changed sync/async batch tests.
   - RL (`review/c3/feat/rl-batch-claim-mode`, each of Commits 1–4): `pytest` on `examples/agent-sandbox-rl/tests/test_batch_claims.py`, `tests/test_fail_fast.py`, `tests/test_cluster.py`, `tests/test_snapshots.py`; `MYPYPATH=clients/python/agentic-sandbox-client python -m mypy examples/agent-sandbox-rl/agent_sandbox_rl` compared against `review/c3/sdk-post-merge` (**0 new errors**, and `python -m mypy --ignore-missing-imports examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py` **0 errors**); lint changed RL lines.
3. **Independent self-check (`inherit` subagent):** spawn one fresh `inherit` subagent (`working_rules.md:80-83`) given only the task spec, the 1-sentence lifecycle rule (and the 5 paths: fresh acquire, explicit `record_pod_identity()`, `suspend()` before `resume()`, `resume()`/`restore()` -> `refresh()` including when `record_pod_identity()` fails best-effort, and `batch.err()`/`detach()`/`release()`), base/head SHAs of `feat/batch-11-acquire-annotations` and `review/c3/feat/rl-batch-claim-mode`, `working_rules.md`, `skills/test-audit/SKILL.md`, and `"Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?"`. Resolve any findings before pushing.
4. **Check `git diff --shortstat` on every compare link (`learnings.md` 7.1):**
   - Verify `git diff --shortstat <sdk-top> feat/batch-11-acquire-annotations` shows only `23/N` (`compare/<sdk-top>...feat/batch-11-acquire-annotations`).
   - Verify `git diff --shortstat review/c3/sdk-post-merge review/c3/feat/rl-batch-claim-mode` shows only the 4 RL commits (`compare/review/c3/sdk-post-merge...review/c3/feat/rl-batch-claim-mode`), and include individual commit links for all 4 RL commits.
5. **Push & report:**
   - Push `feat/batch-11-acquire-annotations`, `review/c3/sdk-post-merge`, and `review/c3/feat/rl-batch-claim-mode` to `origin`, verifying every branch with `git ls-remote`.
   - Write your concise report to `batch-primitives/handoff/work/results/H2-task7-acquire-annotations.md`, commit only that file with `--only`, push safely per the Notes rule above, and verify with `git ls-remote`.
````
