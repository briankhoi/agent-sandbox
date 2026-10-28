# H2: SDK `23/N` `acquire()` `pod_annotations`, Post-Merge Base, and RL Batch Rebase

Date 2026-10-28. Written by the H2 task conversation.

## 1. Summary

Three branches are pushed to `origin` (`briankhoi/agent-sandbox`):

- **`feat/batch-11-acquire-annotations` (`97670d11`, `23/N`)** branches linearly off `origin/feat/batch-10-deletecollection-retry` (`d7e78189`) with zero merge commits (verified). Commit 1 (`f5e91a3a`) adds keyword-only `*, pod_annotations: dict[str, str] | None = None` to `SandboxBatch.acquire()` and `AsyncSandboxBatch.acquire()`, validates and builds `spec.additionalPodMetadata` via `build_pod_metadata(None, pod_annotations)` before `_check_pool`, threads `pod_metadata` across create retries, and updates `clients/python/agentic-sandbox-client/README.md`. Commit 2 (`97670d11`) regenerates `docs/python_sdk_reference.md`.
- **`review/c3/sdk-post-merge` (`778b60e8`)** merges `origin/merge/main-into-batch-claim-2` (`12e04eb7`, carrying `upstream/main` `355803af` and Decision 1A's `file_config` forwarding) into `feat/batch-11-acquire-annotations` (`97670d11`) with regenerated `docs/python_sdk_reference.md`.
- **`review/c3/feat/rl-batch-claim-mode` (`d9d5c258`)** rebases the 4 commits of `origin/feat/rl-batch-claim-mode` (`5c585caa`, `35390caf`, `7090c57d`, `7252e27f`) onto `review/c3/sdk-post-merge`, folding `#2035` cleanup, `#1791` `effective_claim_timeout()` (Decision 2A), `#1791` 0-GET batch-watch liveness (Decision 3), and batch `pod_annotations` / `snapshot_uid` routing strictly into their owning commits (`b68b46e2`, `599897dd`, `47a5e046`, `d9d5c258`).

All per-commit checks pass across all 7 commits: `mypy` with 0 errors on `venv` (`kubernetes_asyncio` 36.1.0) and `venv33` (`kubernetes_asyncio` 33.3.0) for the SDK, 0 new `mypy` errors across `examples/agent-sandbox-rl/agent_sandbox_rl` and 0 errors on `batch_claims.py`, `pycodestyle`, `make generate-python-docs` (zero diff), `make toc-verify`, sync/async `pytest` suites with mutation checks, and author/trailer checks (measured).

## 2. Ranked findings

1. **SDK `23/N` `pod_annotations` forwarding in `acquire()` (`f5e91a3a`, `97670d11`)**
   - Recommendation. Keep `pod_metadata = build_pod_metadata(None, pod_annotations)` inside `acquire()` right after `timeout` validation and before `_check_pool`, threaded as `pod_metadata: AdditionalPodMetadata | None = None` through `_create_one` and `_send_create`.
   - Reason. `build_pod_metadata(None, pod_annotations)` (`pod_metadata.py:69-92`, verified) rejects invalid keys/types up front before any pool `GET` or claim `POST` and returns `None` for `None` or `{}` so empty `spec.additionalPodMetadata` is never emitted (`sandbox_batch.py:690,1541,1575`, `async_sandbox_batch.py:704,1546,1583`, verified). Both shells delegate `helper.create_sandbox_claim` in `TestAcquire` to a real `K8sHelper` / `AsyncK8sHelper` (`test_sandbox_batch.py:2818-2865`, `test_async_sandbox_batch.py:2907-2953`, verified) and verify wire manifests across a 503 create retry, catching mutations in both `acquire -> _create_one` and `_create_one -> _send_create` (measured).

2. **`#1791` 0-GET batch-watch liveness and `record_pod_identity()` skip (`599897dd`)**
   - Recommendation. Keep `if claim is None: handle.record_pod_identity()` in `SandboxFleet._acquire_reserved` (`fleet.py:1648-1649`, verified), zero-arg `BatchClaim.still_reaches()` (`batch_claims.py:64-75`, verified), `rebaselined: bool = False` flipped in `SandboxHandle.refresh()` (`handles.py:516-517`, verified), and the guard in `SandboxHandle.is_alive()` (`handles.py:400-404`, verified).
   - Reason. Skipping `record_pod_identity()` on batch `acquire()` preserves 0 per-claim `GET`s while `is_alive()` returns `False` with 0 `GET`s when a healthy batch's watch (`batch.err() is None`) sees claim deletion, `LOST`/`FAILED`, or a changed `sandbox_name` / `pod_ip`, and falls through to `read_namespaced_pod_status` otherwise (verified across all 5 lifecycle paths and measured in `test_batch_claims.py:337-391`). Because a resumed or restored pod keeps `Name: sandbox.Name` (`controllers/sandbox_controller.go:1574,1824`, verified) and `record_pod_identity()` inside `refresh()` is best-effort (`handles.py:362-377`, verified), `rebaselined: bool = False` prevents `still_reaches()` from comparing a stale pre-resume claim IP when `pod_uid` stays `None` after `refresh()`.

3. **`#1960` `container` keyword interaction in Commit 3's recycle test fixture (`47a5e046`)**
   - Recommendation. Keep `lambda self, cmd, **kw: _CLEAN` on `SandboxHandle.exec` in `_recycle_on_a_fake_clock` (`test_batch_claims.py:446`, verified).
   - Reason. `#1960` (`355803af:recycle.py:53-54,175`, verified) passes `container=handle.runtime_container` into `handle.exec(...)` inside `GitRestoreReset.prime()` and `reset()`. Commit 3's pre-rebase `lambda self, cmd: _CLEAN` raised `TypeError` inside `prime()`, which `recycle.py` caught as non-recyclable (`recyclable = False`), causing 3 claims to be acquired instead of 2 until `**kw` was added to the test double (measured).

4. **`#2035` cleanup, `#1791` `effective_claim_timeout()`, and `mypy` type narrowing in `fleet.py` (`b68b46e2`, `599897dd`)**
   - Recommendation. Keep `cluster.py` identical to `355803af` (0 diff), pass `ready_timeout=self.config.effective_claim_timeout()` into `BatchClaims(...)` (`fleet.py:225-226`, verified), and pass `claim_name=sandbox.claim_name or claim_name` to `SandboxHandle` (`fleet.py:1642`, verified).
   - Reason. A one-off check confirmed `Cluster(..., api_client=api).k8s_helper.coordination_v1_api.api_client.configuration` preserves `api`'s `host` and `retries is False` via `#2035` (measured), and `git grep -E 'batch_sandbox_client|_without_retries|_leases_injected' -- examples/agent-sandbox-rl clients/python` is empty (measured, while unrelated `examples/agent-sandbox-gymnasium/rllib/test_run_verification.py` defines `test_cleanup_http_status_preserves_identity_without_retries`). In `fleet.py`, assigning `sandbox = claim.batch.connect(claim.member)` (`Sandbox`) widens `sandbox` from `Any` to `Sandbox | Any`, exposing `Sandbox.claim_name: str | None` (`sandbox.py:66`, verified). Passing `sandbox.claim_name or claim_name` narrows it back to `str` in one expression with 0 new `mypy` errors (measured).

## 3. Self-check result

A fresh `inherit` subagent (`82041eae-ec7d-4dce-ac39-ee412530ae36`) audited `d7e78189..feat/batch-11-acquire-annotations`, `feat/batch-11-acquire-annotations..review/c3/sdk-post-merge`, and `review/c3/sdk-post-merge..review/c3/feat/rl-batch-claim-mode` against the task goal, Decision 3's 1-sentence lifecycle rule and all 5 lifecycle paths, `working_rules.md`, `skills/test-audit/SKILL.md`, and the minimal-change prompt without seeing my findings.

- **Production logic and 5 lifecycle paths.** The reviewer confirmed zero correctness gaps across SDK `23/N`, the post-merge conflict resolutions, `#2035` cleanup, Decision 2A (`effective_claim_timeout()`), and all 5 lifecycle paths of Decision 3 (`fresh acquire`, `explicit record_pod_identity()`, `suspend() before resume()`, `resume()/restore() -> refresh()` when `record_pod_identity()` succeeds or fails best-effort, and `batch.err()/detach()/release()`), and confirmed `rebaselined: bool = False` is the minimal required state.
- **Reviewer finding 1 (commit-boundary leak between RL Commits 1 and 2).** `BatchClaim.rebaselined` and `BatchClaim.still_reaches()` had been placed in RL Commit 1 with zero callers until Commit 2.
  - Resolution. Moved `rebaselined`, `still_reaches()`, and the `still_reaches` / `select_pod_ip` imports from Commit 1 (`b68b46e2`) into Commit 2 (`599897dd`, `batch_claims.py:36-37,57-70`, verified), matching how `expires_at` lands in Commit 3 (`47a5e046`), and noted `pod_annotations` forwarding in Commit 1's message.
- **Reviewer finding 2 (SDK `README.md` signature in `23/N`).** `clients/python/agentic-sandbox-client/README.md` still listed `acquire(warmpool, timeout=None)` without `*, pod_annotations=None`.
  - Resolution. Updated `clients/python/agentic-sandbox-client/README.md:776-777,812` in Commit 1 (`f5e91a3a`, verified), matching how `18/N` (`b80bbd9c`) updated `README.md` alongside code.
- **Reviewer finding 3 (`file_config` positive test on `sdk-post-merge`).** My initial `upstream/main` merge commit set `self.mock_client.file_config = None` without a positive non-`None` `file_config` test because `origin/merge/main-into-batch-claim-2` had not been pushed yet when Part B first ran.
  - Resolution. Once H1 pushed `origin/merge/main-into-batch-claim-2` (`12e04eb7`), rebuilt `review/c3/sdk-post-merge` (`778b60e8`) by merging `origin/merge/main-into-batch-claim-2` directly into `feat/batch-11-acquire-annotations`, bringing in H1's `TestConnect.test_forwards_client_file_config_when_set` in both sync and async suites (`test_sandbox_batch.py:963-980`, `test_async_sandbox_batch.py:1020-1037`, verified, with 38 `TestConnect or TestAcquire` tests passing, measured).
- **Reviewer finding 4 (`FakeBatch.release()` fidelity).** `FakeBatch.release()` cleared `self._members`, whereas real `SandboxBatch.release()` (`sandbox_batch.py:1000-1059`, verified) leaves `_state._members` at its last snapshot.
  - Resolution. Removed `self._members.clear()` from `FakeBatch.release()` (`test_batch_claims.py:82-83` in `599897dd`, verified).
- **Reviewer finding 5 (smaller expression for `claim_name` in `fleet.py`).** A 5-line `handle_claim_name` ternary in `fleet.py` could be written directly as `claim_name=sandbox.claim_name or claim_name`.
  - Resolution. Simplified to `claim_name=sandbox.claim_name or claim_name` (`fleet.py:1642` in `599897dd`, verified, with 0 new `mypy` errors, measured).
- **Reviewer finding 6 (colon in comment at `test_batch_claims.py:134`).** Noted `# One size-0 group, from the first acquire's pool only: claim_batch prechecks`.
  - Resolution. Left unchanged per `working_rules.md:45` because that line is Brian's unchanged comment from `35390caf` (verified).
- **Test audit (`skills/test-audit/SKILL.md`).** **0** tests flagged for removal out of 20 added tests reviewed (2 in SDK `f5e91a3a` + 18 in RL `test_batch_claims.py`).

## 4. Decisions for Brian

1. **SDK `23/N` PR (`feat/batch-11-acquire-annotations`, target base `feature/batch-claim` once `9/N` through `22/N` merge).**
   - **Drafted PR title:**
     ```text
     feat(python-sdk): pass pod_annotations in batch acquire (23/N)
     ```
   - **Drafted PR body:**
     ````markdown
     #### What this PR does / why we need it:

     This PR is the twenty-third in a series of PRs to implement a batch. It lets `SandboxBatch.acquire()` and `AsyncSandboxBatch.acquire()` stamp annotations onto an acquired member's pod.
     - `acquire(warmpool, timeout=None, *, pod_annotations=None)` validates non-empty `pod_annotations` up front with `build_pod_metadata(None, pod_annotations)` and writes them to `spec.additionalPodMetadata.annotations` on the `SandboxClaim` across create retries so the controller propagates them onto the sandbox pod.
     - `None` and empty dicts omit `spec.additionalPodMetadata`.

     #### Which issue(s) this PR is related to:

     Ref #1876

     #### Release Note
     ```release-note
     NONE
     ```
     ````

2. **Approve syncing `review/c3/feat/rl-batch-claim-mode` (`d9d5c258`) once `merge/main-into-batch-claim-2` and the SDK stack through `23/N` land, and update the RL batch PR summary notes:**
   - Remove the note that acquires with `snapshot_uid` or `pod_annotations` fall back to `per_claim`, because annotated and snapshot-pinned acquires now pass `pod_annotations` through `SandboxBatch.acquire()`.
   - Remove any mention of `Cluster.batch_sandbox_client` (`#2035` injects the cluster's `ApiClient` with `retries=False` on Lease calls directly into `Cluster.sandbox_client`).
   - Note that `BatchClaims` uses `FleetConfig.effective_claim_timeout()` (`claim_timeout` when set, else `ready_timeout`) for `quorum_timeout`, `acquire()` readiness wait, `shutdownTime`, and recycle rotation (`expires_at`).
   - Note that `claim_mode="batch"` skips the initial `pods/{name}/status` `GET` on `acquire()` and checks a healthy batch's watch for claim loss and pod replacement in `is_alive()` before falling back to `read_namespaced_pod_status`, while callers needing container restart-count baselines for in-place OOM detection can call `handle.record_pod_identity()`.

## 5. Links

- **SDK `23/N` compare (`feat/batch-10-deletecollection-retry...feat/batch-11-acquire-annotations`, 6 files, +170/-13):** https://github.com/briankhoi/agent-sandbox/compare/feat/batch-10-deletecollection-retry...feat/batch-11-acquire-annotations
  - Commit 1 (`feat(python-sdk): pass pod_annotations in batch acquire (23/N)`): https://github.com/briankhoi/agent-sandbox/commit/f5e91a3a3188142c8d27203833b3c02d82bb7d87
  - Commit 2 (`docs(python-sdk): regenerate Python SDK reference for acquire pod_annotations`): https://github.com/briankhoi/agent-sandbox/commit/97670d11e0bb8ee4e6cd68b0e96012e0f25a7e9d
- **Simulated post-merge base commit (`review/c3/sdk-post-merge`):** https://github.com/briankhoi/agent-sandbox/commit/778b60e87cbc4b191b1955baf63ebcf4bf46b4ab
- **RL 4-commit compare (`review/c3/sdk-post-merge...review/c3/feat/rl-batch-claim-mode`, 9 files, +911/-59):** https://github.com/briankhoi/agent-sandbox/compare/review/c3/sdk-post-merge...review/c3/feat/rl-batch-claim-mode
  - RL Commit 1 (`feat(agent-sandbox-rl): add claim_mode and per-cluster batch claims`): https://github.com/briankhoi/agent-sandbox/commit/b68b46e2ff8fe5e6cec9687a7dcfe6129f9ed79c
  - RL Commit 2 (`feat(agent-sandbox-rl): claim through the batch in fleet.acquire`): https://github.com/briankhoi/agent-sandbox/commit/599897dd24bf204bc17a7fbae6df11a519e9d2f2
  - RL Commit 3 (`feat(agent-sandbox-rl): rotate recycled batch claims before they expire`): https://github.com/briankhoi/agent-sandbox/commit/47a5e046d0fb7ec9f06004688648bf4fe04e234a
  - RL Commit 4 (`docs(agent-sandbox-rl): document claim_mode`): https://github.com/briankhoi/agent-sandbox/commit/d9d5c25808805d7c888d53f44a0e409b2c406938
- **This report:** https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/H2-task7-acquire-annotations.md
